"""Expired leases stay retired; only reconciled idle episodes can start anew."""
import asyncio
from dataclasses import replace
import json
import time

import pytest

from sage_wow.control.executor import GateSnapshot, SafeExecutor
from sage_wow.grind_runner import GrindRunner
from sage_wow.models import Event
from sage_wow.platform.macos.geometry import Rect
from test_grind_focus import attached, eventually
from test_grind_only import Backend, Frames, Freeze, Sage, cleanup, profile


def expire_idle(executor):
    executor._last_heartbeat-=executor.heartbeat_timeout+1
    executor.heartbeat()
    assert not executor.armed and executor.disarm_reason=='controller_heartbeat_lost'


def events(store,kind):
    return [json.loads(row[0]) for row in store.connection.execute(
        'SELECT payload_json FROM events WHERE event_type=? ORDER BY rowid',(kind,))]


def test_real_idle_stall_retires_late_attack_and_requires_fresh_world(tmp_path):
    async def scenario():
        p=profile(tmp_path);frames=Frames(tmp_path);backend=Backend();rect=Rect(0,0,500,300)
        good=GateSnapshot(7,7,True,rect,rect,True,True)
        executor=SafeExecutor(backend,lambda:good,heartbeat_timeout=.75,watchdog_interval=.01)
        fresh_entered=asyncio.Event();confirm_world=asyncio.Event();cancelled=[];retired=[]
        async def hook():
            count=len(sage.calls)
            if count==3:
                retired.append(runner.controller.cycle.session_epoch)
                runner.controller.history=[{'factual':'retained'}]
                # The real independent watcher must expire while this loop is stuck.
                time.sleep(.85)
                assert not executor.armed
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:cancelled.append(True)
                # Simulate a provider returning its old attack despite cancellation.
            elif count==4:
                fresh_entered.set();await confirm_world.wait()
        sage=Sage(['world_normal_confirmed','player_level_1','attack_mob_level_1','world_normal_confirmed'],hook)
        runner=GrindRunner(p,tmp_path/'run',sage=sage,executor=executor,
            capture=frames.capture,ocr=frames.ocr,freeze_factory=Freeze)
        task=asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(fresh_entered.wait(),5)
            c=runner.controller;deadline=c.deadline
            assert cancelled and runner.state=='RESUMING' and c.require_world
            assert c.cycle.session_epoch!=retired[0] and c.baseline and c.level.last_confirmed_level==1
            assert c.history==[{'factual':'retained'}]
            assert not [entry for entry in backend.events if entry[0] in {'key','text'}]
            recovery=events(runner.store,'grind_heartbeat_recovery')
            assert [entry['status'] for entry in recovery]==['retiring','new_lease']
            assert recovery[0]['trip']['held_inputs']==[] and recovery[0]['trip']['execution_id'] is None
            assert recovery[1]['retired_session_epoch']==retired[0]
            assert recovery[1]['fresh_world_required'] and executor.last_watchdog_trip is None
            assert 'world_normal_confirmed' in sage.calls[-1]['options']
            confirm_world.set();await eventually(lambda:runner.state=='PLAYING')
            assert c.deadline==deadline and not c.require_world and not c.hunt.credited_kills
        finally:
            runner.request_stop();confirm_world.set();await asyncio.wait_for(task,3)
    asyncio.run(scenario())


@pytest.mark.parametrize('change',[
    {'execution_id':'active-click'}, {'held_inputs':[{'kind':'key','code':1}]},
    {'release_error':'OSError'}, {'missing_trip':True},
])
def test_nonidle_or_unproven_expiry_never_soft_pauses_even_for_operator(tmp_path,change):
    async def scenario():
        r,c,f,s,b,store,e=attached(tmp_path);e.arm()
        try:
            expire_idle(e)
            if change.get('missing_trip'):e.last_watchdog_trip=None
            else:e.last_watchdog_trip.update(change)
            await r.pause(operator=True)
            assert r.stopping and c.stopped and r.reason=='controller_heartbeat_lost'
            assert not r.resume() and not e.armed
            assert events(store,'grind_heartbeat_recovery')[0]['status']=='refused'
        finally:await cleanup(e,store)
    asyncio.run(scenario())


