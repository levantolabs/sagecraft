"""Missing app metadata waits without granting input; all native IO is fake."""
import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
import json
import threading
import time

import pytest

from sage_wow.control.executor import GateSnapshot, SafeExecutor, ExecutionRejected
from sage_wow.grind_runner import GrindRunner
from sage_wow.platform.macos.geometry import Rect
from sage_wow.platform.macos.window_gate import gate_recovery_kind
from test_grind_focus import attached, eventually
from test_grind_only import Backend, Frames, Freeze, Sage, cleanup, profile


def gates(reason='application_missing'):
    rect=Rect(0,0,500,300)
    window={'window_id':7,'owner_pid':42,'owner':'Game','title':'Game',
        'bounds':{'X':0,'Y':0,'Width':500,'Height':300},'onscreen':True}
    evidence={'reason':'verified','window':window,'expected_bundle_id':'test.game',
        'expected_path':'/Applications/Test.app','detected_bundle_id':'test.game',
        'detected_path':'/Applications/Test.app','lookups':[{'application_present':True}]}
    good=GateSnapshot(7,7,True,rect,rect,True,True,evidence)
    missing=deepcopy(evidence);missing['reason']=reason
    if reason=='application_missing':
        missing.update(detected_bundle_id=None,detected_path=None,
            lookups=[{'application_present':False}]*2,window_before_retry=window,window_after_retry=window)
    elif reason=='bundle_identifier_missing':missing['detected_bundle_id']=None
    elif reason=='bundle_path_missing':missing['detected_path']=None
    return good,replace(good,client_identity_verified=False,identity_evidence=missing)


def attach_gate(runner,executor,gate):
    executor.inspect_gate=lambda:gate[0]
    runner.observe_executor_gate()


def events(store,kind):
    return [json.loads(row[0]) for row in store.connection.execute(
        'SELECT payload_json FROM events WHERE event_type=? ORDER BY rowid',(kind,))]


@pytest.mark.parametrize('reason',['application_missing','bundle_identifier_missing','bundle_path_missing'])
@pytest.mark.parametrize('foreground',[True,False])
def test_known_absence_is_recoverable_but_never_a_valid_input_gate(reason,foreground):
    good,missing=gates(reason)
    missing=replace(missing,foreground=foreground)
    assert gate_recovery_kind(missing)=='identity' and not missing.valid
    assert gate_recovery_kind(replace(good,foreground=foreground))==('valid' if foreground else 'focus')


@pytest.mark.parametrize('change',['bundle_mismatch','path_mismatch','exception','legacy_boolean',
    'window_disappeared','geometry_changed','calibration_changed','window_id_changed',
    'retry_window_changed','missing_retry','missing_owner','missing_configuration'])
def test_contradictions_and_unattributed_failures_remain_hard(change):
    good,bad=gates('bundle_identifier_missing' if change=='path_mismatch' else 'bundle_path_missing')
    e=deepcopy(bad.identity_evidence)
    if change=='bundle_mismatch':e['detected_bundle_id']='other.app'
    elif change=='path_mismatch':e['detected_path']='/Applications/Other.app'
    elif change=='exception':e.update(reason='identity_query_exception',error_type='TypeError')
    elif change=='legacy_boolean':e=None
    elif change=='window_disappeared':bad=replace(bad,window_id=None,bounds=None)
    elif change=='geometry_changed':bad=replace(bad,bounds=Rect(1,0,500,300))
    elif change=='calibration_changed':bad=replace(bad,calibrated=False)
    elif change=='window_id_changed':bad=replace(bad,window_id=8)
    elif change=='retry_window_changed':
        _,bad=gates();e=deepcopy(bad.identity_evidence);e['window_after_retry']['owner_pid']=43
        e['window_before_retry']=good.identity_evidence['window'];e['window']=good.identity_evidence['window']
    elif change=='missing_retry':e['lookups']=[]
    elif change=='missing_owner':del e['window']['owner_pid']
    elif change=='missing_configuration':del e['expected_path']
    assert gate_recovery_kind(replace(bad,identity_evidence=e))=='invalid'


