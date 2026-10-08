import json
import time
from datetime import datetime,timezone
import pytest
from sage_wow.dashboard.telemetry import action_label,live_snapshot,display_lines
from sage_wow.models import Event
from sage_wow.storage import EventStore


def test_overlay_distinguishes_choice_from_actual_input(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3')
    store.append(Event.create('session_started',{}))
    store.append(Event.create('sage_decision',{'chosen':'loot_remaining','request_duration_ms':1000}))
    store.append(Event.create('action_dispatched',{'chosen':'loot_remaining','execution':{'dispatched':False}}))
    (tmp_path/'status.json').write_text(json.dumps({'state':'PLAYING'}))
    data=live_snapshot(tmp_path)
    assert data['choice']=='loot_remaining'
    assert data['input']=='Observation / no input'
    assert data['recent']==[]
    assert len(display_lines(data))==8
    store.close()


def test_stopped_overlay_does_not_show_endless_thinking(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3')
    store.append(Event.create('sage_request_started',{'request_id':'pending'}))
    (tmp_path/'status.json').write_text(json.dumps({'state':'STOPPED'}))
    assert live_snapshot(tmp_path)['request_age'] is None
    store.close()


def test_dashboard_uses_committed_readiness_instead_of_old_counter(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3')
    store.append(Event.create('quest_progress_observed',{'count':7,'total':8,'item':'Meat'}))
    store.save_checkpoint('tactical',{'phase':'RETURN_QUEST',
        'progress':{'count':8,'total':8,'item':'Meat','status':'ready_for_turn_in'}})
    snapshot=live_snapshot(tmp_path)
    assert snapshot['progress']=='8/8 Meat · ready for turn-in'
    assert snapshot['phase']=='RETURN_QUEST'
    store.close()


def test_overlay_exposes_self_review_schedule(tmp_path):
    data=tmp_path/'realm'/'character'
    data.mkdir(parents=True)
    now=time.time()
    store=EventStore(data/'events.sqlite3')
    store.save_checkpoint('tactical',{'phase':'COMBAT','last_self_review_at':now-17})
    snapshot=live_snapshot(data,now=now)
    assert snapshot['next_self_review_seconds']==43
    assert snapshot['last_self_review_age']==17
    lines=display_lines(snapshot)
    assert len(lines)==8
    assert 'Sage check' in lines[6] and 'last 00:17 ago' in lines[6]
    store.close()


def test_overlay_shows_active_burst_step_then_clears_it_after_terminal_event(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3')
    old=time.time()-2
    def add(kind,payload,at):
        store.connection.execute(
            'INSERT INTO events(event_id,occurred_at,event_type,payload_json) VALUES(?,?,?,?)',
            (f'{kind}-{at}',datetime.fromtimestamp(at,timezone.utc).isoformat(),kind,json.dumps(payload)))
        store.connection.commit()
    add('cast_burst_step',{'kind':'cast_burst','phase':'pulse_dispatched','step_index':1,
                           'requested_count':3,'spell':'Smite','expected_target_name':'Young Boar',
                           'evidence':{'dispatched':True}},old)
    (tmp_path/'status.json').write_text(json.dumps({'state':'PLAYING'}))
    active=live_snapshot(tmp_path,now=time.time())
    assert active['cast_burst_active']
    assert active['latest_cast_burst_step']['step_index']==1
    assert 'Smite 2/3' in display_lines(active)[7]
    add('action_dispatched',{'chosen':'smite_burst_3','execution':{
        'kind':'cast_burst','completed':True,'completed_steps':[{}, {}, {}]}},time.time()-1)
    done=live_snapshot(tmp_path,now=time.time())
    assert not done['cast_burst_active']
    assert 'Smite 2/3' not in display_lines(done)[7]
    store.close()


def test_completed_old_burst_is_not_displayed_as_current_casting(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3')
    at=time.time()-30
    store.append(Event.create('cast_burst_step',{'kind':'cast_burst','phase':'pulse_dispatched',
        'step_index':2,'requested_count':3,'spell':'Smite','expected_target_name':'Young Boar'}))
    (tmp_path/'status.json').write_text(json.dumps({'state':'PAUSED'}))
    snapshot=live_snapshot(tmp_path,now=at+31)
    assert not snapshot['cast_burst_active']
    assert 'Smite 3/3' not in display_lines(snapshot)[7]
    store.close()


def test_absent_self_review_data_stays_unknown(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3')
    snapshot=live_snapshot(tmp_path)
    assert snapshot['run_remaining_seconds'] is None
    assert snapshot['next_self_review_seconds'] is None
    assert 'Sage check —' in display_lines(snapshot)[6]
    store.close()


def test_current_v2_status_projects_goal_receipt_and_verified_progress_without_claiming_effect(tmp_path):
    now=time.time()
    store=EventStore(tmp_path/'events.sqlite3')
    store.append(Event.create('session_started',{}))
    store.append(Event.create('v2_controller_status',{
        'controller':'v2','status':'NO_PROGRESS','session_id':'goal-1','session_epoch':'epoch-4',
        'target_level':5,'absolute_deadline':datetime.fromtimestamp(now+420,timezone.utc).isoformat(),
        'task_id':'v2-session:12345678-1234-1234-1234-123456789abc',
        'objective':'grind_nearby','child_task_id':'wolf-kill:12345678-1234-1234-1234-123456789abc',
        'child_phase':'combat',
        'child_attempts_used':2,'child_attempt_budget':5,'next_parent_review_at':None,
        'child_no_progress_count':2,'child_no_progress_budget':3,
        'last_verified_progress_age_seconds':35,'blocked_reason':'No verified target damage yet.',
        'last_verified_progress':{'kind':'xp','task_id':'child-1','observed_at':'2026-09-27T00:00:00Z',
            'progress_id':'xp-1','value':{'xp_delta':12},'session_id':'goal-1'},
        'last_receipt':{'receipt_id':'receipt-9','attempted':True,'possible_input':True,
            'dispatched':False,'dispatch_unknown':True,'effect_status':'unknown'},'frame_id':'frame-7'}))
    (tmp_path/'status.json').write_text(json.dumps({'state':'PLAYING'}))
    snapshot=live_snapshot(tmp_path,now=now)
    v2=snapshot['v2_controller']
    assert v2['session_id']=='goal-1' and v2['session_epoch']=='epoch-4'
    assert v2['task_id']=='v2-session:12345678-1234-1234-1234-123456789abc'
    assert v2['objective']=='grind_nearby'
    assert v2['child_task_id']=='wolf-kill:12345678-1234-1234-1234-123456789abc'
    assert v2['child_attempts_used']==2
    assert v2['child_no_progress_count']==2
    assert v2['child_no_progress_budget']==3
    assert v2['last_verified_progress']['kind']=='xp'
    assert v2['last_verified_progress']['value']=={'xp_delta':12}
    assert v2['deadline_remaining_seconds']==pytest.approx(420,abs=1)
    assert v2['last_verified_progress_age_seconds']>=35
    assert v2['blocked_reason']=='No verified target damage yet.'
    assert v2['last_receipt']['dispatch_unknown'] is True
    lines=display_lines(snapshot)
    assert 'V2 NO_PROGRESS' in lines[0]
    assert 'target L5' in lines[1]
    assert len(lines[5])<=64
    assert 'grind nearby' in lines[5]
    assert 'tries 2/5' in lines[5]
    assert 'no prog 2/3' in lines[5]
    assert 'verified XP 00:35' in lines[5]
    assert 'V2 blocked:' in lines[6]
    assert 'dispatch unknown' in lines[3]
    assert 'possibly' not in '\n'.join(lines).lower()
    store.close()


def test_v2_progress_summary_is_hidden_when_it_belongs_to_another_session(tmp_path):
    now=time.time()
    store=EventStore(tmp_path/'events.sqlite3')
    store.append(Event.create('session_started',{}))
    store.append(Event.create('v2_controller_status',{
        'controller':'v2','status':'NO_PROGRESS','session_id':'active-goal','session_epoch':'epoch-a',
        'absolute_deadline':datetime.fromtimestamp(now+600,timezone.utc).isoformat(),
        'last_verified_progress_age_seconds':4,
        'last_verified_progress':{'kind':'loot','session_id':'old-goal','value':{'item':'old item'}},
        'child_no_progress_count':1,'child_no_progress_budget':3,
    }))
    (tmp_path/'status.json').write_text(json.dumps({'state':'PLAYING'}))
    v2=live_snapshot(tmp_path,now=now)['v2_controller']
    assert v2['last_verified_progress'] is None
    store.close()


def test_sage_quest_checklist_reaches_native_overlay_without_progress_inference(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3')
    try:
        store.append(Event.create('session_started',{}))
        marks={key:{'complete':key=='uninterrupted','verified_count':int(key=='uninterrupted'),'required':1}
            for key in ('level_5','two_quests','repeated_kill_loot','sale','purchase','new_spell','two_different_objectives','uninterrupted')}
        store.append(Event.create('v2_controller_status',{'controller':'v2','status':'ACTIVE',
            'session_id':'current-attempt','target_level':5,'objective':'vendor',
            'sage_quest':{'session_id':'current-attempt','checklist':marks,'target_level':5,
                'elapsed_seconds':90,'remaining_seconds':7110,'active_objective':'vendor',
                'latest_sage_choice':'inspect_bag_slot_1','latest_verified_outcome':'No verified sale yet'}}))
        (tmp_path/'status.json').write_text(json.dumps({'state':'PLAYING'}))
        snapshot=live_snapshot(tmp_path)
        assert not snapshot['v2_controller']['sage_quest']['checklist']['sale']['complete']
        lines=display_lines(snapshot)
        assert 'Sage Quest' in lines[1] and 'Sage Quest' in lines[5]
        assert all(label in lines[5] for label in ('○L5','○Q2','○K/L','○Sell','○Buy','○Spell','○Obj2','✓Auto'))
        assert lines[4]=='Result: No verified sale yet'
        assert len(lines[5])<=64
    finally:store.close()


def test_older_v2_status_does_not_override_a_newer_legacy_session(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3')
    store.append(Event.create('session_started',{}))
    store.append(Event.create('v2_controller_status',{
        'controller':'v2','status':'BLOCKED','session_id':'old-v2','session_epoch':'e1',
        'target_level':5,'task_id':'old-task','objective':'old objective'}))
    store.append(Event.create('session_started',{}))
    snapshot=live_snapshot(tmp_path)
    assert snapshot['v2_controller'] is None
    assert 'old-v2' not in '\n'.join(display_lines(snapshot))
    store.close()


def test_legacy_database_without_v2_events_keeps_legacy_projection(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3')
    store.append(Event.create('session_started',{}))
    store.save_checkpoint('tactical',{'phase':'QUEST'})
    snapshot=live_snapshot(tmp_path)
    assert snapshot['v2_controller'] is None
    assert snapshot['phase']=='QUEST'
    store.close()


def test_smite_burst_action_label_is_friendly():
    assert action_label('smite_burst_3')=='Smite burst ×3'


def test_coordinate_motor_receipt_is_attributed_to_harness_under_sage_waypoint(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3')
    store.append(Event.create('session_started',{}))
    store.append(Event.create('sage_decision',{'chosen':'navigate_quest_waypoint'}))
    store.append(Event.create('v2_controller_status',{
        'controller':'v2','status':'ACTIVE','session_id':'navigation-test','session_epoch':'current',
        'objective':'quest','last_receipt':{'receipt_id':'motor-1','attempted':True,
            'possible_input':True,'dispatched':True,'dispatch_unknown':False,
            'effect_status':'unknown','chosen_option':'turn_right',
            'authorization_type':'sage_coordinate_navigation','authorization_id':'waypoint-1'}}))
    snapshot=live_snapshot(tmp_path)
    assert snapshot['v2_controller']['last_receipt']['authorization_id']=='waypoint-1'
    lines=display_lines(snapshot)
    assert lines[3]=='Harness: turn right · Sage waypoint'
    assert 'waypoint' in lines[2]
    assert 'effect unverified' in lines[7]
    assert 'no verified progress' in lines[5]
    store.close()


@pytest.mark.parametrize(('mode','handoff_status','expected'),[
    ('AWAITING_TASK','awaiting_new_task','Hand-in confirmed · choosing next task'),
    ('GRIND','grinding','Grinding · no active quest'),
    ('QUEST','new_task_confirmed','New task selected · awaiting progress'),
])
def test_dashboard_does_not_resurrect_previous_quest_counter_after_handin(tmp_path,mode,handoff_status,expected):
    store=EventStore(tmp_path/'events.sqlite3')
    store.append(Event.create('quest_progress_observed',{'count':8,'total':8,'item':'Tough Wolf Meat'}))
    store.save_checkpoint('tactical',{'phase':'RETURN_QUEST','progress':None,
        'game_mode':mode,'handin_history':[{'at':time.time()-5}],
        'quest_handoff':{'status':handoff_status}})
    snapshot=live_snapshot(tmp_path)
    assert snapshot['progress']==expected
    assert '8/8' not in '\n'.join(display_lines(snapshot))
    store.close()


def test_grind_projects_actual_phase_receipt_effect_and_credit_separately(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3')
    try:
        store.append(Event.create('session_started',{'mode':'grind_only','session_id':'recovery-1'}))
        store.append(Event.create('tactical_context',{'task':'WORLD'}))
        store.save_checkpoint('tactical',{'phase':'WORLD'})
        store.append(Event.create('action_outcome',{'outcome':'stale generic success'}))
        store.append(Event.create('sage_decision',{'chosen':'turn_left','request_duration_ms':800}))
        store.append(Event.create('execution_receipt',{'receipt_id':'turn-1','chosen_option':'turn_left',
            'attempted':True,'possible_input':True,'dispatched':True,'completed':True,
            'dispatch_unknown':False,'effect_status':'unknown'}))
        store.append(Event.create('grind_action_outcome',{'purpose':'search','outcome':'motion_no_useful_effect','receipt_id':'turn-1'}))
        store.append(Event.create('grind_action_outcome',{'purpose':'semantic_question','outcome':'archived_unresolved'}))
        store.append(Event.create('grind_death_observed',{'encounter_id':1}))
        store.append(Event.create('grind_credited_kill',{'encounter_id':2}))
        store.save_checkpoint('grind_only',{'level':2,'hunt':{'phase':'search',
            'recovery_requested':{'reason':'repeated_uncertain_answer'},'credited_kills':[{'encounter_id':2}]}})
        store.append(Event.create('grind_compact_question',{'stage':'recovery','question':'Change the viewpoint?'}))
        (tmp_path/'status.json').write_text(json.dumps({'state':'PLAYING','mode':'grind_only',
            'session_id':'recovery-1','goal_level':3,'player_level':2,'verified_kills':0}))
        snapshot=live_snapshot(tmp_path)
        grind=snapshot['grind_controller']
        assert snapshot['phase']=='GRIND RECOVERY' and snapshot['choice']=='turn_left'
        assert 'completed; effect unverified' in snapshot['input']
        assert snapshot['outcome']=='search: motion_no_useful_effect'
        assert grind['verified_kills']==1 and grind['current_level']==2 and grind['goal_level']==3
        assert grind['waiting_reason']=='repeated_uncertain_answer'
        assert snapshot['recent'][0]['action']=='turn_left'
        lines=display_lines(snapshot)
        assert len(lines)==8 and lines[5]=='Level 2 / goal 3 · 1 credited kills'
        assert lines[6]=='Waiting: repeated_uncertain_answer'
        assert 'Quest' not in lines[5] and 'stale generic' not in '\n'.join(lines)
    finally:store.close()


@pytest.mark.parametrize('receipt,expected',[
    ({'possible_input':True,'dispatched':True,'completed':True,'dispatch_unknown':True},'dispatch unknown'),
    ({'possible_input':True,'dispatched':True,'completed':False,'dispatch_unknown':False},'partial input'),
    ({'attempted':False,'possible_input':False,'dispatched':False,'completed':True},'Observation / no input'),
    ({'attempted':False,'possible_input':False,'dispatched':False,'completed':False,'error':'guard rejected'},'input rejected'),
])
def test_grind_receipt_completion_and_unknown_input_never_imply_damage(tmp_path,receipt,expected):
    store=EventStore(tmp_path/'events.sqlite3')
    try:
        store.append(Event.create('session_started',{'mode':'grind_only','session_id':'receipt-test'}))
        store.append(Event.create('execution_receipt',{'chosen_option':'attack_mob_level_1',**receipt}))
        snapshot=live_snapshot(tmp_path)
        assert expected in snapshot['input']
        assert snapshot['outcome']=='Not yet verified'
        assert snapshot['grind_controller']['verified_kills']==0
        assert snapshot['grind_controller']['last_receipt']['completed']==receipt['completed']
    finally:store.close()


def test_old_grind_checkpoint_and_status_cannot_override_new_legacy_run(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3')
    try:
        store.append(Event.create('session_started',{'mode':'grind_only','session_id':'old'}))
        store.save_checkpoint('grind_only',{'level':2,'hunt':{'phase':'fight'}})
        store.append(Event.create('session_started',{'mode':'agent'}))
        store.save_checkpoint('tactical',{'phase':'QUEST'})
        (tmp_path/'status.json').write_text(json.dumps({'state':'PLAYING','mode':'grind_only','session_id':'old'}))
        snapshot=live_snapshot(tmp_path)
        assert snapshot['grind_controller'] is None and snapshot['phase']=='QUEST'
    finally:store.close()


def test_new_grind_run_does_not_reuse_old_choice_input_or_progress(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3')
    try:
        store.append(Event.create('session_started',{'mode':'grind_only','session_id':'old'}))
        store.append(Event.create('sage_request_started',{'request_id':'old-pending'}))
        store.append(Event.create('execution_receipt',{'chosen_option':'attack_mob_level_1','possible_input':True,'completed':True}))
        store.append(Event.create('grind_credited_kill',{'encounter_id':1}))
        store.save_checkpoint('grind_only',{'level':2,'hunt':{'phase':'fight'}})
        store.append(Event.create('session_started',{'mode':'grind_only','session_id':'new'}))
        (tmp_path/'status.json').write_text(json.dumps({'state':'PLAYING','mode':'grind_only','session_id':'new','goal_level':3}))
        snapshot=live_snapshot(tmp_path)
        assert snapshot['choice']=='—' and snapshot['input']=='No input receipt yet'
        assert snapshot['grind_controller']['current_level'] is None
        assert snapshot['grind_controller']['verified_kills']==0
        assert snapshot['recent']==[] and snapshot['request_age'] is None
    finally:store.close()
