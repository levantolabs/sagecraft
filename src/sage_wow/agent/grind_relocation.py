"""An accepted hunting relocation survives observation-only travel recovery."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path

from sage_wow.agent.grind_search import target_identity


def record(c, frame, target, request_id, *, selected_task=None):
    h=c.hunt
    from sage_wow.agent.grind_inspection import current_work
    if selected_task:
        if (not isinstance(selected_task,dict) or selected_task.get('kind')!='selected_sensing'
            or not _intact(selected_task.get('source') or {})
            or selected_task.get('decision_receipt')!=c.cycle.last_receipt
            or selected_task.get('request_id')!=request_id
            or selected_task.get('source',{}).get('config_hash')!=config_hash(c)
            or selected_task.get('episode_id')!=(current_work(c) or {}).get('id')):return
        h.strategy_required['selected_exit'] = deepcopy(selected_task)
        c.event('grind_selected_task_disposed', deepcopy(selected_task))
        return
    owner=h.combat_history_key
    history=h.target_history.get(owner) or {}
    # Unknown loss can end the active encounter while retaining its cast
    # constraint. Sage's explicit exit must still own the same selected work;
    # otherwise living-HUD inspection repeatedly preempts destination planning.
    if ((h.encounter_ended and not h.cast_obligation)
            or not target.get('name') or owner!=(h.approach or {}).get('history_key')
            or target_identity(target['name'])!=h.last_target
            or history.get('encounter_id')!=h.encounter or history.get('completed')):
        return
    h.strategy_required['selected_exit']={'request_id':request_id,'owner':owner,
        'encounter_id':h.encounter,'target_continuity':h.target_continuity,
        'session_epoch':c.cycle.session_epoch,'target_name':h.last_target,
        'frame_id':frame.frame_id,'claim':'Sage chose relocation; no movement, escape or new input authority'}


def config_hash(c):
    return hashlib.sha256(json.dumps({'settings':c.config, 'controls':c.profile.values.get('controls'),
        'ui':c.profile.values.get('ui')}, sort_keys=True, default=str).encode()).hexdigest()


def _source(frame, c):
    return {'frame_id':frame.frame_id, 'captured_at':frame.captured_at, 'image_path':frame.image_path,
        'sha256':hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
        'scope':{'session_epoch':c.cycle.session_epoch, 'source':frame.source,
            'width':frame.width, 'height':frame.height}, 'input_generation':c.cycle.input_generation,
        'controller_revision':c.revision, 'config_hash':config_hash(c)}


def _intact(source):
    from sage_wow.agent.grind_cast_feedback import _intact_image
    scope=source.get('scope') or {}
    return _intact_image(source.get('image_path'), source.get('sha256'), (scope.get('width'),scope.get('height')))


def current_target(c, frame, target):
    from sage_wow.agent.grind_resources import hud_resources
    hud=target.get('hud') or {}
    if hud.get('frame_id')!=frame.frame_id:hud=hud_resources(c,frame)
    return {**target,'hud':hud}


def safe_current(c, frame, target, *, pending=None):
    h=c.hunt;hud=current_target(c,frame,target)['hud']
    return bool(c.current() and c.fresh(frame) and not c.cycle._scope_error(frame)
        and not c.require_world and not h.blocked and not h.input_effect_unverified
        and not h.active_threat and not h.disengagement and not c.heal_pending and not h.no_mana
        and not h.loot_request and h.pending is pending
        and hud.get('player_health') is not None and (hud.get('health_confidence') or 0)>=.8
        and hud['player_health']>max(c.config['critical_health_threshold'],c.config['heal_health_fraction'])
        and hud.get('player_mana')!=0)


def capture_selected_task(c, frame, target, pending, result):
    """Capture accepted sensing work before its acquisition is archived unknown."""
    from sage_wow.agent.grind_inspection import current_work
    h=c.hunt;episode=current_work(c);receipt=result.receipt or {}
    target=current_target(c,frame,target)
    from sage_wow.agent.grind_inspection import selected_unavailable, availability
    if (not episode or not selected_unavailable(c,frame,target) or not safe_current(c,frame,target,pending=pending)
        or not h.positive_selection(target) or target.get('invalid_text') or target.get('self_target')
        or result.status!='dispatched' or receipt!=c.cycle.last_receipt
        or not h.known_completed_input({'receipt':receipt}) or receipt.get('possible_input')
        or receipt.get('source_frame_id')!=frame.frame_id
        or (receipt.get('selected_binding') or {}).get('type')!='observe_only'
        or receipt.get('session_epoch')!=c.cycle.session_epoch
        or receipt.get('generation_after')!=c.cycle.input_generation):return None
    # An accepted current combat encounter continues to use its original owner.
    if not h.encounter_ended and h.combat_history_key and not (pending and pending.get('family')=='target'):
        return None
    acquisition=None
    if pending:
        from sage_wow.agent.grind_acquisition import acquisition_proof
        if acquisition_proof(c,frame,pending,h.linked(frame,c.cycle.input_generation)) is None:return None
        acquisition=deepcopy(pending)
    visual=target.get('visual_observation') or {}
    return {'kind':'selected_sensing','request_id':receipt['request_id'], 'decision_receipt':deepcopy(receipt),
        'episode_id':episode['id'], 'episode':deepcopy(episode), 'source':_source(frame,c),
        'sensing_availability':availability(c,frame,target),
        'target_continuity':h.target_continuity, 'selection_revision':h.selection_revision,
        'target':{'name':target.get('name'), 'levels':list(target.get('levels') or []),
            'life_state':visual.get('life_state'), 'target_kind':visual.get('target_kind'),
            'name_box':c.config.get('target_name_box'), 'badge':target.get('badge'), 'box':target.get('box')},
        'acquisition':acquisition, 'disposition':'handed_off',
        'claim':'Accepted selected sensing task exit; suitability/life/damage remain unassessed, no combat owner or progress'}


def selected_disposition(c, frame, target, pending=None):
    """Pure three-state applicability; stale intent grants no movement authority."""
    h=c.hunt;intent=(h.strategy_required or {}).get('selected_exit') or {}
    if intent.get('kind')!='selected_sensing':return None
    reasons=[];source=intent.get('source') or {};scope=source.get('scope') or {}
    prior=intent.get('target') or {};receipt=intent.get('decision_receipt') or {}
    target=current_target(c,frame,target);visual=target.get('visual_observation') or {}
    def answer(status,reason):return {'status':status,'reasons':[reason], 'request_id':intent.get('request_id'),
        'frame_id':frame.frame_id,'input_authority':False,'progress_credit':False}
    if intent.get('superseded'):return answer('superseded','retained_positive_supersession')
    if intent.get('disposition')=='closed':return answer('superseded','selected_task_closed')
    if intent.get('disposition')=='assessment_exhausted':return answer('exhausted','selected_clear_assessment_unknown')
    if (not _intact(source) or not h.known_completed_input({'receipt':receipt})
        or receipt.get('possible_input') or receipt.get('request_id')!=intent.get('request_id')
        or receipt.get('source_frame_id')!=source.get('frame_id')
        or (receipt.get('selected_binding') or {}).get('type')!='observe_only'
        or source.get('config_hash')!=config_hash(c)):
        return answer('temporarily_unproven','source_receipt_or_configuration_unverified')
    if (not c.current() or not c.fresh(frame) or c.cycle._scope_error(frame) or c.require_world
        or scope!={'session_epoch':c.cycle.session_epoch,'source':frame.source,'width':frame.width,'height':frame.height}
        or source.get('controller_revision')!=c.revision):
        return answer('temporarily_unproven','current_scope_or_focus_unverified')
    if pending and pending.get('family')=='target':
        from sage_wow.agent.grind_acquisition import acquisition_proof
        if (acquisition_proof(c,frame,pending,h.linked(frame,c.cycle.input_generation)) is not None
            and (pending.get('receipt') or {}).get('receipt_id')!=((intent.get('acquisition') or {}).get('receipt') or {}).get('receipt_id')
            and h.positive_selection(target)):
            return answer('superseded','new_completed_selected_task')
    for key in ('life_state','target_kind'):
        old=prior.get(key);new=visual.get(key)
        if old and old!='unknown' and new and new!='unknown' and old!=new:
            return answer('superseded','positive_'+key+'_conflict')
    from sage_wow.agent.grind_only import same_patch, same_target_badge
    name_pixels=bool(prior.get('name_box') and same_patch(source['image_path'],frame.image_path,prior['name_box']))
    old_name=target_identity(prior.get('name'));name=target_identity(target.get('name'))
    if old_name and name and old_name!=name:
        return answer('superseded' if not name_pixels else 'temporarily_unproven','positive_identity_changed' if not name_pixels else 'OCR_relabel_without_changed_identity_pixels')
    if prior.get('levels') and target.get('levels') and prior['levels']!=target['levels']:
        return answer('superseded','positive_numeral_conflict')
    if not h.positive_selection(target) or target.get('self_target') or target.get('invalid_text') or target.get('observation_conflict'):
        return answer('temporarily_unproven','current_selected_evidence_unverified')
    if h.target_continuity!=intent.get('target_continuity'):
        return answer('temporarily_unproven','selection_continuity_unverified')
    # Exact dispatch revisions remain strict elsewhere. Numeric OCR fill/drop
    # is neutral for this historical task when calibrated identity is intact.
    if h.selection_revision!=intent.get('selection_revision') and not (
        old_name and name==old_name and not target.get('self_target') and not target.get('invalid_text')
        and len(target.get('levels') or [])<=1 and len(prior.get('levels') or [])<=1):
        return answer('temporarily_unproven','selection_revision_unverified')
    if not name_pixels or not prior.get('badge') or not same_target_badge(source['image_path'],frame.image_path,prior['badge'],c.config.get('target_badge_continuity'),combat_glow=c.config.get('committed_combat',False)):
        return answer('temporarily_unproven','selected_identity_pixels_unverified')
    anchor=intent.get('transport_anchor') or source
    if not _intact(anchor) or anchor.get('scope')!=scope:return answer('temporarily_unproven','transport_anchor_unverified')
    cursor=anchor.get('input_generation');edges=[]
    for move in h.recent_moves:
        if (move.get('input_completed') and move.get('session_epoch')==c.cycle.session_epoch
            and move.get('frame_source')==frame.source and move.get('frame_size')==[frame.width,frame.height]
            and _intact({'image_path':move.get('source_image'),'sha256':move.get('source_hash'),'scope':scope})
            and _intact({'image_path':move.get('after_image'),'sha256':move.get('after_hash'),'scope':scope})):
            edges.append((move.get('generation_before'),move.get('generation_after'),move['receipt_id']))
    transaction=c.observation_transaction or {}
    if (transaction.get('status')=='restored_verified' and not transaction.get('restoration_needed')
        and not transaction.get('chain_invalid') and c.observation.known_chain(transaction)):
        edges.append((transaction.get('source_generation'),transaction.get('final_generation'),transaction.get('transaction_id')))
    for start,end,identity in sorted(edges,key=lambda value:value[0] if isinstance(value[0],int) else -1):
        if start==cursor and isinstance(end,int) and end>start:cursor=end
    if cursor!=c.cycle.input_generation:return answer('temporarily_unproven','unproved_intervening_input')
    return answer('matching','same_disposed_selected_work')


def note_selected_transport(c, frame, target):
    """Bounded provenance anchor only after a contiguous known nonselection chain."""
    status=selected_disposition(c,frame,target,c.hunt.pending)
    intent=(c.hunt.strategy_required or {}).get('selected_exit') or {}
    if not status or status['status']!='matching' or c.hunt.pending:return False
    prior=intent.get('transport_anchor') or intent['source']
    if prior['input_generation']==c.cycle.input_generation:return False
    intent['transport_anchor']={**_source(frame,c),'previous_generation':prior['input_generation'],
        'latest_movement_receipt':(c.hunt.last_completed_action or {}).get('receipt_id'),
        'observation_transaction_id':(c.observation_transaction or {}).get('transaction_id'),
        'claim':'Contiguous completed nonselection input; identity remains guarded, no progress or allowance'}
    return True


def typed_release(h):
    """Verified current HUD release, deliberately unrelated to a combat owner."""
    intent=(h.strategy_required or {}).get('selected_exit') or {}
    clear=intent.get('clear') or {};proof=intent.get('assessment') or {}
    marker=clear.get('selected_task_abandon') or {};receipt=clear.get('receipt') or {}
    assessment_receipt=proof.get('receipt') or {};scope=proof.get('scope') or {}
    if (intent.get('kind')!='selected_sensing' or intent.get('disposition')!='closed'
        or not isinstance(marker,dict) or marker.get('request_id')!=intent.get('request_id')
        or marker.get('episode_id')!=intent.get('episode_id') or not _intact(intent.get('source') or {})
        or not h.known_completed_input(clear) or not receipt.get('possible_input')
        or receipt.get('source_frame_id')!=clear.get('source_frame_id')
        or not _intact({'image_path':clear.get('source_image'),'sha256':clear.get('source_hash'),
            'scope':clear.get('source_scope')}) or not _intact(proof)
        or scope!=clear.get('source_scope') or receipt.get('session_epoch')!=scope.get('session_epoch')
        or proof.get('input_generation')!=receipt.get('generation_after')
        or not h.known_completed_input({'receipt':assessment_receipt}) or assessment_receipt.get('possible_input')
        or assessment_receipt.get('source_frame_id')!=proof.get('frame_id')
        or assessment_receipt.get('session_epoch')!=scope.get('session_epoch')
        or assessment_receipt.get('generation_before')!=assessment_receipt.get('generation_after')
        or assessment_receipt.get('generation_after')!=proof.get('input_generation')
        or (assessment_receipt.get('selected_binding') or {}).get('type')!='observe_only'):return None
    try:
        if not datetime.fromisoformat(receipt['occurred_at'])<datetime.fromisoformat(proof['captured_at'])<=datetime.fromisoformat(assessment_receipt['occurred_at']):return None
    except (KeyError,ValueError,TypeError):return None
    return {'kind':'selected_sensing','disposition':'closed','clear':clear,
        'assessment':proof,'request_id':intent['request_id'], 'own_state_transport':intent.get('own_state_transport')}


def _absence(target):
    hud=target.get('hud') or {}
    return not (target.get('self_target') or target.get('invalid_text') or target.get('observation_conflict')
        or target.get('name') or target.get('levels') or (target.get('visual_observation') or {}).get('selected_hud')=='present'
        or (target.get('visual_observation') or {}).get('name')
        or (target.get('visual_observation') or {}).get('level') is not None
        or hud.get('target_health') is not None and (hud.get('target_health_confidence') or 0)>=.8)


def start_clear_assessment(c, request, request_id):
    state=request['state']
    if (not request['valid']() or len(state['request_ids'])>=2 or request_id in state['request_ids']
        or state.get('active_request')):
        raise ValueError('Selected clear assessment is stale or spent')
    state['request_ids'].append(request_id);state['active_request']=request_id
    c.event('grind_selected_clear_assessment_admitted',{'clear_receipt_id':state['clear_receipt_id'],
        'request_id':request_id,'admitted_count':len(state['request_ids']),'limit':2})


def release_clear_unknown(c, pending, intent, frame, target, reason):
    if c.hunt.pending is not pending:return
    c.hunt.resolve('unknown',frame,c.hunt.last_measurement,pending=pending,linked=True)
    intent.update(disposition='assessment_exhausted',clear=deepcopy(pending),
        clear_exhausted_source=_source(frame,c),clear_exhausted_continuity=c.hunt.target_continuity)
    c.event('grind_selected_clear_assessment_exhausted',{'clear_receipt_id':pending['receipt']['receipt_id'],
        'reason':reason,'absence_proved':False,'allowance_renewed':False})
    handoff_unknown(c,frame,target)


def terminate_clear_assessment(c, pending, intent, frame, target, reason):
    """Dispose the original question even when its response lost authority."""
    owner=intent.get('clear_assessment_owner')
    state=(intent.get('clear_assessments') or {}).get(owner) or {}
    original=state.get('pending_snapshot')
    if (original is not None and c.hunt.pending is pending and pending==original):
        release_clear_unknown(c,pending,intent,frame,target,reason)
    elif original is not None:
        # An in-place mutation/replacement must not inherit this receipt's
        # assessment allowance or be consumed/charged as the original clear.
        intent.update(disposition='assessment_exhausted',clear=deepcopy(original),
            clear_assessment_invalidated=reason)
        c.event('grind_selected_clear_assessment_invalidated',{'clear_receipt_id':owner,
            'reason':reason,'replacement_pending_consumed':False,'absence_proved':False})
    return original is not None


def finish_clear_assessment(c, request, pending, intent, result, frame, target):
    state=request['state'];request_id=state.pop('active_request',None)
    if not request_id:return
    valid=request['valid']()
    actual=bool(valid and result is not None and result.decision is not None
        and result.decision.envelope.request_id==request_id)
    state['previous_unclear']=bool(actual and (result.no_input_abstention or
        result.status=='dispatched' and result.decision.chosen=='cannot_assess'))
    state['last_outcome']=getattr(result,'status','interrupted')
    if (valid and len(state['request_ids'])>=2 and c.hunt.pending is pending
        and (c.hunt.strategy_required or {}).get('selected_exit') is intent):
        release_clear_unknown(c,pending,intent,frame,target,'two_admitted_assessments_unresolved')


def handoff_unknown(c, frame, target):
    """Yield an exhausted factual question; existing action guards still decide."""
    h=c.hunt
    if not safe_current(c,frame,target) or not _absence(current_target(c,frame,target)):return False
    if not h.encounter_ended or h.cast_blocks_acquisition():return False
    state=h.travel_policy.get('recovery_review')
    if state:state['selection_assessment']=False
    # Yielding an unresolved sensing question must preserve Sage's accepted
    # request to choose another destination on the next fresh frame.
    if h.planning_requested and h.phase=='choose_area':
        h.compact_stage='planning'
    else:
        h.phase='travel' if (h.plan or {}).get('phase')=='travel' else 'search'
        h.compact_stage='acquire';h.planning_requested=False
    c.event('grind_sensing_unknown_handoff',{'frame_id':frame.frame_id,
        'absence_proved':False,'allowance_renewed':False,'progress_credit':False})
    return True


def close_retained_clear(c, frame, target, result):
    intent=(c.hunt.strategy_required or {}).get('selected_exit') or {}
    pending=intent.get('clear');source=intent.get('clear_exhausted_source') or {}
    if (intent.get('disposition')!='assessment_exhausted' or not pending or c.hunt.pending is not None
        or source.get('input_generation')!=c.cycle.input_generation
        or source.get('config_hash')!=config_hash(c) or not _intact(source)
        or intent.get('clear_exhausted_continuity')!=c.hunt.target_continuity):return False
    return close_selected_task(c,frame,target,pending,result,retained=True)


def close_selected_task(c, frame, target, pending, result, *, retained=False):
    """Consume the marked clear once without touching historical combat ownership."""
    h=c.hunt;intent=(h.strategy_required or {}).get('selected_exit') or {}
    marker=(pending or {}).get('selected_task_abandon');receipt=result.receipt or {}
    physical=(pending or {}).get('receipt') or {};scope=(pending or {}).get('source_scope') or {}
    operation=(intent.get('clear_assessments') or {}).get(physical.get('receipt_id')) or {}
    target=current_target(c,frame,target)
    valid=bool(isinstance(marker,dict) and intent.get('kind')=='selected_sensing'
        and intent.get('disposition')==('assessment_exhausted' if retained else 'handed_off') and marker.get('request_id')==intent.get('request_id')
        and operation.get('clear_receipt_id')==physical.get('receipt_id')
        and (retained or operation.get('active_request')==receipt.get('request_id'))
        and marker.get('episode_id')==intent.get('episode_id') and marker.get('config_hash')==config_hash(c)
        and marker.get('plan_request_id')==(h.plan or {}).get('request_id')
        and _intact(intent.get('source') or {}) and (h.pending is None if retained else h.pending is pending) and pending.get('family')=='clear'
        and pending.get('purpose')=='selected_task_abandon' and h.known_completed_input(pending)
        and physical.get('possible_input') and physical.get('dispatched')
        and physical.get('source_frame_id')==pending.get('source_frame_id')
        and _intact({'image_path':pending.get('source_image'),'sha256':pending.get('source_hash'),'scope':scope})
        and scope=={'session_epoch':c.cycle.session_epoch,'source':frame.source,'width':frame.width,'height':frame.height}
        and physical.get('generation_after')==c.cycle.input_generation
        and (retained or h.linked(frame,c.cycle.input_generation)) and safe_current(c,frame,target,pending=None if retained else pending)
        and result.status=='dispatched' and receipt==c.cycle.last_receipt
        and h.known_completed_input({'receipt':receipt}) and not receipt.get('possible_input')
        and (receipt.get('selected_binding') or {}).get('type')=='observe_only'
        and receipt.get('source_frame_id')==frame.frame_id and receipt.get('session_epoch')==c.cycle.session_epoch
        and receipt.get('generation_after')==physical.get('generation_after')==c.cycle.input_generation)
    choice=getattr(result.decision,'chosen',None)
    verified=valid and choice in ({'no_selected_frame'} if retained else {'target_cleared'}) and _absence(target)
    if verified:
        try:verified=datetime.fromisoformat(frame.captured_at)>datetime.fromisoformat(physical['occurred_at'])
        except (KeyError,ValueError,TypeError):verified=False
    terminal=verified or valid and choice=='clear_failed'
    if terminal and not retained:
        h.resolve('target_cleared' if verified else 'clear_failed',
            frame,c.hunt.last_measurement,pending=pending,linked=True)
    if verified:
        intent.update(disposition='closed',clear=deepcopy(pending),assessment={**_source(frame,c),'receipt':deepcopy(receipt)})
        if not h.continuity_absent:h.target_continuity+=1
        empty=(target.get('name'),tuple(target.get('levels') or []),target.get('self_target'),target.get('invalid_text'))
        if h.selection!=empty:h.selection_revision+=1
        h.selection=empty;h.continuity_absent=True
        h.selected_presence=False;h.attempt_absence=True
        h.phase='travel' if (h.plan or {}).get('phase')=='travel' else 'search'
        h.compact_stage='acquire'
        c.target_observation=None
        from sage_wow.agent.grind_inspection import complete_clear_absence
        complete_clear_absence(c,frame,target,pending,result)
        c.event('grind_selected_task_closed', {'request_id':intent['request_id'],'clear_receipt_id':physical['receipt_id'],
            'frame_id':frame.frame_id,'combat_owner_unchanged':True,'progress_credit':False,'allowance_renewed':False})
    else:
        c.event('grind_selected_task_clear_unverified',{'frame_id':frame.frame_id,'choice':choice,'valid':valid,
            'prior_owner_unchanged':True,'positive_credit':False})
    return verified


async def decide_selected_exit(c, frame, target, measurement, *, exhausted=False):
    from sage_wow.agent.grind_clear import DESCRIPTION
    """A finite existing-action question for disposed selected sensing work."""
    from sage_wow.agent.cycle import ActionCandidate, CycleResult, DispatchValidation
    from sage_wow.agent.grind_only import composed_evidence_image
    h=c.hunt;pending=h.pending;intent=(h.strategy_required or {}).get('selected_exit') or {}
    marked=pending is not None and 'selected_task_abandon' in pending
    status=selected_disposition(c,frame,target,pending)
    if marked:
        owner=intent.get('clear_assessment_owner')
        if owner is None:
            owner=pending['receipt']['receipt_id'];intent['clear_assessment_owner']=owner
            intent.setdefault('clear_assessments',{})[owner]={'clear_receipt_id':owner,
                'pending_snapshot':deepcopy(pending),'request_ids':[],'previous_unclear':False}
        state=intent['clear_assessments'][owner]
        if pending!=state['pending_snapshot']:
            terminate_clear_assessment(c,pending,intent,frame,target,'original_clear_pending_changed')
            from sage_wow.agent.grind_navigation_recovery import wait
            return wait(c,'selected_clear_owner_changed',frame,target)
    if not marked:
        if h.phase=='choose_area' and h.planning_requested:return None
        if status and status['status']=='exhausted':return None
        if not status or status['status']=='superseded':
            if status and intent.get('disposition')!='closed':
                intent['superseded']=deepcopy(status)
                h.phase='search';h.compact_stage='inspect';h.planning_requested=False
                return CycleResult('grind_reobserve',detail='Fresh selected work superseded the disposed task; no travel input')
            return None
        if status['status']=='matching' and not exhausted:return None
        if not safe_current(c,frame,target) or not h.positive_selection(current_target(c,frame,target)):return None
    elif not safe_current(c,frame,target,pending=pending):
        terminate_clear_assessment(c,pending,intent,frame,target,'selected_task_clear_current_authority_unverified');c.flush_outcomes()
        return CycleResult('grind_reobserve',detail='Typed clear remains unverified; old combat debt preserved')
    if marked:
        if state['clear_receipt_id']!=pending['receipt']['receipt_id'] or len(state['request_ids'])>=2:
            terminate_clear_assessment(c,pending,intent,frame,target,'selected_clear_assessment_no_remaining_authority');c.flush_outcomes()
            return CycleResult('grind_reobserve',detail='Selected clear assessment authority unavailable')
    source=_source(frame,c);authority=(c.revision,c.cycle.session_epoch,c.cycle.input_generation)
    pending_snapshot=deepcopy(pending)
    plan=deepcopy(h.plan)
    intent_basis=lambda:{key:value for key,value in intent.items() if key!='clear_assessments'}
    intent_snapshot=deepcopy(intent_basis())
    def valid():
        return bool(h.pending is pending and authority==(c.revision,c.cycle.session_epoch,c.cycle.input_generation)
            and pending==pending_snapshot
            and config_hash(c)==source['config_hash'] and _intact(source)
            and h.plan==plan and (h.strategy_required or {}).get('selected_exit') is intent and intent_basis()==intent_snapshot
            and safe_current(c,frame,target,pending=pending))
    identity=str(('selected_task_exit',intent.get('request_id'),intent.get('episode_id')))
    if not c.select_question(identity,'feedback'):return CycleResult('grind_blocked',detail='Selected-task question history capacity unavailable')
    if not marked and h.unclear>=2:
        from sage_wow.agent.grind_navigation_recovery import wait
        return wait(c,'selected_task_exit_unselected',frame,target)
    image=await c.prepare_image(frame,composed_evidence_image,frame,target['box'],
        Path(frame.image_path).with_name('grind-view-selected-exit-'+frame.frame_id+'.png'),player_badge=tuple(c.config['player_level_box']))
    if isinstance(image,CycleResult):return image
    options=[];metadata={}
    def observe(name,text,**meta):
        options.append(ActionCandidate(name,text,{'type':'observe_only'},precondition=valid));metadata[name]=meta
    if marked:
        observe('target_cleared','No selected target portrait with an attached health bar is visible now. Background creatures, names and tooltips are not a selected target HUD.')
        observe('clear_failed','A selected target portrait with an attached health bar is visible now, even if its name or level is unreadable.')
    else:
        from sage_wow.agent.grind_navigation_recovery import goal_capacity_exhausted
        ordinary_exhausted=bool(exhausted or (h.travel_policy.get('retry_menu') or {}).get('exhausted') or goal_capacity_exhausted(c))
        controls=c.profile.values['controls']['bindings'];binding=controls.get('escape') or {}
        if binding.get('verified_from') and type(binding.get('keycode')) is int and h.allowed('reject','clear'):
            original=await c.clear_guard(frame,target)
            async def guard(binding):
                checked=await original(binding)
                if not checked.approved:return checked
                actual=await c.target_proposal(checked.dispatch_frame)
                approved=valid() and safe_current(c,checked.dispatch_frame,actual) and h.positive_selection(current_target(c,checked.dispatch_frame,actual))
                return DispatchValidation(approved,checked.dispatch_frame if approved else None,
                    'Current selected sensing clear only; no historical combat owner',checked.evidence)
            options.append(ActionCandidate('reject_selected_target',
                'Drop this CURRENT unassessed selection with the existing clear action, then verify absence. Its numeral, suitability, life and previous damage remain unknown. Preserve the destination and all prior failed methods.'+DESCRIPTION,
                {'type':'keypress','keycode':binding['keycode'],'hold_seconds':.08},precondition=lambda:valid() and h.allowed('reject','clear'),dispatch_guard=guard))
            metadata['reject_selected_target']={'action':'reject','family':'clear','purpose':'selected_task_abandon',
                'selected_task_abandon':{'request_id':intent['request_id'],'episode_id':intent['episode_id'],
                    'plan_request_id':(h.plan or {}).get('request_id'),'config_hash':source['config_hash']}}
        if (not ordinary_exhausted and status['status']=='temporarily_unproven'
            and status['reasons']!=['source_receipt_or_configuration_unverified'] and h.plan):
            original=await c.clear_guard(frame,target)
            async def resume_guard(binding):
                checked=await original(binding)
                if not checked.approved:return checked
                actual=await c.target_proposal(checked.dispatch_frame)
                approved=valid() and safe_current(c,checked.dispatch_frame,actual) and h.positive_selection(current_target(c,checked.dispatch_frame,actual))
                return DispatchValidation(approved,checked.dispatch_frame if approved else None,'Fresh explicit selected-work handoff to retained destination',checked.evidence)
            options.append(ActionCandidate('resume_destination',
                'Deliberately hand off THIS current unassessed selected work and resume the retained destination. This is fresh intent, not proof of physical identity across focus/input gaps, movement or a new allowance.',
                {'type':'observe_only'},precondition=valid,dispatch_guard=resume_guard));metadata['resume_destination']={'resume_destination':True}
        if not options:
            Path(image).unlink(missing_ok=True)
            from sage_wow.agent.grind_navigation_recovery import wait
            return wait(c,'selected_task_clear_exhausted',frame,target)
    observe('ui_blocked','An actual blocking UI requires its ordinary current reconciliation.',ui=True)
    observe('recover_now','Actual threat or resource emergency requires ordinary recovery.',recover=True)
    if marked:observe('cannot_assess','The current image does not show whether a selected target portrait and attached health bar are present.',unclear=True)
    c.event('grind_selected_task_exit_question',{'frame_id':frame.frame_id,'applicability':status,
        'exhausted_travel':exhausted,'linked_clear_assessment':marked,'input_authority':False})
    destination=(h.plan or {}).get('area',{}).get('label','the chosen hunting destination')
    context=('World of Warcraft. Inspect the CURRENT selected-target HUD region. ' if marked else
        f'World of Warcraft. We previously chose to travel to {destination}. '
        + (
           'The selected creature is still on the HUD after target inspection and travel choices ran out. Choose one offered action to leave this selection and continue the hunt. ' if exhausted else
           'Current evidence cannot connect the selected HUD to the earlier travel choice. Choose clear, or deliberately resume that destination for this current selection when offered. '))
    request={'state':state,'valid':valid} if marked else None
    result=None
    try:
        result=await c.decide(frame,image,context,
            'Is a selected target portrait with an attached health bar visible now?' if marked else
            'Clear drops the selected HUD and requires a fresh result check; it does not mean the creature is dead or unsuitable. Resume only the offered current task. Prior failed actions retain their limits.',
            options,travel_budget=c.travel_budget,compact=True,selected_clear=request)
    except BaseException:
        if marked:finish_clear_assessment(c,request,pending,intent,None,frame,target)
        raise
    receipt=result.receipt or {};choice=getattr(result.decision,'chosen',None);data=metadata.get(choice,{})
    accepted=result.status=='dispatched' and receipt==c.cycle.last_receipt and h.known_completed_input({'receipt':receipt})
    if (receipt.get('possible_input') or receipt.get('dispatch_unknown')) and not accepted:c.stop('partial_or_unknown_grind_input')
    elif accepted and (not marked or valid()):
        if marked:
            close_selected_task(c,frame,target,pending,result)
            if data.get('ui') or data.get('recover'):
                release_clear_unknown(c,pending,intent,frame,target,'accepted_stronger_task')
                await c.apply_choice(frame,target,measurement,None,False,result,data,choice,c.level.last_confirmed_level)
        elif choice=='resume_destination':
            intent.setdefault('original_handoff',{'request_id':intent['request_id'],'source':deepcopy(intent['source']),
                'decision_receipt':deepcopy(intent['decision_receipt'])})
            intent.update(request_id=receipt['request_id'],source=_source(frame,c),decision_receipt=deepcopy(receipt),
                target_continuity=h.target_continuity,selection_revision=h.selection_revision)
            actual=current_target(c,frame,target);visual=actual.get('visual_observation') or {}
            intent['target']={'name':actual.get('name'),'levels':list(actual.get('levels') or []),
                'life_state':visual.get('life_state'),'target_kind':visual.get('target_kind'),
                'name_box':c.config.get('target_name_box'),'badge':actual.get('badge'),'box':actual.get('box')}
            intent.pop('transport_anchor',None)
            h.plan['relocation_request_id']=intent['request_id']
            await c.apply_choice(frame,target,measurement,None,False,result,data,choice,c.level.last_confirmed_level)
            c.event('grind_selected_task_fresh_resume',{'request_id':intent['request_id'],'frame_id':frame.frame_id,
                'physical_identity_claimed':False,'allowance_renewed':False})
        else:
            await c.apply_choice(frame,target,measurement,None,False,result,data,choice,c.level.last_confirmed_level)
            if h.pending and h.pending.get('selected_task_abandon') and h.pending.get('receipt')==receipt:
                # Only a new accepted physical clear can replace this owner.
                intent.pop('clear_assessment_owner',None)
    # This operation owns unresolved clear feedback. Generic uncertainty
    # recovery must not archive its receipt before the finite owner does.
    if marked:
        if result.no_input_abstention or accepted and data.get('unclear'):
            h.question_answer(unclear=True);c.null_answers=h.unclear;c.wait_until=__import__('time').time()+2
        elif accepted:h.question_answer()
        finish_clear_assessment(c,request,pending,intent,result,frame,target)
    else:c.finish_question(result,data)
    c.flush_outcomes()
    c.store.save_checkpoint('grind_only',{'hunt':h.context(),'deadline':c.deadline,'baseline_verified':c.baseline,'level':c.level.last_confirmed_level})
    return result


def resume(c, frame, target, pending, phase):
    h=c.hunt
    intent=(h.strategy_required or {}).get('selected_exit') or {}
    if intent.get('kind')=='selected_sensing':
        state=selected_disposition(c,frame,target,pending)
        reason=(h.recovery_requested or {}).get('latest_reason',(h.recovery_requested or {}).get('reason'))
        if (phase!='travel' or h.phase!='travel' or h.compact_stage!='recovery'
            or reason not in {'repeated_null_provider_answer','repeated_uncertain_answer'}
            or state['status']!='matching' or not safe_current(c,frame,target)
            or (h.plan or {}).get('relocation_request_id')!=intent['request_id']):return False
        h.compact_stage='acquire'
        c.event('grind_selected_task_travel_resumed',{**state,'reason':reason})
        return True
    history=h.target_history.get(intent.get('owner')) or {}
    visual=target.get('visual_observation') or {};hud=target.get('hud') or {}
    reason=(h.recovery_requested or {}).get('latest_reason',(h.recovery_requested or {}).get('reason'))
    if (phase!='travel' or h.phase!='travel' or h.compact_stage!='recovery'
            or reason not in {'repeated_null_provider_answer','repeated_uncertain_answer'}
            or not intent or (h.plan or {}).get('relocation_request_id')!=intent['request_id']
            or intent['session_epoch']!=c.cycle.session_epoch
            or intent['owner']!=h.combat_history_key or intent['owner']!=(h.approach or {}).get('history_key')
            or intent['encounter_id']!=h.encounter or intent['target_continuity']!=h.target_continuity
            or history.get('completed') or history.get('retired_by_acquisition')
            or target_identity(target.get('name'))!=intent['target_name']
            or target.get('self_target') or target.get('invalid_text')
            or visual.get('selected_hud')=='absent' or visual.get('life_state')=='dead'
            or visual.get('target_kind') in {'player','friendly_or_self'}
            or not c.fresh(frame) or c.cycle._scope_error(frame)
            or h.blocked or h.input_effect_unverified or h.active_threat or c.heal_pending or h.no_mana
            or h.loot_request or pending is not None
            or hud.get('frame_id')!=frame.frame_id or hud.get('player_health') is None
            or hud['player_health']<=max(c.config['critical_health_threshold'],c.config['heal_health_fraction'])
            or (hud.get('health_confidence') or 0)<.8):
        return False
    # Recovery evidence and all failed methods remain. This restores a question
    # already chosen by Sage; the ordinary travel menu/dispatch guard owns input.
    h.compact_stage='acquire'
    c.event('grind_relocation_resumed',{'frame_id':frame.frame_id,'request_id':intent['request_id'],
        'owner':intent['owner'],'reason':reason,'input_authority':False,'progress_credit':False})
    return True


def exhausted_exit(c, frame, target, pending, stage, *, mixed=None, probe=None):
    h=c.hunt;history=h.target_history.get(h.combat_history_key) or {}
    visual=target.get('visual_observation') or {};hud=target.get('hud') or {}
    return bool(c.config.get('committed_combat',False) and stage in {'inspect','reinspect'}
        and pending is None and h.pending is None and not mixed and not probe
        and c.fresh(frame) and not c.cycle._scope_error(frame) and not h.blocked
        and not h.input_effect_unverified and not h.active_threat and not c.heal_pending
        and not h.no_mana and not h.loot_request and not h.disengagement
        and not h.encounter_ended and not h.target_dead_observed
        and not (h.attempt_absence and h.attempt_changed_view)
        and history.get('key')==(h.approach or {}).get('history_key')
        and history.get('encounter_id')==h.encounter and not history.get('completed')
        and not history.get('retired_by_acquisition') and history.get('correction_rounds',0)>=3
        and target.get('name') and target_identity(target['name'])==h.last_target
        and not target.get('self_target') and not target.get('invalid_text')
        and visual.get('selected_hud')=='present' and visual.get('life_state')=='alive'
        and target.get('eligibility')=='eligible'
        and hud.get('frame_id')==frame.frame_id and hud.get('player_health') is not None
        and hud['player_health']>c.config['critical_health_threshold'] and (hud.get('health_confidence') or 0)>=.8)


def current_combat_exit(c, frame, target, pending):
    """An accepted relocation still owns this unresolved selected encounter."""
    h=c.hunt;intent=(h.strategy_required or {}).get('selected_exit') or {}
    history=h.target_history.get(intent.get('owner')) or {}
    return bool(intent and intent.get('kind')!='selected_sensing'
        and pending is None and safe_current(c,frame,target)
        and intent.get('session_epoch')==c.cycle.session_epoch
        and intent.get('owner')==h.combat_history_key
        and intent.get('owner')==(h.approach or {}).get('history_key')
        and intent.get('encounter_id')==h.encounter==history.get('encounter_id')
        and intent.get('target_continuity')==h.target_continuity
        and not history.get('completed') and not history.get('retired_by_acquisition')
        and target.get('name') and target_identity(target['name'])==intent.get('target_name')
        and not target.get('self_target') and not target.get('invalid_text')
        and not target.get('observation_conflict') and h.positive_selection(target))


def exit_context(c, target):
    history=c.hunt.target_history[c.hunt.combat_history_key]
    return (f'World of Warcraft. The current selected creature {target["name"]} is alive and eligible. '
        f'This local encounter has reached its limit of {history["correction_rounds"]} correction rounds without resolving the fight. '
        'Further casts and cast corrections are unavailable for this attempt. '
        'Choose clear to drop this selection and hunt another creature, or choose relocation to a hunting destination. '
        'Clearing an unresolved approach does not mean this creature is dead or ineligible. '
        'The harness verifies the clear or observes the chosen route before further gameplay. '
        'Prior failed actions and unknown combat effects remain recorded. Use the CURRENT scene for the offered action.')


def travel_context(c):
    h=c.hunt;intent=(h.strategy_required or {}).get('selected_exit') or {}
    if intent.get('kind')=='selected_sensing':
        return ('We chose to continue to the hunting destination while this selected creature remains on the HUD. '
            'Choose an offered movement using the current world scene. Its unreadable numeral and life remain unassessed; '
            'prior failed actions retain their limits.' if intent_current(c) else '')
    if (not intent or (h.plan or {}).get('relocation_request_id')!=intent['request_id']
            or intent['session_epoch']!=c.cycle.session_epoch or intent['owner']!=h.combat_history_key
            or intent['encounter_id']!=h.encounter or intent['target_continuity']!=h.target_continuity):return ''
    return ('Accepted task: You chose to leave the unresolved encounter and travel to this hunting destination. '
        'The old selected creature HUD may remain visible. That alone does not reopen its fight. '
        'Continue the chosen relocation using one offered movement on visible safe ground, '
        'or choose another destination. A fresh actual attacker or survival emergency still interrupts travel. '
        'An unreadable forward bearing does not prevent choosing an offered turn or local detour; '
        'the harness measures the result. No movement direction or progress is assumed.')


def intent_current(c):
    h=c.hunt;intent=(h.strategy_required or {}).get('selected_exit') or {}
    if intent.get('kind')=='selected_sensing':
        return bool(not intent.get('superseded') and intent.get('disposition')=='handed_off'
            and _intact(intent.get('source') or {}) and intent['source'].get('config_hash')==config_hash(c)
            and intent['source']['scope'].get('session_epoch')==c.cycle.session_epoch
            and intent.get('target_continuity')==h.target_continuity)
    return bool(intent and intent['session_epoch']==c.cycle.session_epoch
        and intent['owner']==h.combat_history_key and intent['owner']==(h.approach or {}).get('history_key')
        and intent['encounter_id']==h.encounter and intent['target_continuity']==h.target_continuity)


def recovery_context(c, geometry, *, ordinary_retry=False):
    h=c.hunt
    if not ordinary_retry:
        if not intent_current(c) or c.null_answers<2:return None
        intent=h.strategy_required['selected_exit']
        if (h.plan or {}).get('relocation_request_id')!=intent['request_id']:return None
    lines=['World of Warcraft. Current task: Reach the selected hunting area.',
        'Continue the hunting destination you already chose.' if ordinary_retry else
        'You chose to leave the unresolved selected encounter. Continue that relocation.',
        'Choose one offered turn or movement using the CURRENT world scene. A short backward or sideways step can create room; a turn changes the view.']
    if not ordinary_retry:
        lines.append('The selected portrait is the old encounter, not a direction marker. A travel move does not require that creature to be in front of us.')
    if geometry.get('position'):lines.append(f'Current map position: {geometry["position"]}.')
    if geometry.get('destination'):lines.append(f'Chosen map destination: {geometry["destination"]}.')
    last=h.last_completed_action
    if last:lines.append(f'Last completed movement: {last["action"]}; observed result: {last.get("status","unassessed")}; position {last.get("position_before")} to {last.get("position_after")}.')
    lines.append('Use visible safe ground. Choose a different approach when the last movement did not make room. Movement and facing will be observed afterward; no direction needs to be guessed from the portrait. A current attacker, low health or blocking dialog requires recovery.')
    return ' '.join(lines)
