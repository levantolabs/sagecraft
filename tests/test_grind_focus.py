"""Small offline lifecycle checks; fake capture/provider/backend only."""
import asyncio
from dataclasses import replace
import json
import time

import pytest

from sage_wow.agent.cycle import CycleResult
from sage_wow.grind_runner import GrindRunner, GrindFreeze, digest, source_hashes
from sage_wow.models import Event
from test_grind_only import Frames, Freeze, Sage, Backend, profile, setup, cleanup
from sage_wow.control.executor import GateSnapshot, SafeExecutor, ExecutionRejected
from sage_wow.platform.macos.geometry import Rect


def attached(tmp_path,choices=(),hook=None):
    c,f,s,b,store,e=setup(tmp_path,choices,hook)
    runner=GrindRunner(c.profile,tmp_path,sage=s,executor=e,capture=f.capture,ocr=f.ocr)
    runner.controller=c;runner.store=store;runner.state='PLAYING'
    return runner,c,f,s,b,store,e


@pytest.mark.parametrize('field,value',[
    ('window_id',None),('window_id',8),('calibrated',False),
    ('client_identity_verified',False),('bounds',Rect(1,0,500,300)),
])
def test_only_foreground_loss_is_soft(tmp_path,field,value):
    r,c,f,s,b,store,e=attached(tmp_path)
    try:
        gate=e.inspect_gate()
        e.inspect_gate=lambda:replace(gate,foreground=False)
        assert r.gate_state()=='focus'
        e.inspect_gate=lambda:replace(gate,foreground=False,**{field:value})
        assert r.gate_state()=='invalid'
    finally:store.close()


def test_owner_loss_is_not_a_focus_pause(tmp_path):
    r,c,f,s,b,store,e=attached(tmp_path)
    try:
        def lost():raise RuntimeError('native owner lost')
        b._ensure_owner=lost
        with pytest.raises(RuntimeError,match='native owner'):r.gate_state()
    finally:store.close()


def test_completed_observation_and_physical_receipts_reconcile(tmp_path):
    async def scenario():
        r,c,f,s,b,store,e=attached(tmp_path);e.arm()
        try:
            for binding in ({'type':'observe_only'}, {'type':'keypress','keycode':1,'hold_seconds':.05}):
                result=await c.cycle._dispatch_with_receipt(binding,frame=f.capture(),request_id='fake',
                    session_epoch=c.cycle.session_epoch,candidate_set_version='fake',chosen_option='fake')
                assert result.receipt['completed'] and r.input_reconciled()
            original=c.cycle.last_receipt
            broken=dict(original,dispatch_unknown=True)
            c.cycle._last_receipt=broken
            store.append(Event.create('execution_receipt',broken))
            assert not r.input_reconciled()
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_later_observation_cannot_hide_earlier_unknown_input(tmp_path):
    r,c,f,s,b,store,e=attached(tmp_path)
    try:
        broken={'generation_before':0,'generation_after':1,'possible_input':True,
            'completed':False,'dispatch_unknown':True,'input_steps':[{'status':'effect_unknown'}]}
        later={'generation_before':1,'generation_after':1,'possible_input':False,
            'completed':True,'dispatch_unknown':False,'attempted':False,'dispatched':False,'input_steps':[]}
        for receipt in (broken,later):store.append(Event.create('execution_receipt',receipt))
        c.cycle._last_receipt=later;c.cycle._input_generation=1
        assert not r.input_reconciled()
    finally:store.close()


