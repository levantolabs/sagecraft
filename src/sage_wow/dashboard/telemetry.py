"""Read-only event projection shared by native overlay and browser display."""
from datetime import datetime, timezone
import json
import math
import sqlite3
import time
from sage_wow.status import read_status


def stamp(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def live_snapshot(directory, now=None):
    now=time.time() if now is None else now
    runner_status=read_status(directory)
    result={'state':runner_status.get('state','STOPPED'),'phase':'—','choice':'—','input':'—','outcome':'—',
            'elapsed':0,'decision_age':None,'request_age':None,'progress_age':None,'progress':'—','latency':None,'recent':[],
            'last_self_review_age':None,'next_self_review_seconds':None,'run_remaining_seconds':None,
            'latest_cast_burst_step':None,'cast_burst_active':False,'cast_burst_age':None,
            'v2_controller':None,'grind_controller':None}
    path=directory/'events.sqlite3'
    if not path.exists():return result
    with sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=.2) as conn:
        def latest(kind):
            row=conn.execute('SELECT occurred_at,payload_json FROM events WHERE event_type=? ORDER BY occurred_at DESC LIMIT 1',(kind,)).fetchone()
            return (stamp(row[0]),json.loads(row[1])) if row else (None,{})
        start,_=latest('session_started'); stopped,_=latest('session_stopped')
        if start:result['elapsed']=max(0,(stopped if stopped and stopped>start else now)-start)
        result['v2_controller'] = _current_v2_status(conn, now)
        at,context=latest('tactical_context');result['phase']=context.get('task','LEGACY' if start else '—')
        if at:result['context_age']=now-at
        dec,decision=latest('sage_decision')
        if dec:
            result.update(choice=decision.get('chosen') or 'No safe choice',decision_age=max(0,now-dec),latency=decision.get('request_duration_ms'))
        req,request=latest('sage_request_started')
        terminal=[]
        for kind in ('sage_request_failed','sage_response_discarded'):
            at,payload=latest(kind)
            if at and payload.get('request_id')==request.get('request_id'):terminal.append(at)
        if req and (dec is None or req>dec) and not terminal and result['state']=='PLAYING':
            result['request_age']=max(0,now-req)
        at,action=latest('action_dispatched')
        discarded,rejection=latest('sage_response_discarded')
        failed,failure=latest('action_execution_failed')
        if at:
            actual=action.get('execution',{}).get('dispatched',False)
            result['input']=(action.get('chosen','—')+' · sent') if actual else 'Observation / no input'
        if discarded and (not at or discarded>at):result['input']='Discarded: '+rejection.get('reason','stale')
        if failed and (not at or failed>at):
            result['input']='Partial sequence; stopped' if failure.get('execution',{}).get('completed_steps') else 'Input rejected'
        _,outcome=latest('action_outcome');result['outcome']=outcome.get('outcome','Not yet verified')
        progress_at,progress=latest('quest_progress_observed')
        if progress_at:result['progress_age']=max(0,now-progress_at);result['progress']=f"{progress.get('count','?')}/{progress.get('total','?')} {progress.get('item','quest items')}"
        row=conn.execute("SELECT payload_json FROM checkpoints WHERE checkpoint_key='tactical'").fetchone()
        if row:
            tactical=json.loads(row[0]);result['phase']=tactical.get('phase',result['phase'])
            result['position']=tactical.get('observation',{}).get('position')
            last_review=tactical.get('last_self_review_at')
            if isinstance(last_review,(int,float)) and not isinstance(last_review,bool):
                age=max(0,now-float(last_review))
                result['last_self_review_age']=age
                result['next_self_review_seconds']=max(0,60-age)
            # An explicit cleared progress value must retire older tracker OCR
            # after hand-in or a switch to freeform grinding.
            if 'progress' in tactical:
                checkpoint_progress=tactical.get('progress')
                mode=tactical.get('game_mode')
                handins=tactical.get('handin_history') or []
                if checkpoint_progress is None and (mode in {'AWAITING_TASK','GRIND'} or handins):
                    progress=None
                    handoff=tactical.get('quest_handoff') or {}
                    if mode=='GRIND' or handoff.get('status')=='grinding':
                        result['progress']='Grinding · no active quest'
                    elif handoff.get('status')=='new_task_confirmed':
                        result['progress']='New task selected · awaiting progress'
                    elif handins:
                        result['progress']='Hand-in confirmed · choosing next task'
                    else:
                        result['progress']='Choosing next task'
                else:
                    # Display the same committed progress used by gameplay.
                    progress=checkpoint_progress or progress
        if progress:
            result['progress']=f"{progress.get('count','?')}/{progress.get('total','?')} {progress.get('item','quest items')}"
            if progress.get('status')=='ready_for_turn_in':result['progress']+=' · ready for turn-in'
        rows=conn.execute("SELECT occurred_at,payload_json FROM events WHERE event_type='action_dispatched' ORDER BY occurred_at DESC LIMIT 18").fetchall()
        result['recent']=[{'at':stamp(at),'action':json.loads(raw).get('chosen')} for at,raw in rows if json.loads(raw).get('execution',{}).get('dispatched')][:4]
        burst_at,burst=latest('cast_burst_step')
        if burst_at:
            result['latest_cast_burst_step']=burst
            result['cast_burst_age']=max(0,now-burst_at)
            terminal_at=_latest_burst_terminal(conn)
            result['cast_burst_active']=(result['state']=='PLAYING' and burst_at>(terminal_at or 0)
                                         and result['cast_burst_age']<=10)
        _project_grind(conn, runner_status, result, now)
    return result


