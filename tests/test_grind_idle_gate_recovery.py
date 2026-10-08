"""Actual periodic expiries and bounded owner retirement; fake I/O only."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import json
import os
import socket
import shutil
import subprocess
import threading
import time

import pytest

from sage_wow.control.executor import ExecutionRejected
from sage_wow.models import Event
from test_grind_focus import attached
from test_grind_only import Freeze, baseline, cleanup
from test_grind_heartbeat_recovery import events


@pytest.fixture(autouse=True)
def deny_external_io(monkeypatch):
    assert os.environ['SAGE_WOW_DISABLE_LIVE_INPUT']==os.environ['SAGE_WOW_DISABLE_LIVE_CAPTURE']=='1'
    def deny(*args,**kwargs):raise AssertionError('offline network/process forbidden')
    for name in ('connect','connect_ex','sendto'):monkeypatch.setattr(socket.socket,name,deny)
    for name in ('create_connection','getaddrinfo'):monkeypatch.setattr(socket,name,deny)
    monkeypatch.setattr(subprocess,'Popen',deny);monkeypatch.setattr(os,'system',deny)
    # Capacity is a fake dependency here; live reserve values/guards are unchanged.
    usage=shutil.disk_usage('.')
    monkeypatch.setattr(shutil,'disk_usage',lambda path:usage._replace(free=8*1024**3))


async def until(check,seconds=3):
    async with asyncio.timeout(seconds):
        while not check():await asyncio.sleep(.005)


@asynccontextmanager
async def live_fixture(tmp_path):
    r,c,f,s,b,store,e=attached(tmp_path,['player_level_1'])
    e.heartbeat_timeout=.75;c.deadline=time.time()+30
    raw=e.inspect_gate;queue=[];reads=[];released=threading.Event()
    def reader():
        if threading.current_thread().name=='sage-wow-periodic-gate':
            reads.append(time.monotonic())
            if queue:
                item=queue.pop(0)
                if item=='blocked':released.wait(8)
                elif isinstance(item,float):time.sleep(item)
                elif isinstance(item,BaseException):raise item
                else:return item
        return raw()
    e.inspect_gate=reader;r.observe_executor_gate();e.arm()
    async def pulse():
        while True:e.heartbeat();await asyncio.sleep(.05)
    pulse_task=asyncio.create_task(pulse())
    try:
        await baseline(c,f)
        yield r,c,f,s,b,store,e,queue,reads,released
    finally:
        released.set()
        for task in (r.task,r.gate_retirement_task):
            if task and not task.done():task.cancel()
        await asyncio.gather(*(x for x in (r.task,r.gate_retirement_task) if x),return_exceptions=True)
        pulse_task.cancel();await asyncio.gather(pulse_task,return_exceptions=True)
        await cleanup(e,store)


async def expire(r,e,queue,item=.81):
    queue.append(item)
    try:await r.poll_gate_state()
    except ExecutionRejected:pass
    await until(lambda:not e.armed)
    assert e.disarm_reason=='periodic_gate_inspection_timeout'
    assert e.periodic_timeout_trip['execution_id'] is None


def physical(b):return [x for x in b.events if x[0] in ('key','text')]


def test_completed_receipt_history_and_debt_survive_new_lease_world_fence(tmp_path):
    async def run():
        async with live_fixture(tmp_path) as (r,c,f,s,b,store,e,q,reads,_):
            receipt=(await c.cycle._dispatch_with_receipt({'type':'keypress','keycode':1,'hold_seconds':.02},
                frame=f.capture(),request_id='fake',session_epoch=c.cycle.session_epoch,
                candidate_set_version='fake',chosen_option='fake',authority_check=lambda:None)).receipt
            c.history=[{'retained':'actual history'}];c.hunt.action_failures['retained-failure']=2
            generation=c.cycle.input_generation;epoch=c.cycle.session_epoch;deadline=c.deadline
            before=physical(b);await expire(r,e,q)
            trip=e.periodic_timeout_trip;trip['context']['epoch']='external-copy-mutation'
            assert e.periodic_timeout_trip['context']['epoch']==epoch
            await r.recover_idle_gate(Freeze())
            assert not r.stopping and e.armed and r.state=='RESUMING' and c.require_world
            assert c.cycle.input_generation==generation and c.cycle.session_epoch!=epoch and c.deadline==deadline
            assert c.history==[{'retained':'actual history'}] and c.hunt.action_failures['retained-failure']==2
            assert c.cycle.last_receipt==receipt and physical(b)==before and r.input_reconciled(strict=True)
            assert [v['status'] for v in events(store,'grind_periodic_gate_recovery')]==['retiring','reconciled','new_lease']
            assert events(store,'grind_paused')[-1]['state']=='PAUSED_GATE'
            s.choices.insert(0,'world_normal_confirmed');await c.process(f.capture())
            assert not c.require_world and physical(b)==before and r.periodic_gate_recoveries==1
    asyncio.run(run())


@pytest.mark.parametrize('change',['missing','execution','click_active','held','release','lease','context','reason'])
def test_actual_trip_must_remain_exact_idle_authority(tmp_path,change):
    async def run():
        async with live_fixture(tmp_path) as (r,c,f,s,b,store,e,q,reads,_):
            await expire(r,e,q)
            if change=='missing':e._periodic_timeout_trip=None
            elif change=='execution':e._periodic_timeout_trip['execution_id']='active-text'
            elif change=='click_active':e._periodic_timeout_trip['execution_active']=True
            elif change=='held':e._periodic_timeout_trip['held_inputs']=[{'kind':'key','code':13}]
            elif change=='release':e._periodic_timeout_trip['release_error']='OSError'
            elif change=='lease':e._gate_lease+=1
            elif change=='context':c.revision+=1
            else:e._periodic_timeout_trip['reason']='arbitrary-failure'
            await r.recover_idle_gate(Freeze())
            assert r.stopping and not e.armed and r.periodic_gate_recoveries==0 and not physical(b)
            assert events(store,'grind_periodic_gate_recovery')[-1]['status']=='refused'
    asyncio.run(run())


def add_identity_partial(r,c,store):
    earlier=c.cycle.last_receipt
    broken={'receipt_id':'prior-identity-movement','generation_before':c.cycle.input_generation,
        'generation_after':c.cycle.input_generation+1,'possible_input':True,'completed':False,
        'dispatch_unknown':False,'error':'interrupted','input_steps':[{'status':'completed'}]}
    store.append(Event.create('execution_receipt',broken));c.cycle._last_receipt=broken
    c.cycle._input_generation+=1
    r.reconciled_movements[broken['receipt_id']]=hashlib.sha256(json.dumps(broken,sort_keys=True).encode()).hexdigest()
    assert earlier is not None and r.input_reconciled() and not r.input_reconciled(strict=True)
    return broken


def test_prior_identity_movement_exception_is_not_periodic_recovery_authority(tmp_path):
    async def run():
        async with live_fixture(tmp_path) as (r,c,f,s,b,store,e,q,reads,_):
            add_identity_partial(r,c,store)
            await c.cycle._dispatch_with_receipt({'type':'observe_only'},frame=f.capture(),request_id='later',
                session_epoch=c.cycle.session_epoch,candidate_set_version='fake',chosen_option='fake')
            assert r.input_reconciled() and not r.input_reconciled(strict=True)
            await expire(r,e,q)
            await r.recover_idle_gate(Freeze())
            assert r.stopping and not e.armed and not physical(b)
            assert not events(store,'grind_resuming')
    asyncio.run(run())


@pytest.mark.parametrize('change',['epoch','config','profile','lease','trip','invalid','source','deadline','stop'])
def test_foreign_change_during_retirement_cannot_arm(tmp_path,change):
    async def run():
        async with live_fixture(tmp_path) as (r,c,f,s,b,store,e,q,reads,_):
            await expire(r,e,q);freeze=Freeze();original=r.status
            async def mutate():
                await original()
                if change=='epoch':c.cycle.invalidate('foreign-test-mutation')
                elif change=='config':c.config['goal_level']=4
                elif change=='profile':r.profile.values['character']['name']='foreign'
                elif change=='lease':e._gate_lease+=1
                elif change=='trip':e._periodic_timeout_trip['caller']='replaced'
                elif change=='invalid':r.apply_executor_gate(replace(e.inspect_gate(),window_id=999),'newer-invalid')
                elif change=='source':freeze.changed=lambda:'grind_sources_changed'
                elif change=='deadline':c.deadline=time.time()-1
                else:r.request_stop('operator_stop')
            r.status=mutate
            await r.recover_idle_gate(freeze)
            assert r.stopping and not e.armed and not physical(b)
            assert not events(store,'grind_resuming')
    asyncio.run(run())


@pytest.mark.parametrize('failure',['timeout','native_error','invalid'])
def test_one_fresh_probe_failure_is_terminal_not_nested_recovery(tmp_path,failure):
    async def run():
        async with live_fixture(tmp_path) as (r,c,f,s,b,store,e,q,reads,_):
            await expire(r,e,q);trip=e.periodic_timeout_trip
            q.append(.81 if failure=='timeout' else OSError('offline native') if failure=='native_error'
                else replace(e.inspect_gate(),window_id=99))
            await r.recover_idle_gate(Freeze())
            assert r.stopping and not e.armed and not physical(b) and r.periodic_gate_recoveries==1
            assert e.periodic_timeout_trip==trip
            assert [v['status'] for v in events(store,'grind_periodic_gate_recovery')]==['retiring']
    asyncio.run(run())


def test_two_episode_limit_does_not_reset_after_fresh_world(tmp_path):
    async def run():
        async with live_fixture(tmp_path) as (r,c,f,s,b,store,e,q,reads,_):
            for episode in range(1,4):
                await expire(r,e,q);await r.recover_idle_gate(Freeze())
                if episode<3:
                    assert e.armed and c.require_world and r.periodic_gate_recoveries==episode
                    s.choices.insert(0,'world_normal_confirmed');await c.process(f.capture())
                else:assert r.stopping and r.reason=='idle_gate_recovery_exhausted' and not e.armed
            assert r.periodic_gate_recoveries==2 and not physical(b)
    asyncio.run(run())


@pytest.mark.parametrize('block',['worker','provider','operator_pause'])
def test_fixed_five_second_budget_preserves_tracked_work_and_never_rearms(tmp_path,block):
    async def run():
        async with live_fixture(tmp_path) as (r,c,f,s,b,store,e,q,reads,release):
            finish=asyncio.Event();cancelled=asyncio.Event()
            async def resistant():
                try:await finish.wait()
                except asyncio.CancelledError:cancelled.set();await finish.wait()
            if block=='provider':r.task=asyncio.create_task(resistant());await asyncio.sleep(0)
            await expire(r,e,q,'blocked' if block=='worker' else .81)
            trip=e.periodic_timeout_trip;pending=r.task
            if block=='operator_pause':r.operator_paused=True
            try:
                await asyncio.wait_for(r.recover_idle_gate(Freeze()),6)
                elapsed=time.monotonic()-trip['detected_at_monotonic']
                assert 5<=elapsed<5.6 and r.reason=='idle_gate_recovery_timeout' and not e.armed
                assert not events(store,'grind_resuming') and not physical(b)
                if block=='provider':assert cancelled.is_set() and r.task is pending and not pending.done()
                if block=='worker':assert e.periodic_gate_busy
            finally:
                release.set();finish.set()
                if pending:await pending
                await until(lambda:not e.periodic_gate_busy)
            assert not e.armed
    asyncio.run(run())


def test_invalid_observation_inside_arm_cannot_restore_authority(tmp_path):
    async def run():
        async with live_fixture(tmp_path) as (r,c,f,s,b,store,e,q,reads,_):
            await expire(r,e,q);original=e.arm
            def raced():
                original();r.apply_executor_gate(replace(e.inspect_gate(),window_id=999),'arm-race')
            e.arm=raced
            await r.recover_idle_gate(Freeze())
            assert r.stopping and not e.armed and not physical(b) and not events(store,'grind_resuming')
    asyncio.run(run())


def test_real_held_movement_trip_remains_terminal_after_release(tmp_path):
    async def run():
        async with live_fixture(tmp_path) as (r,c,f,s,b,store,e,q,reads,_):
            action=asyncio.create_task(c.cycle._dispatch_with_receipt(
                {'type':'keypress','keycode':13,'hold_seconds':1.5},frame=f.capture(),request_id='held',
                session_epoch=c.cycle.session_epoch,candidate_set_version='fake',chosen_option='fake',
                authority_check=lambda:None))
            await until(lambda:bool(e._held_since));q.append(.81)
            try:await r.poll_gate_state()
            except ExecutionRejected:pass
            await until(lambda:not e.armed)
            trip=e.periodic_timeout_trip
            assert trip['execution_id'] and trip['execution_active'] and trip['held_inputs']
            await asyncio.gather(action,return_exceptions=True)
            before=physical(b);await r.recover_idle_gate(Freeze())
            assert r.stopping and not e.armed and not e._held_since and physical(b)==before
            assert not c.cycle.last_receipt['completed'] and r.periodic_gate_recoveries==0
    asyncio.run(run())


def test_real_public_runner_retires_late_answer_and_requires_fresh_world(tmp_path):
    from sage_wow.control.executor import SafeExecutor, GateSnapshot
    from sage_wow.grind_runner import GrindRunner
    from sage_wow.platform.macos.geometry import Rect
    from test_grind_only import Backend, Frames, Sage, profile
    async def run():
        p=profile(tmp_path);p.values['grind_only']['duration_seconds']=20
        f=Frames(tmp_path);b=Backend();rect=Rect(0,0,500,300)
        good=GateSnapshot(7,7,True,rect,rect,True,True)
        trigger=threading.Event();fresh=asyncio.Event();allow=asyncio.Event();cancelled=[];retired={}
        def read():
            if threading.current_thread().name=='sage-wow-periodic-gate' and trigger.is_set():
                trigger.clear();time.sleep(.82)
            return good
        async def provider():
            if len(s.calls)==3:
                retired.update(epoch=r.controller.cycle.session_epoch,deadline=r.controller.deadline)
                r.controller.history=[{'retained':'natural'}];trigger.set()
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:cancelled.append(True)
            elif len(s.calls)==4:fresh.set();await allow.wait()
        s=Sage(['world_normal_confirmed','player_level_1','attack_mob_level_1','world_normal_confirmed'],provider)
        e=SafeExecutor(b,read,heartbeat_timeout=.75,watchdog_interval=.01)
        r=GrindRunner(p,tmp_path/'run',sage=s,executor=e,capture=f.capture,ocr=f.ocr,freeze_factory=Freeze)
        task=asyncio.create_task(r.run())
        try:
            await asyncio.wait_for(fresh.wait(),7)
            assert cancelled and e.armed and r.state=='RESUMING' and r.controller.require_world
            assert r.controller.cycle.session_epoch!=retired['epoch']
            assert r.controller.deadline==retired['deadline'] and r.controller.history==[{'retained':'natural'}]
            assert not physical(b) and r.periodic_gate_recoveries==1
            allow.set();await until(lambda:r.state=='PLAYING')
            assert not r.controller.require_world and not physical(b)
        finally:r.request_stop();allow.set();await asyncio.wait_for(task,3)
    asyncio.run(run())