def test_partial_input_never_enters_soft_focus_pause(tmp_path):
    async def scenario():
        r,c,f,s,b,store,e=attached(tmp_path);e.arm()
        try:
            receipt={'generation_before':0,'generation_after':1,'possible_input':True,
                'completed':False,'dispatch_unknown':False,'error':'interrupted',
                'input_steps':[{'status':'completed'}]}
            store.append(Event.create('execution_receipt',receipt))
            c.cycle._last_receipt=receipt;c.cycle._input_generation=1
            await r.pause()
            assert r.stopping and c.stopped and r.reason=='partial_or_unknown_grind_input'
            assert r.state!='PAUSED_FOCUS' and not r.resume() and not e.armed
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_focus_pause_cancels_inference_and_preserves_session_history(tmp_path):
    async def scenario():
        entered=asyncio.Event();cancelled=[]
        async def blocked():
            entered.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:cancelled.append(True);raise
        r,c,f,s,b,store,e=attached(tmp_path,['player_level_1'],blocked);e.arm()
        deadline=c.deadline;c.history=[{'factual':'retained'}];epoch=c.cycle.session_epoch
        try:
            r.task=asyncio.create_task(c.process(f.capture()))
            await asyncio.wait_for(entered.wait(),1)
            await r.pause()
            assert r.state=='PAUSED_FOCUS' and cancelled and not e.armed
            assert r.task is None and not c.stopped and c.deadline==deadline
            assert c.history==[{'factual':'retained'}] and c.cycle.session_epoch!=epoch
            assert r.input_reconciled() and not any(x[0]=='text' for x in b.events)
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_operator_pause_requires_explicit_resume_and_arm_race_repauses(tmp_path):
    async def scenario():
        r,c,f,s,b,store,e=attached(tmp_path);e.arm()
        try:
            await r.pause(operator=True)
            assert r.state=='PAUSED_OPERATOR' and not r.resume() and not e.armed
            r.operator_paused=False
            gate=e.inspect_gate();calls=[]
            def flicker():
                calls.append(True)
                return gate if len(calls)==1 else replace(gate,foreground=False)
            e.inspect_gate=flicker
            assert not r.resume() and not e.armed and r.state=='PAUSED_OPERATOR'
            e.inspect_gate=lambda:gate
            assert r.resume() and r.state=='RESUMING' and e.armed
            assert c.require_world
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_pause_preserves_terminal_retention_failure(tmp_path):
    async def scenario():
        r,c,f,s,b,store,e=attached(tmp_path);e.arm()
        try:
            async def failed():return CycleResult('evidence_retention_failed')
            r.task=asyncio.create_task(failed());await r.task
            await r.pause()
            assert r.stopping and r.reason=='evidence_retention_failed' and c.stopped
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_late_thread_capture_cannot_enter_retired_controller(tmp_path):
    async def scenario():
        r,c,f,s,b,store,e=attached(tmp_path)
        frame=f.capture();entered=asyncio.Event();release=asyncio.Event()
        original=asyncio.to_thread
        async def fake_thread(fn,*args):
            if fn==r.capture:
                entered.set();await release.wait();return frame
            return await original(fn,*args)
        calls=[]
        async def process(_):calls.append(True);return CycleResult('dispatched')
        c.process=process
        with pytest.MonkeyPatch.context() as monkey:
            monkey.setattr(asyncio,'to_thread',fake_thread)
            pending=asyncio.create_task(r.step());await entered.wait()
            c.pause_focus();release.set()
            assert (await pending).status=='invalidated' and not calls
        await cleanup(e,store)
    asyncio.run(scenario())


@pytest.mark.parametrize('reason',['operator_stop','absolute_deadline'])
def test_stop_and_original_deadline_never_auto_resume(tmp_path,reason):
    async def scenario():
        r,c,f,s,b,store,e=attached(tmp_path);e.arm()
        try:
            await r.pause()
            if reason=='operator_stop':r.request_stop()
            else:c.deadline=time.time()-1
            assert not r.resume() and not e.armed and r.reason==reason
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_custom_catalog_mutation_breaks_frozen_session(tmp_path):
    p=profile(tmp_path);catalog=tmp_path/'custom-areas.yaml';catalog.write_text('original')
    frozen=object.__new__(GrindFreeze);frozen.root=tmp_path;frozen.profile=p;frozen.catalog=catalog
    frozen.manifest={'sources':source_hashes(tmp_path),'area_catalog_sha256':digest(catalog),
        'profile_sha256':digest(p.path),
        'runtime_profile_sha256':__import__('hashlib').sha256(json.dumps(p.values,sort_keys=True).encode()).hexdigest()}
    assert frozen.changed() is None
    catalog.write_text('changed')
    assert frozen.changed()=='grind_area_catalog_changed'


async def eventually(predicate):
    async def wait():
        while not predicate():await asyncio.sleep(.01)
    await asyncio.wait_for(wait(),2)