@pytest.mark.parametrize('uncertainty',['unknown_earlier','generation','active'])
def test_idle_trip_does_not_bypass_full_receipt_reconciliation(tmp_path,uncertainty):
    async def scenario():
        r,c,f,s,b,store,e=attached(tmp_path);e.arm()
        try:
            if uncertainty=='unknown_earlier':
                broken={'generation_before':0,'generation_after':1,'possible_input':True,
                    'completed':False,'dispatch_unknown':True,'input_steps':[{'status':'effect_unknown'}]}
                later={'generation_before':1,'generation_after':1,'possible_input':False,
                    'completed':True,'dispatch_unknown':False,'attempted':False,'dispatched':False,'input_steps':[]}
                for receipt in (broken,later):store.append(Event.create('execution_receipt',receipt))
                c.cycle._last_receipt=later;c.cycle._input_generation=1
            elif uncertainty=='generation':c.cycle._input_generation=1
            else:c.cycle._active_receipt={'possible_input':False}
            expire_idle(e);await r.pause()
            assert r.stopping and r.reason=='partial_or_unknown_grind_input'
            assert not r.resume() and not e.armed
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_completed_input_reconciles_but_delayed_recovery_probe_stays_disarmed(tmp_path):
    async def scenario():
        r,c,f,s,b,store,e=attached(tmp_path);e.arm()
        try:
            await c.cycle._dispatch_with_receipt({'type':'keypress','keycode':1,'hold_seconds':.05},
                frame=f.capture(),request_id='fake',session_epoch=c.cycle.session_epoch,
                candidate_set_version='fake',chosen_option='fake',authority_check=lambda:None)
            generation=c.cycle.input_generation;deadline=c.deadline
            expire_idle(e);await r.pause()
            assert r.state=='PAUSED_HEARTBEAT' and r.input_reconciled()
            e.heartbeat();assert not e.armed and not r.resume()
            r.recovery_probe_at-=e.heartbeat_timeout+1
            assert not r.resume() and not e.armed
            await asyncio.sleep(.11)
            assert r.resume() and e.armed and c.require_world
            assert c.cycle.input_generation==generation and c.deadline==deadline
        finally:await cleanup(e,store)
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['source','deadline','hard_gate','restored_hard_gate','operator_pause'])
def test_runner_rechecks_resume_constraints_after_idle_trip(tmp_path,change):
    async def scenario():
        p=profile(tmp_path);frames=Frames(tmp_path);backend=Backend();rect=Rect(0,0,500,300)
        good=GateSnapshot(7,7,True,rect,rect,True,True);gate=[good];entered=asyncio.Event()
        async def blocked():entered.set();await asyncio.Event().wait()
        sage=Sage([],blocked)
        executor=SafeExecutor(backend,lambda:gate[0],heartbeat_timeout=.75,watchdog_interval=.01)
        class MutableFreeze(Freeze):
            changed_reason=None
            def changed(self):return self.changed_reason
        runner=GrindRunner(p,tmp_path/'run',sage=sage,executor=executor,
            capture=frames.capture,ocr=frames.ocr,freeze_factory=MutableFreeze)
        original_pause=runner.pause
        async def pause(**kwargs):
            await original_pause(**kwargs)
            if change=='source':MutableFreeze.changed_reason='grind_sources_changed'
            elif change=='deadline':runner.controller.deadline=time.time()-1
            elif change=='hard_gate':gate[0]=replace(good,calibrated=False)
            elif change=='restored_hard_gate':runner.invalid_gate=replace(good,calibrated=False)
            else:runner.operator_paused=True
        runner.pause=pause
        task=asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(entered.wait(),2);expire_idle(executor)
            if change=='operator_pause':
                await eventually(lambda:runner.state=='PAUSED_HEARTBEAT')
                await asyncio.sleep(.25)
                assert not executor.armed and len(sage.calls)==1
                assert not events(runner.store,'grind_resuming')
                runner.request_stop()
            await asyncio.wait_for(task,3)
            expected={'source':'grind_sources_changed','deadline':'absolute_deadline',
                'hard_gate':'focus_or_calibration_invalid','restored_hard_gate':'focus_or_calibration_invalid',
                'operator_pause':'operator_stop'}
            assert runner.reason==expected[change] and not executor.armed and len(sage.calls)==1
        finally:
            if not task.done():runner.request_stop();await asyncio.wait_for(task,3)
    asyncio.run(scenario())