@pytest.mark.parametrize('reason',['application_missing','bundle_identifier_missing','bundle_path_missing'])
def test_runner_recovers_without_restart_and_retires_late_attack(tmp_path,reason):
    async def run():
        p=profile(tmp_path);frames=Frames(tmp_path);backend=Backend();good,missing=gates(reason);gate=[good]
        executor=SafeExecutor(backend,lambda:gate[0],heartbeat_timeout=5,watchdog_interval=.01)
        old_entered=asyncio.Event();fresh_entered=asyncio.Event();fresh_release=asyncio.Event();cancelled=[]
        async def hook():
            if len(sage.calls)==3:
                old_entered.set()
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:cancelled.append(True)
                # Return the obsolete attack even after cancellation.
            elif len(sage.calls)==4:
                fresh_entered.set();await fresh_release.wait()
        sage=Sage(['world_normal_confirmed','player_level_1','attack_mob_level_1','world_normal_confirmed'],hook)
        runner=GrindRunner(p,tmp_path/'run',sage=sage,executor=executor,
            capture=frames.capture,ocr=frames.ocr,freeze_factory=Freeze)
        task=asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(old_entered.wait(),3)
            c=runner.controller;deadline=c.deadline;epoch=c.cycle.session_epoch;generation=c.cycle.input_generation
            c.history.append({'factual':'retained'})
            gate[0]=missing
            await eventually(lambda:runner.state=='PAUSED_IDENTITY')
            assert cancelled and not executor.armed and not task.done() and not c.stopped
            calls,captures=len(sage.calls),frames.count
            await asyncio.sleep(.25)
            assert (len(sage.calls),frames.count)==(calls,captures)
            assert not runner.resume() and not executor.armed
            gate[0]=good
            await asyncio.wait_for(fresh_entered.wait(),3)
            assert runner.state=='RESUMING' and c.require_world
            assert c.deadline==deadline and c.cycle.session_epoch!=epoch
            assert c.baseline and c.level.last_confirmed_level==1 and {'factual':'retained'} in c.history
            assert c.cycle.input_generation==generation
            assert not any(x[0] in {'key','text'} for x in backend.events)
            fresh_release.set();await eventually(lambda:runner.state=='PLAYING')
            recovery=events(runner.store,'grind_identity_recovery')
            assert [x['status'] for x in recovery]==['waiting','verified']
            assert recovery[0]['observation']['snapshot']==asdict(missing)
            assert recovery[1]['verified_gate']==asdict(good) and recovery[1]['fresh_world_required']
            assert len(events(runner.store,'session_started'))==1
            assert not events(runner.store,'session_stopped') and not events(runner.store,'grind_gate_failure')
            runner.request_stop();await asyncio.wait_for(task,2)
        finally:
            if not task.done():runner.request_stop();await asyncio.wait_for(task,2)
    asyncio.run(run())


