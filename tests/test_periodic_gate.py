"""Periodic read-only scheduling; fake native/backend IO and real async lifecycles."""
import asyncio
from dataclasses import replace
import threading
import time

import pytest

from sage_wow.control.executor import ExecutionRejected, SafeExecutor
from test_executor import FakeBackend, valid_gate
from test_grind_focus import attached
from test_grind_only import cleanup


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():await asyncio.sleep(.001)


def blocked_reader(result=None):
    entered=threading.Event();release=threading.Event();calls=[]
    def read():
        calls.append(threading.get_ident());entered.set()
        assert release.wait(2),'offline worker was not released'
        return result or valid_gate()
    return read,entered,release,calls


def test_periodic_worker_does_not_block_synchronous_action_or_run_backend_off_owner():
    async def run():
        owner=threading.get_ident();backend=FakeBackend();threads=[]
        original=backend.key
        def key(*args):threads.append(threading.get_ident());return original(*args)
        backend.key=key
        e=SafeExecutor(backend,valid_gate,heartbeat_timeout=1,watchdog_interval=10)
        read,entered,release,calls=blocked_reader();e.configure_periodic_gate(reader=read)
        e.arm();task=asyncio.create_task(e.inspect_gate_periodic('test'))
        try:
            await until(entered.is_set)
            assert e._validate_gate().valid  # Independent; no periodic admission wait.
            async with asyncio.timeout(.5):
                result=await e.execute({'type':'keypress','keycode':13,'hold_seconds':.02})
            assert result['dispatched'] and threads==[owner,owner]
            assert len(calls)==1 and calls[0]!=owner and e.periodic_gate_busy
            release.set();assert (await task).valid
        finally:release.set();await e.stop()
    asyncio.run(run())


@pytest.mark.parametrize('invalid',[False,True])
def test_cancelled_waiter_keeps_same_lease_invalid_observation_on_owner_thread(invalid):
    async def run():
        owner=threading.get_ident();seen=[]
        good=valid_gate();value=replace(good,foreground=False) if invalid else good
        read,entered,release,calls=blocked_reader(value)
        e=SafeExecutor(FakeBackend(),valid_gate,heartbeat_timeout=1,watchdog_interval=10)
        e.configure_periodic_gate(reader=read,observer=lambda gate,caller:(seen.append((gate,threading.get_ident())) or gate))
        e.arm();task=asyncio.create_task(e.inspect_gate_periodic('cancelled'))
        try:
            await until(entered.is_set);task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert e.periodic_gate_busy
            with pytest.raises(ExecutionRejected,match='in flight'):e.arm()
            release.set();await until(lambda:not e.periodic_gate_busy)
            assert seen[0][1]==owner and not e.backend.events
            assert e.armed is not invalid
        finally:release.set();await e.stop()
    asyncio.run(run())


@pytest.mark.parametrize('change',['generation','window','revision'])
@pytest.mark.parametrize('invalid',[False,True])
def test_periodic_scope_changes_do_not_adopt_old_positive_or_poison_new_scope(change,invalid):
    async def run():
        scope={'generation':0,'window':10,'revision':1};seen=[]
        value=replace(valid_gate(),foreground=False) if invalid else valid_gate()
        read,entered,release,_=blocked_reader(value)
        e=SafeExecutor(FakeBackend(),valid_gate,heartbeat_timeout=1,watchdog_interval=10)
        e.configure_periodic_gate(reader=read,context=lambda:dict(scope),observer=lambda gate,caller:(seen.append(gate) or gate))
        e.arm();task=asyncio.create_task(e.inspect_gate_periodic('scope'))
        try:
            await until(entered.is_set);scope[change]+=1;release.set()
            result=await task
            if change=='generation' and invalid:
                assert result is not None and not result.valid and not e.armed and seen
            else:assert result is None and e.armed
            assert not e.backend.events
        finally:release.set();await e.stop()
    asyncio.run(run())


def test_timeout_stop_and_late_invalid_cannot_overlap_or_poison_new_lease():
    async def run():
        seen=[];read,entered,release,calls=blocked_reader(replace(valid_gate(),foreground=False))
        e=SafeExecutor(FakeBackend(),valid_gate,heartbeat_timeout=.08,watchdog_interval=10)
        e.configure_periodic_gate(reader=read,observer=lambda gate,caller:(seen.append(gate) or gate))
        e.arm();task=asyncio.create_task(e.inspect_gate_periodic('timeout'))
        try:
            await until(entered.is_set)
            with pytest.raises(ExecutionRejected,match='timed out'):await task
            assert not e.armed and e.periodic_gate_busy
            second=asyncio.create_task(e.inspect_gate_periodic('second'))
            await asyncio.sleep(.01);second.cancel()
            with pytest.raises(asyncio.CancelledError):await second
            assert len(calls)==1
            started=time.monotonic();await e.stop()
            assert time.monotonic()-started<.5 and e.periodic_gate_busy
            with pytest.raises(ExecutionRejected,match='in flight'):e.arm()
            release.set();await until(lambda:not e.periodic_gate_busy)
            assert not seen and not e.backend.events
            e.arm();assert e.armed
        finally:release.set();await e.stop()
    asyncio.run(run())