def _project_grind(conn, runner_status, result, now):
    """Project current-run facts into existing HUD lines, never infer effects."""
    start=conn.execute("SELECT rowid,occurred_at,payload_json FROM events WHERE event_type='session_started' ORDER BY rowid DESC LIMIT 1").fetchone()
    if start:
        started=json.loads(start[2])
        if started.get('mode')!='grind_only':return
        start_row,start_at=start[0],stamp(start[1])
    else:
        if runner_status.get('mode')!='grind_only':return
        started={};start_row=0;start_at=0
    if result['v2_controller']:return
    session_id=started.get('session_id') or runner_status.get('session_id')
    status=runner_status if not started.get('session_id') or runner_status.get('session_id')==session_id else {}

    def latest(kind):
        row=conn.execute('SELECT occurred_at,payload_json FROM events WHERE event_type=? AND rowid>? ORDER BY rowid DESC LIMIT 1',(kind,start_row)).fetchone()
        return (stamp(row[0]),json.loads(row[1])) if row else (None,{})

    def text(value,limit=240):
        return value[:limit] if isinstance(value,str) else None

    def number(value):
        return value if type(value) is int and value>=0 else None

    row=conn.execute("SELECT saved_at,payload_json FROM checkpoints WHERE checkpoint_key='grind_only'").fetchone()
    checkpoint_at=stamp(row[0]) if row else 0
    checkpoint=json.loads(row[1]) if row and checkpoint_at>=start_at else {}
    hunt=checkpoint.get('hunt') or {}
    question_at,question=latest('grind_compact_question')
    cycle_at,cycle=latest('grind_cycle_result')
    phase=text(hunt.get('phase') or status.get('phase') or question.get('stage'),80) or 'initializing'
    if question_at and question_at>checkpoint_at:phase=text(question.get('stage'),80) or phase
    decision_at,decision=latest('sage_decision')
    result.update(choice=text(decision.get('chosen')) or ('No safe choice' if decision_at else '—'),
        decision_age=max(0,now-decision_at) if decision_at else None,
        latency=decision.get('request_duration_ms'))
    request_at,request=latest('sage_request_started')
    terminal=any(latest(kind)[1].get('request_id')==request.get('request_id')
                 for kind in ('sage_request_failed','sage_response_discarded'))
    result['request_age']=(max(0,now-request_at) if request_at and
        (not decision_at or request_at>decision_at) and not terminal and result['state']=='PLAYING' else None)
    receipt_at,raw_receipt=latest('execution_receipt')
    receipt=None
    result['input']='No input receipt yet'
    if receipt_at:
        receipt={key:text(raw_receipt.get(key)) for key in ('receipt_id','chosen_option','effect_status','error')}
        receipt.update({key:raw_receipt.get(key) if type(raw_receipt.get(key)) is bool else None
            for key in ('attempted','possible_input','dispatched','dispatch_unknown','completed')})
        action=action_label(receipt['chosen_option'] or 'unknown action')
        if receipt['dispatch_unknown'] is True:
            result['input']=action+' · dispatch unknown; input may have occurred'
        elif receipt['possible_input'] is True or receipt['dispatched'] is True:
            result['input']=action+(' · completed; effect unverified' if receipt['completed'] is True
                                   else ' · partial input; completion unverified')
        elif receipt['attempted'] is False and not receipt['error']:
            result['input']='Observation / no input'
        elif receipt['error'] or receipt['completed'] is False:
            result['input']=action+' · input rejected'
        else:result['input']='Input status unknown'
    outcome_at,outcome=latest('grind_action_outcome')
    # Semantic history compaction is bookkeeping, not an observed game effect.
    if outcome.get('purpose')=='semantic_question':
        row=conn.execute("SELECT occurred_at,payload_json FROM events WHERE event_type='grind_action_outcome' AND rowid>? ORDER BY rowid DESC",(start_row,))
        outcome_at,outcome=None,{}
        for at,raw in row:
            candidate=json.loads(raw)
            if candidate.get('purpose')!='semantic_question':outcome_at,outcome=stamp(at),candidate;break
    effect=text(outcome.get('outcome'))
    result['outcome']=((text(outcome.get('purpose'),60) or 'action')+': '+effect) if effect else 'Not yet verified'
    current=number(status.get('player_level'))
    if current is None:current=number(checkpoint.get('level'))
    goal=number(status.get('goal_level'))
    if goal is None:goal=number(checkpoint.get('goal_level'))
    recorded_kills=conn.execute("SELECT count(*) FROM events WHERE event_type='grind_credited_kill' AND rowid>?",(start_row,)).fetchone()[0]
    kills=max(number(status.get('verified_kills')) or 0,recorded_kills)
    recovery=status.get('recovery') if 'recovery' in status else hunt.get('recovery_requested')
    blocked=status.get('blocked') if 'blocked' in status else hunt.get('blocked')
    waiting=text(status.get('reason') or checkpoint.get('reason'))
    if not waiting and isinstance(blocked,dict):waiting=text(blocked.get('reason'))
    if not waiting and isinstance(recovery,dict):waiting=text(recovery.get('reason'))
    if not waiting and cycle.get('status') not in {None,'dispatched'}:waiting=text(cycle.get('detail') or cycle.get('status'))
    if not waiting and hunt.get('pending_outcome'):
        waiting='Awaiting observed effect: '+str(hunt['pending_outcome'].get('action','last input'))[:100]
    if not waiting and result.get('request_age') is not None:waiting=text(question.get('question')) or 'Sage assessment pending'
    deadline=status.get('deadline',checkpoint.get('deadline'))
    remaining=max(0,deadline-now) if type(deadline) in (int,float) and math.isfinite(deadline) else None
    result['grind_controller']={'controller':'grind_only','session_id':session_id,'phase':phase,
        'question_stage':text(question.get('stage'),80),'status':text(cycle.get('status'),80),
        'current_level':current,'goal_level':goal,'verified_kills':kills,
        'waiting_reason':waiting,'deadline_remaining_seconds':remaining,'last_receipt':receipt,
        'last_outcome':{'purpose':text(outcome.get('purpose'),60),'outcome':effect,
            'receipt_id':text(outcome.get('receipt_id')),'reason':text(outcome.get('reason'))} if outcome_at else None}
    result.update(phase='GRIND '+phase.upper(),progress=f'Level {current or "?"} / goal {goal or "?"} · {kills} credited kills',
        progress_age=None,cast_burst_active=False)
    rows=conn.execute("SELECT occurred_at,payload_json FROM events WHERE event_type='execution_receipt' AND rowid>? ORDER BY rowid DESC LIMIT 24",(start_row,)).fetchall()
    result['recent']=[{'at':stamp(at),'action':text(item.get('chosen_option')) or 'unknown action'}
        for at,raw in rows if (item:=json.loads(raw)).get('completed') is True
        and item.get('dispatch_unknown') is not True and
        (item.get('possible_input') is True or item.get('dispatched') is True)][:4]