@pytest.mark.parametrize('origin',['watchdog','preparation_worker'])
def test_short_worker_only_gap_still_requires_pause_and_new_world(tmp_path,origin):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path);good,missing=gates();gate=[good]
        attach_gate(r,e,gate);e.arm();epoch=c.cycle.session_epoch
        try:
            def fail_once():
                gate[0]=missing
                if origin=='watchdog':
                    with pytest.raises(ExecutionRejected):e._validate_gate()
                else:assert not e.inspect_gate().valid
                gate[0]=good
            await asyncio.to_thread(fail_once)
            assert not e.armed and r.identity_recovery is not None
            assert r.identity_recovery['observation']['thread_name']!=threading.current_thread().name
            await r.pause()
            assert r.state=='PAUSED_IDENTITY' and not r.stopping
            assert r.resume() and c.require_world and c.cycle.session_epoch!=epoch
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_initial_arm_lookup_gap_waits_without_capture_then_starts_same_run(tmp_path):
    async def run():
        p=profile(tmp_path);frames=Frames(tmp_path);backend=Backend();good,missing=gates();gate=[missing]
        executor=SafeExecutor(backend,lambda:gate[0],heartbeat_timeout=5,watchdog_interval=.01)
        sage=Sage(['world_normal_confirmed','player_level_1'])
        r=GrindRunner(p,tmp_path/'run',sage=sage,executor=executor,
            capture=frames.capture,ocr=frames.ocr,freeze_factory=Freeze)
        task=asyncio.create_task(r.run())
        try:
            await eventually(lambda:r.state=='PAUSED_IDENTITY')
            assert frames.count==0 and not sage.calls and not executor.armed
            deadline=r.controller.deadline;gate[0]=good
            await eventually(lambda:r.controller.baseline)
            assert r.controller.deadline==deadline and not r.stopping
            assert len(events(r.store,'session_started'))==1
            r.request_stop();await asyncio.wait_for(task,2)
        finally:
            if not task.done():r.request_stop();await asyncio.wait_for(task,2)
    asyncio.run(run())


@pytest.mark.parametrize('change',['owner','geometry','bundle','path'])
def test_identity_pause_cannot_resume_into_changed_application_or_window(tmp_path,change):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path);good,missing=gates();gate=[good]
        attach_gate(r,e,gate);e.arm()
        try:
            gate[0]=missing;assert r.gate_state()=='identity';await r.pause()
            changed=deepcopy(good.identity_evidence)
            if change=='owner':changed['window']['owner_pid']=43
            elif change=='bundle':changed.update(reason='bundle_identifier_mismatch',detected_bundle_id='other.app')
            elif change=='path':changed.update(reason='bundle_path_mismatch',detected_path='/Applications/Other.app')
            gate[0]=replace(good,identity_evidence=changed,
                client_identity_verified=change not in {'bundle','path'},
                bounds=Rect(1,0,500,300) if change=='geometry' else good.bounds)
            assert r.gate_state()=='invalid'
            # Even a later missing or valid sample cannot erase the contradiction.
            gate[0]=missing;assert r.gate_state()=='invalid'
            gate[0]=good;assert not r.resume() and r.stopping and not e.armed
            assert r.reason=='focus_or_calibration_invalid'
        finally:await cleanup(e,store)
    asyncio.run(run())


@pytest.mark.parametrize('termination',['operator','deadline','focus','arm_race'])
def test_identity_recovery_respects_pause_stop_deadline_and_arm_race(tmp_path,termination):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path);good,missing=gates();gate=[good]
        attach_gate(r,e,gate);e.arm()
        try:
            gate[0]=missing;assert r.gate_state()=='identity';await r.pause()
            deadline=c.deadline;gate[0]=good
            if termination=='operator':await r.pause(operator=True)
            elif termination=='deadline':c.deadline=time.time()-1
            elif termination=='focus':gate[0]=replace(good,foreground=False)
            else:
                original=r.original_gate_inspector;calls=[]
                def flicker():
                    calls.append(True)
                    return good if len(calls)==1 else missing
                r.original_gate_inspector=flicker
            assert not r.resume() and not e.armed
            if termination=='deadline':assert r.reason=='absolute_deadline' and r.stopping
            else:
                if termination=='operator':r.operator_paused=False
                if termination=='arm_race':r.original_gate_inspector=original
                gate[0]=good
                assert r.resume() and c.require_world and c.deadline==deadline
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_identity_loss_during_real_key_receipt_releases_and_stops_partial_input(tmp_path):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path);good,missing=gates();gate=[good]
        attach_gate(r,e,gate);e.arm()
        try:
            r.task=asyncio.create_task(c.cycle._dispatch_with_receipt(
                {'type':'keypress','keycode':1,'hold_seconds':.5},frame=f.capture(),request_id='fake',
                session_epoch=c.cycle.session_epoch,candidate_set_version='fake',chosen_option='fake'))
            await eventually(lambda:any(x[0]=='key' and x[-1] for x in b.events))
            gate[0]=missing;assert r.gate_state()=='identity'
            assert not e.armed and b.events[-1]==('release',)
            await r.pause()
            assert r.stopping and c.stopped and r.reason=='partial_or_unknown_grind_input'
            gate[0]=good;assert not r.resume()
        finally:await cleanup(e,store)
    asyncio.run(run())