def test_each_periodic_poll_gets_a_new_raw_read_and_keeps_hard_invalid_latch(tmp_path):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path)
        raw=e.inspect_gate;count=[]
        def reader():count.append(threading.get_ident());return raw()
        e.inspect_gate=reader;r.observe_executor_gate();e.arm()
        try:
            assert await r.poll_gate_state()=='valid'
            assert await r.poll_gate_state()=='valid'
            assert len([x for x in count if x!=threading.get_ident()])==2
            r.apply_executor_gate(replace(raw(),window_id=999),'independent_newer_invalid')
            assert await r.poll_gate_state()=='invalid'  # A later positive cannot clear it.
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_standalone_periodic_fallback_preserves_synchronous_inspector():
    async def run():
        reads=[];owner=threading.get_ident()
        def inspect():reads.append(threading.get_ident());return valid_gate()
        e=SafeExecutor(FakeBackend(),inspect,heartbeat_timeout=1,watchdog_interval=10)
        e.arm()
        try:
            assert e.inspect_gate is inspect and (await e.inspect_gate_periodic('fallback')).valid
            assert owner in reads and any(x!=owner for x in reads)
        finally:await e.stop()
    asyncio.run(run())


@pytest.mark.parametrize('failure',[False,True])
def test_resume_restores_temporary_inspection_wrapper(tmp_path,monkeypatch,failure):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path);r.observe_executor_gate();e.arm()
        try:
            await r.pause(operator=True);r.operator_paused=False
            wrapper=e.inspect_gate
            if failure:
                def fail():raise ExecutionRejected('fake arm refusal')
                monkeypatch.setattr(e,'arm',fail)
                with pytest.raises(ExecutionRejected,match='fake arm'):r.resume()
            else:assert r.resume()
            assert e.inspect_gate is wrapper
            assert e._periodic_reader is not None and e._periodic_observer==r.apply_executor_gate
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_cancelled_admission_never_starts_queued_native_work_or_strands_lock():
    async def run():
        read,entered,release,calls=blocked_reader()
        e=SafeExecutor(FakeBackend(),valid_gate,heartbeat_timeout=1,watchdog_interval=10)
        e.configure_periodic_gate(reader=read);e.arm()
        first=asyncio.create_task(e.inspect_gate_periodic('first'))
        try:
            await until(entered.is_set)
            queued=asyncio.create_task(e.inspect_gate_periodic('cancelled_admission'))
            await asyncio.sleep(.01);queued.cancel()
            with pytest.raises(asyncio.CancelledError):await queued
            assert len(calls)==1
            release.set();assert (await first).valid
            assert (await e.inspect_gate_periodic('new_fresh_read')).valid and len(calls)==2
        finally:release.set();await e.stop()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['reader','worker_start'])
def test_periodic_transport_failure_retires_authority_without_input(monkeypatch,failure):
    async def run():
        e=SafeExecutor(FakeBackend(),valid_gate,heartbeat_timeout=1,watchdog_interval=10)
        e.arm()
        def fail(*args):raise RuntimeError('offline native inspection failure')
        if failure=='reader':e.configure_periodic_gate(reader=fail)
        else:
            original_start=threading.Thread.start
            def fail_periodic_start(thread):
                if thread.name=='sage-wow-periodic-gate':fail()
                return original_start(thread)
            monkeypatch.setattr(threading.Thread,'start',fail_periodic_start)
        try:
            with pytest.raises(ExecutionRejected,match='inspection failed'):await e.inspect_gate_periodic('failure')
            assert not e.armed and not e.periodic_gate_busy and not e.backend.events
        finally:await e.stop()
    asyncio.run(run())


def test_failed_release_still_fences_and_drains_periodic_worker():
    async def run():
        read,entered,release,_=blocked_reader()
        e=SafeExecutor(FakeBackend(),valid_gate,heartbeat_timeout=.08,watchdog_interval=10)
        e.configure_periodic_gate(reader=read);e.arm()
        task=asyncio.create_task(e.inspect_gate_periodic('release_failure'))
        original=e.backend.release_all
        try:
            await until(entered.is_set)
            def fail():raise OSError('offline release failure')
            e.backend.release_all=fail
            with pytest.raises(OSError,match='release failure'):await e.stop()
            assert not e.armed and e.periodic_gate_busy
            e.backend.release_all=original;release.set();await until(lambda:not e.periodic_gate_busy)
            assert await task is None and not e.backend.events
        finally:e.backend.release_all=original;release.set();await e.stop()
    asyncio.run(run())


def test_runner_teardown_restores_raw_and_periodic_inspection_interfaces(tmp_path):
    from test_grind_only import Backend,Frames,Freeze,Sage,profile
    from sage_wow.grind_runner import GrindRunner
    async def run():
        b=Backend();p=profile(tmp_path);frames=Frames(tmp_path)
        from sage_wow.control.executor import GateSnapshot
        from sage_wow.platform.macos.geometry import Rect
        rect=Rect(0,0,500,300)
        raw=lambda:GateSnapshot(7,7,True,rect,rect,True,True)
        e=SafeExecutor(b,raw,heartbeat_timeout=1,watchdog_interval=.01)
        async def stop():r.request_stop('offline_teardown')
        sage=Sage(['world_normal_confirmed'],stop)
        r=GrindRunner(p,tmp_path/'run',sage=sage,executor=e,capture=frames.capture,ocr=frames.ocr,freeze_factory=Freeze)
        await r.run()
        assert e.inspect_gate is raw
        assert e._periodic_reader is e._periodic_observer is e._periodic_context is None
        assert b.closed and not e.armed and not e.periodic_gate_busy
    asyncio.run(run())