def test_runner_auto_resumes_clean_focus_without_provider_work_while_paused(tmp_path):
    async def scenario():
        p=profile(tmp_path);frames=Frames(tmp_path);entered=asyncio.Event();first=True
        async def blocked_once():
            nonlocal first
            if first:
                first=False;entered.set();await asyncio.Event().wait()
        sage=Sage(['world_normal_confirmed','player_level_1'],blocked_once)
        backend=Backend();rect=Rect(0,0,500,300)
        good=GateSnapshot(7,7,True,rect,rect,True,True);gate=[good]
        executor=SafeExecutor(backend,lambda:gate[0],heartbeat_timeout=.75,watchdog_interval=.01)
        directory=tmp_path/'run'
        runner=GrindRunner(p,directory,sage=sage,executor=executor,
            capture=frames.capture,ocr=frames.ocr,freeze_factory=Freeze)
        task=asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(entered.wait(),2);deadline=runner.controller.deadline
            gate[0]=replace(good,foreground=False)
            await eventually(lambda:runner.state=='PAUSED_FOCUS')
            calls=len(sage.calls);captures=frames.count
            await asyncio.sleep(.2)
            assert len(sage.calls)==calls and frames.count==captures and not executor.armed
            gate[0]=good
            await eventually(lambda:runner.state=='PLAYING' and runner.controller.baseline)
            assert runner.controller.deadline==deadline and runner.config['session_id']==p.values['grind_only']['session_id']
            (directory/'control.json').write_text(json.dumps({'command':'pause'}))
            await eventually(lambda:runner.state=='PAUSED_OPERATOR')
            calls=len(sage.calls);await asyncio.sleep(.2)
            assert runner.state=='PAUSED_OPERATOR' and len(sage.calls)==calls and not executor.armed
            (directory/'control.json').write_text(json.dumps({'command':'resume'}))
            await eventually(lambda:executor.armed)
            assert runner.controller.baseline and runner.controller.deadline==deadline
            runner.request_stop();await asyncio.wait_for(task,2)
            assert runner.reason=='operator_stop' and backend.closed and sage.closed
        finally:
            if not task.done():runner.request_stop();await asyncio.wait_for(task,2)
    asyncio.run(scenario())


def test_resume_keeps_original_baseline_and_level_schedule(tmp_path):
    async def scenario():
        r,c,f,s,b,store,e=attached(tmp_path,['world_normal_confirmed','search_here']);e.arm()
        try:
            c.baseline=True;c.level.last_confirmed_level=3;c.level.history=[{'level':1},{'level':3}];c.level.last_attempt_at=time.time()
            deadline=c.deadline;await r.pause();assert r.resume()
            assert c.baseline and c.level.last_confirmed_level==3 and c.level.history[:2]==[{'level':1},{'level':3}]
            assert c.require_world
            await c.process(f.capture())
            assert not c.require_world
            await c.process(f.capture())
            assert not c.level.due(time.time()) and not c.stopped and c.baseline and c.deadline==deadline
            assert c.level.last_confirmed_level==3 and c.level.session_epoch==c.cycle.session_epoch
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_transient_watchdog_geometry_failure_stays_hard_after_restoration(tmp_path):
    async def scenario():
        p=profile(tmp_path);frames=Frames(tmp_path);entered=asyncio.Event()
        async def blocked():entered.set();await asyncio.Event().wait()
        sage=Sage([],blocked);backend=Backend();rect=Rect(0,0,500,300)
        good=GateSnapshot(7,7,True,rect,rect,True,True);gate=[good]
        executor=SafeExecutor(backend,lambda:gate[0],heartbeat_timeout=.75,watchdog_interval=.01)
        runner=GrindRunner(p,tmp_path/'run',sage=sage,executor=executor,
            capture=frames.capture,ocr=frames.ocr,freeze_factory=Freeze)
        task=asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(entered.wait(),2)
            gate[0]=replace(good,bounds=Rect(1,0,500,300))
            with pytest.raises(ExecutionRejected):executor._validate_gate()
            gate[0]=good
            await asyncio.wait_for(task,2)
            assert runner.reason=='focus_or_calibration_invalid' and runner.state=='STOPPED'
            assert not executor.armed and len(sage.calls)==1 and not any(x[0]=='text' for x in backend.events)
        finally:
            if not task.done():runner.request_stop();await asyncio.wait_for(task,2)
    asyncio.run(scenario())