@pytest.mark.parametrize('timing',['before_arm','after_arm'])
@pytest.mark.parametrize('contradiction',[False,True])
def test_a_concurrent_bad_observation_cannot_be_cleared_by_successful_arm(tmp_path,timing,contradiction):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path);good,missing=gates();gate=[good]
        attach_gate(r,e,gate);e.arm()
        try:
            gate[0]=missing;assert r.gate_state()=='identity';await r.pause()
            gate[0]=good;epoch=c.cycle.session_epoch;original_arm=e.arm
            def interleave():
                gate[0]=replace(good,calibrated=False) if contradiction else missing
                assert not e.inspect_gate().valid
                gate[0]=good
            def racing_arm():
                if timing=='before_arm':interleave()
                original_arm()
                if timing=='after_arm':interleave()
            e.arm=racing_arm
            assert not r.resume() and not e.armed and c.cycle.session_epoch==epoch
            assert r.identity_recovery is not None
            e.arm=original_arm
            if contradiction:
                assert not r.resume() and r.stopping and r.reason=='focus_or_calibration_invalid'
            else:assert r.resume() and c.require_world
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_failed_release_during_identity_gap_is_not_hidden_by_later_success(tmp_path):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path);good,missing=gates();gate=[good]
        attach_gate(r,e,gate);e.arm();release=b.release_all
        try:
            def failed_release():raise OSError('fake release failure')
            b.release_all=failed_release;gate[0]=missing
            with pytest.raises(OSError,match='fake release'):e.inspect_gate()
            b.release_all=release;gate[0]=good
            await r.pause()
            assert r.stopping and r.reason=='input_release_failed' and not e.armed
            assert not r.resume()
        finally:
            b.release_all=release;await cleanup(e,store)
    asyncio.run(run())


def test_repeated_clean_identity_gaps_preserve_history_and_deadline(tmp_path):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path);good,missing=gates();gate=[good]
        attach_gate(r,e,gate);e.arm();deadline=c.deadline;c.history=[{'factual':'retained'}]
        try:
            for _ in range(3):
                gate[0]=missing;assert r.gate_state()=='identity';await r.pause()
                gate[0]=good;assert r.resume() and c.require_world and c.deadline==deadline
                assert c.history==[{'factual':'retained'}] and r.identity_recovery is None
            assert [row['status'] for row in events(store,'grind_identity_recovery')]==['waiting','verified']*3
            assert not any(row[0] in {'key','text'} for row in b.events)
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_identity_rejection_before_first_input_keeps_receipt_and_can_resume(tmp_path):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path);good,missing=gates();gate=[good]
        attach_gate(r,e,gate);e.arm()
        try:
            gate[0]=missing
            result=await c.cycle._dispatch_with_receipt(
                {'type':'keypress','keycode':1,'hold_seconds':.1},frame=f.capture(),request_id='fake',
                session_epoch=c.cycle.session_epoch,candidate_set_version='fake',chosen_option='fake')
            assert result.status=='execution_failed' and not result.receipt['possible_input']
            assert not result.receipt['input_steps'] and not e.armed
            assert r.input_reconciled()
            await r.pause();assert r.state=='PAUSED_IDENTITY' and not r.stopping
            gate[0]=good;assert r.resume() and c.require_world
            assert c.cycle.last_receipt==result.receipt and c.cycle.input_generation==0
            assert not any(row[0] in {'key','text'} for row in b.events)
        finally:await cleanup(e,store)
    asyncio.run(run())