def _current_v2_status(conn, now):
    """Return the latest V2 status only when it belongs to the newest run.

    V2 is opt-in and reports its own session ID. Event row order is used for
    run boundaries so an older V2 status can never outlive a newer legacy
    ``session_started`` event, even when timestamps have equal precision.
    """
    try:
        latest_start = conn.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM events WHERE event_type='session_started'"
        ).fetchone()[0]
        row = conn.execute(
            "SELECT rowid,event_id,occurred_at,payload_json FROM events "
            "WHERE event_type='v2_controller_status' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row or int(row[0]) < int(latest_start or 0):
        return None
    try:
        payload = json.loads(row[3])
        at = stamp(row[2])
    except (TypeError, ValueError, json.JSONDecodeError, OverflowError):
        return None
    if not isinstance(payload, dict) or payload.get('controller') != 'v2':
        return None
    session_id = payload.get('session_id')
    if not isinstance(session_id, str) or not session_id.strip():
        return None

    def bounded_text(key, limit=500):
        value = payload.get(key)
        return value[:limit] if isinstance(value, str) else None

    def nonnegative_number(value):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None

    deadline = bounded_text('absolute_deadline', 80)
    remaining = None
    if deadline:
        try:
            remaining = max(0.0, stamp(deadline) - now)
        except (TypeError, ValueError, OverflowError):
            pass
    receipt = payload.get('last_receipt')
    if isinstance(receipt, dict):
        receipt = {key: receipt.get(key) for key in (
            'receipt_id', 'attempted', 'possible_input', 'dispatched',
            'dispatch_unknown', 'effect_status', 'chosen_option',
            'authorization_type', 'authorization_id')}
        for key in ('attempted', 'possible_input', 'dispatched', 'dispatch_unknown'):
            if not isinstance(receipt.get(key), bool):
                receipt[key] = None
        if not isinstance(receipt.get('receipt_id'), str):
            receipt['receipt_id'] = None
        if not isinstance(receipt.get('effect_status'), str):
            receipt['effect_status'] = None
        for key in ('chosen_option','authorization_type','authorization_id'):
            value=receipt.get(key)
            receipt[key]=value[:120] if isinstance(value,str) else None
    else:
        receipt = None
    verified_age = nonnegative_number(payload.get('last_verified_progress_age_seconds'))
    if verified_age is not None:
        verified_age += max(0.0, now - at)
    def nonnegative_integer(value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def bounded_progress_summary(value):
        if not isinstance(value, dict) or value.get('session_id') != session_id:
            return None
        summary = {
            'kind': value.get('kind')[:40] if isinstance(value.get('kind'), str) else None,
            'task_id': value.get('task_id')[:160] if isinstance(value.get('task_id'), str) else None,
            'observed_at': value.get('observed_at')[:80] if isinstance(value.get('observed_at'), str) else None,
            'progress_id': value.get('progress_id')[:120] if isinstance(value.get('progress_id'), str) else None,
            'session_id': session_id,
            'value': {},
        }
        raw_value = value.get('value')
        if isinstance(raw_value, dict):
            for key, item in list(raw_value.items())[:8]:
                if not isinstance(key, str):
                    continue
                key = key[:64]
                if isinstance(item, str):
                    summary['value'][key] = item[:120]
                elif item is None or isinstance(item, (bool, int, float)):
                    if not isinstance(item, float) or math.isfinite(item):
                        summary['value'][key] = item
                elif isinstance(item, dict):
                    summary['value'][key] = {
                        str(subkey)[:48]: (subvalue[:100] if isinstance(subvalue, str) else subvalue)
                        for subkey, subvalue in list(item.items())[:5]
                        if isinstance(subkey, str) and (subvalue is None or isinstance(subvalue, (bool, int, float, str)))
                        and (not isinstance(subvalue, float) or math.isfinite(subvalue))
                    }
        return summary

    verified_summary = bounded_progress_summary(payload.get('last_verified_progress'))
    def bounded_sage_quest(value):
        if not isinstance(value, dict) or value.get('session_id') != session_id:
            return None
        checklist = {}
        raw_checklist = value.get('checklist')
        if isinstance(raw_checklist, dict):
            for key, row in list(raw_checklist.items())[:16]:
                if (isinstance(key, str) and isinstance(row, dict)
                        and isinstance(row.get('complete'), bool)):
                    checklist[key[:64]] = {
                        'complete': row['complete'],
                        'verified_count': nonnegative_integer(row.get('verified_count')),
                        'required': nonnegative_integer(row.get('required')),
                    }
        history = []
        for row in value.get('objective_history', [])[-8:] if isinstance(value.get('objective_history'), list) else []:
            if not isinstance(row, dict):
                continue
            history.append({key: row.get(key)[:160] if isinstance(row.get(key), str) else None
                            for key in ('objective_id', 'objective_type', 'objective_name', 'task_id', 'observed_at')})
        transitions = []
        for row in value.get('objective_transitions', [])[-8:] if isinstance(value.get('objective_transitions'), list) else []:
            if isinstance(row, dict):
                transitions.append({key: row.get(key)[:160] if isinstance(row.get(key), str) else None
                                    for key in ('from_objective_id', 'from_objective_name',
                                                'selected_type', 'request_id', 'selected_at')})
        spell_reviews = []
        for row in value.get('spell_reviews', [])[-20:] if isinstance(value.get('spell_reviews'), list) else []:
            if isinstance(row, dict):
                spell_reviews.append({key: row.get(key) for key in ('level', 'status', 'decision', 'dependency')
                                      if isinstance(row.get(key), (str, int, float, dict)) or row.get(key) is None})
        interventions = []
        for row in value.get('interventions', [])[-8:] if isinstance(value.get('interventions'), list) else []:
            if isinstance(row, dict):
                interventions.append({'reason': bounded_text_value(row.get('reason'), 240)})
        return {
            'name': 'Sage Quest', 'session_id': session_id,
            'target_level': nonnegative_integer(value.get('target_level')),
            'checklist': checklist, 'active_objective': bounded_text_value(value.get('active_objective'), 160),
            'objective_history': history, 'objective_transitions': transitions,
            'elapsed_seconds': nonnegative_number(value.get('elapsed_seconds')),
            'remaining_seconds': nonnegative_number(value.get('remaining_seconds')),
            'latest_sage_choice': bounded_text_value(value.get('latest_sage_choice'), 160),
            'latest_verified_outcome': bounded_text_value(value.get('latest_verified_outcome'), 240),
            'spell_reviews': spell_reviews, 'complete': value.get('complete') is True,
            'interventions': interventions,
        }

    def bounded_text_value(value, limit):
        return value[:limit] if isinstance(value, str) else None

    return {
        'event_id': row[1], 'occurred_at': row[2], 'event_age_seconds': max(0.0, now - at),
        'controller': 'v2', 'status': bounded_text('status', 80),
        'session_id': session_id, 'session_epoch': bounded_text('session_epoch', 120),
        'target_level': payload.get('target_level') if isinstance(payload.get('target_level'), int)
            and not isinstance(payload.get('target_level'), bool) else None,
        'absolute_deadline': deadline, 'deadline_remaining_seconds': remaining,
        'task_id': bounded_text('task_id', 160), 'objective': bounded_text('objective', 160),
        'child_task_id': bounded_text('child_task_id', 160), 'child_phase': bounded_text('child_phase', 120),
        'child_attempts_used': nonnegative_number(payload.get('child_attempts_used')),
        'child_attempt_budget': nonnegative_number(payload.get('child_attempt_budget')),
        'child_no_progress_count': nonnegative_integer(payload.get('child_no_progress_count')),
        'child_no_progress_budget': nonnegative_integer(payload.get('child_no_progress_budget')),
        'next_parent_review_at': bounded_text('next_parent_review_at', 80),
        'last_verified_progress_age_seconds': verified_age,
        'last_verified_progress': verified_summary,
        'sage_quest': bounded_sage_quest(payload.get('sage_quest')),
        'blocked_reason': bounded_text('blocked_reason'), 'last_receipt': receipt,
        'frame_id': bounded_text('frame_id', 160),
    }


def _latest_burst_terminal(conn):
    """Find the latest completed/failed Sage choice for a burst action."""
    rows=conn.execute("""SELECT occurred_at,payload_json FROM events
        WHERE event_type IN ('action_dispatched','action_execution_failed')
        ORDER BY occurred_at DESC LIMIT 80""").fetchall()
    for occurred_at,payload_json in rows:
        payload=json.loads(payload_json)
        chosen=str(payload.get('chosen',''))
        execution=payload.get('execution') or {}
        if chosen in {'smite_burst_2','smite_burst_3'} or execution.get('kind')=='cast_burst':
            return stamp(occurred_at)
    return None


def duration(seconds):
    if seconds is None:return '—'
    seconds=int(max(0,seconds));return f'{seconds//60:02d}:{seconds%60:02d}'


def action_label(value):
    return {'smite':'Attack: Smite','target_quest':'Target quest mob','target_mob':'Target quest mob','loot_interact':'Loot corpse',
            'heal_self':'Heal self','backtrack':'Back away','request_recovery':'Request recovery',
            'quest_ready':'Quest ready for turn-in',
            'target_and_smite':'Target + conditional Smite','loot_visible':'Locate lootable body','loot_verified':'Verify corpse empty','turn_around':'Turn to scan behind',
            'corpse_select_0':'Old corpse selection','smite_burst_2':'Smite burst ×2',
            'smite_burst_3':'Smite burst ×3'}.get(value,value.replace('_',' '))


def display_lines(data):
    pending=data['request_age']
    progress=f"Quest {data['progress']}"
    if data.get('progress_age') is not None:progress+=f" · {duration(data['progress_age'])} ago"
    review=data.get('next_self_review_seconds')
    last_review=data.get('last_self_review_age')
    review_text=(f"Sage check {duration(review)} / last {duration(last_review)} ago"
                 if review is not None else 'Sage check —')
    run_left=data.get('run_remaining_seconds')
    run_text='Run '+duration(run_left)+' left' if run_left is not None else 'Run —'
    burst=data.get('latest_cast_burst_step')
    v2=data.get('v2_controller')
    grind=data.get('grind_controller')
    sage_quest=None
    if v2:
        sage_quest=v2.get('sage_quest') if isinstance(v2.get('sage_quest'),dict) else None
        v2_status=v2.get('status') or 'ACTIVE'
        objective=(v2.get('objective') or 'objective pending').replace('_',' ')[:24]
        attempts=v2.get('child_attempts_used')
        budget=v2.get('child_attempt_budget')
        attempt_text=(f"tries {int(attempts)}/{int(budget)}"
                      if attempts is not None and budget is not None else '')
        no_progress_count=v2.get('child_no_progress_count')
        no_progress_budget=v2.get('child_no_progress_budget')
        no_progress_text=(f"no prog {no_progress_count}/{no_progress_budget}"
                          if no_progress_count is not None and no_progress_budget is not None else '')
        verified=v2.get('last_verified_progress_age_seconds')
        progress_summary=v2.get('last_verified_progress') or {}
        progress_kind=progress_summary.get('kind') if isinstance(progress_summary, dict) else None
        verified_text=(f"verified {str(progress_kind)[:8].upper()} {duration(verified)}"
                       if verified is not None and progress_kind
                       else f"verified {duration(verified)} ago" if verified is not None
                       else 'no verified progress')
        v2_summary=' · '.join(part for part in (
            f"V2 {objective}", no_progress_text, attempt_text, verified_text) if part)
        remaining=v2.get('deadline_remaining_seconds')
        target=v2.get('target_level')
        goal_text=(f"target L{target}" if target is not None else 'target unknown')
        deadline_text=(duration(remaining)+' left' if remaining is not None else 'deadline unknown')
        line0=f"SAGE   {data['state']}  ·  V2 {v2_status}"
        if sage_quest:
            elapsed=sage_quest.get('elapsed_seconds')
            left=sage_quest.get('remaining_seconds')
            line1=f"Sage Quest {duration(elapsed)} · {goal_text} · {duration(left)} left"
            check=sage_quest.get('checklist') or {}
            milestone_labels=(('level_5','L5'),('two_quests','Q2'),('repeated_kill_loot','K/L'),
                              ('sale','Sell'),('purchase','Buy'),('new_spell','Spell'),
                              ('two_different_objectives','Obj2'),('uninterrupted','Auto'))
            line5='Sage Quest '+ ' '.join(
                (('✓' if check.get(key,{}).get('complete') else '○')+label)
                for key,label in milestone_labels)
            active=sage_quest.get('active_objective')
            if active:
                review_text='Objective: '+str(active).replace('_',' ')[:48]
            latest_outcome=sage_quest.get('latest_verified_outcome')
            if latest_outcome:
                data['outcome']=str(latest_outcome)[:120]
        else:
            line1=f"Session {duration(data['elapsed'])} · {goal_text} · deadline {deadline_text}"
            line5=v2_summary
        blocked=v2.get('blocked_reason')
        if blocked:
            review_text='V2 blocked: '+str(blocked)
        receipt=v2.get('last_receipt')
        if receipt:
            if receipt.get('dispatched') is True:
                receipt_text='Input dispatched; effect unverified'
            elif receipt.get('possible_input') is True or receipt.get('dispatch_unknown') is True:
                receipt_text='Input may have occurred; dispatch unknown'
            elif receipt.get('attempted') is False:
                receipt_text='No input attempted'
            else:
                receipt_text='Input status unknown'
            final=f"{run_text} · {receipt_text}"
            input_line='Input: '+receipt_text
            if receipt.get('authorization_type')=='sage_coordinate_navigation':
                action=action_label(receipt.get('chosen_option') or 'unknown')
                prefix='Harness: ' if receipt.get('dispatched') is True else 'Harness attempt: '
                input_line=prefix+action+' · Sage waypoint'
        else:
            final=run_text
            input_line='Input: '+data['input']
    else:
        line0=f"SAGE   {data['state']}  ·  {data['phase']}"
        line1=f"Session {duration(data['elapsed'])}    Decision {duration(data['decision_age'])} ago"
        line5=progress
        input_line='Input: '+data['input']
        if grind:
            line5=data['progress']
            review_text=('Waiting: '+grind['waiting_reason']) if grind.get('waiting_reason') else 'Grind active'
            remaining=grind.get('deadline_remaining_seconds')
            if remaining is not None:run_text='Run '+duration(remaining)+' left'
    if data.get('cast_burst_active') and burst:
        step=int(burst.get('step_index',0))+1
        total=burst.get('requested_count','?')
        phase='checked' if burst.get('phase')=='guard_observed' else 'pulse sent'
        if not v2:
            final=f"{run_text} · Smite {step}/{total} {phase}"
    elif not v2:
        recent=' → '.join(action_label(item['action']) for item in reversed(data['recent'][:3]))
        final=run_text+(' · '+recent if recent else '')
    return [line0,
            line1,
            (f"Thinking… {pending:.1f}s" if pending is not None else 'Choice: '+action_label(
                (sage_quest or {}).get('latest_sage_choice') or data['choice'])),
            input_line,
            'Result: '+data['outcome'],
            line5[:64],
            review_text[:64],
            final[:64] or 'Waiting for live events']
