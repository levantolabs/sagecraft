"""Offline first-down scheduling and post-yield authority regressions."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import socket
import subprocess
import time
import threading

import pytest

from sage_wow.agent.cycle import ActionCandidate, DecisionCycle, DispatchValidation, TravelDecisionBudget
from sage_wow.control.executor import ExecutionRejected, SafeExecutor
from sage_wow.models import Frame
from sage_wow.storage import EventStore
from test_executor import FakeBackend, valid_gate
from test_decision_cycle import FakeSage


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def deny(*a, **k):
        raise AssertionError('first-input tests prohibit network and child processes')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)
    monkeypatch.setattr(socket.socket, 'sendto', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr(subprocess, 'Popen', deny)


def during_boundary(executor, mutate):
    original = executor._prepare_first_down
    async def prepare(guard=None):
        task = asyncio.current_task()
        asyncio.get_running_loop().call_soon(mutate, task)
        return await original(guard)
    executor._prepare_first_down = prepare


def downs(backend):
    return [event for event in backend.events if event[0] == 'key' and event[-1] is True]


@pytest.mark.parametrize('kind', ['keypress', 'keypress_chord'])
@pytest.mark.parametrize('change', ['focus', 'identity', 'window', 'calibration', 'lease', 'context', 'stop', 'stale', 'cancel'])
def test_first_down_rejects_boundary_invalidation_without_phantom_up(kind, change):
    backend = FakeBackend(); gate = [valid_gate()]; context = {'generation':0}
    executor = SafeExecutor(backend, lambda:gate[0])
    executor.configure_periodic_gate(context=lambda:dict(context))
    def mutate(task):
        if change == 'focus': gate[0] = replace(gate[0], foreground=False)
        elif change == 'identity': gate[0] = replace(gate[0], client_identity_verified=False)
        elif change == 'window': gate[0] = replace(gate[0], window_id=99)
        elif change == 'calibration': gate[0] = replace(gate[0], calibrated=False)
        elif change == 'lease': executor._gate_lease += 1
        elif change == 'context': context['generation'] += 1
        elif change == 'stop': executor._stopped.set()
        elif change == 'stale': executor._last_heartbeat -= 1
        elif change == 'cancel': task.cancel()
    during_boundary(executor, mutate)
    binding = {'type':kind,'hold_seconds':.01, **({'keycode':13} if kind=='keypress' else {'keycodes':[13,49]})}
    async def run():
        executor.arm()
        try:
            with pytest.raises((ExecutionRejected, asyncio.CancelledError)):
                await executor.execute(binding)
            assert backend.events == []  # No down and no synthetic up.
        finally:
            await executor.stop()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['keypress', 'keypress_chord'])
def test_normal_boundary_does_not_manufacture_heartbeat_or_yield_while_held(kind):
    backend = FakeBackend(); executor = SafeExecutor(backend,valid_gate)
    checks = []; prior = executor._prepare_first_down
    async def prepare(guard=None):
        assert not executor._held_since
        stamp = executor._last_heartbeat
        await prior(guard)
        checks.append(executor._last_heartbeat == stamp)
    executor._prepare_first_down = prepare
    binding={'type':kind,'hold_seconds':.01,**({'keycode':13} if kind=='keypress' else {'keycodes':[13,49]})}
    async def run():
        executor.arm()
        try:
            result=await executor.execute(binding)
            assert result['dispatched'] and checks == [True]
            assert len(downs(backend)) == (1 if kind=='keypress' else 2)
        finally: await executor.stop()
    asyncio.run(run())


def test_expired_before_boundary_cannot_be_renewed_by_late_ordinary_pulse(monkeypatch):
    backend=FakeBackend(); executor=SafeExecutor(backend,valid_gate)
    monkeypatch.setattr(executor,'_watch_heartbeat',lambda stop:stop.wait())
    async def run():
        executor.arm(); executor._last_heartbeat -= 1
        asyncio.get_running_loop().call_soon(executor.heartbeat)
        try:
            with pytest.raises(ExecutionRejected):
                await executor.execute({'type':'keypress','keycode':13,'hold_seconds':.01})
            await asyncio.sleep(.001)
            assert not executor.armed and not downs(backend)
        finally: await executor.stop()
    asyncio.run(run())


@pytest.mark.parametrize('slow', ['native', 'guard', 'persistence', 'backend'])
def test_slow_final_work_preserves_expiry_and_partial_accounting(slow):
    backend=FakeBackend(); count=[0]
    def gate():
        count[0]+=1
        if slow=='native' and count[0]==3: time.sleep(.12)
        return valid_gate()
    executor=SafeExecutor(backend,gate,heartbeat_timeout=.06,watchdog_interval=.005)
    def guard():
        if slow=='guard': time.sleep(.12)
    if slow=='persistence':
        executor.set_physical_step_callback(lambda step:time.sleep(.12) if step['kind']=='key_down' else None)
    if slow=='backend': backend.on_key_down=lambda:time.sleep(.12)
    async def run():
        executor.arm()
        try:
            with pytest.raises(ExecutionRejected) as error:
                await executor.execute({'type':'keypress','keycode':13,'hold_seconds':.03},pre_dispatch_guard=guard)
            assert not executor.armed
            assert len(downs(backend)) == (1 if slow=='backend' else 0)
            if slow=='backend':
                assert any(s['kind']=='key_down' for s in error.value.input_steps)
                assert executor.last_watchdog_trip
        finally: await executor.stop()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['epoch','generation','scope_fields','scope_object','scope_current',
    'original_frame','dispatch_frame','binding','wire','decision','precondition','travel_deadline','travel_mutation',
    'callback_binding','callback_scope','current_callback_binding','receipt','cancel'])
def test_decision_authority_is_rechecked_after_first_down_yield(tmp_path, change):
    backend=FakeBackend(); executor=SafeExecutor(backend,valid_gate)
    sage=FakeSage('go'); store=EventStore(tmp_path/'events.sqlite3')
    cycle=DecisionCycle(sage,executor,store,session_epoch='original-epoch')
    original=Frame.create('fixture',800,600,image_data=b'original'); fresh=Frame.create('fixture',800,600,image_data=b'fresh')
    binding={'type':'keypress','keycode':13,'hold_seconds':.01}
    current=[True]; condition=[True]; at_boundary=[False]; scope_holder=[]; decision_holder=[]
    original_decide=sage.decide_image_choice
    async def decide(*a,**k):
        answer=await original_decide(*a,**k);decision_holder.append(answer);return answer
    sage.decide_image_choice=decide
    budget=TravelDecisionBudget(time.time(),time.time()+5)
    def precondition():
        if at_boundary[0] and change=='callback_binding': binding['keycode']=99
        if at_boundary[0] and change=='callback_scope': scope_holder[0].deadline_epoch += 100
        return condition[0]
    def is_current():
        if at_boundary[0] and change=='current_callback_binding': binding['keycode']=99
        return current[0]
    async def guard(frame): return DispatchValidation(True,fresh)
    def mutate(task):
        at_boundary[0]=True
        if change=='epoch': cycle.session_epoch='new-epoch'
        elif change=='generation': cycle._input_generation+=1
        elif change=='scope_fields': scope_holder[0].objective_revision+=1
        elif change=='scope_object': cycle._execution_scope.set(replace(scope_holder[0]))
        elif change=='scope_current': current[0]=False
        elif change in {'original_frame','dispatch_frame'}:
            object.__setattr__(original if change=='original_frame' else fresh,'captured_at',
                               (datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat())
        elif change=='binding': binding['keycode']=99
        elif change=='wire': decision_holder[0].envelope.action_bindings['go']['keycode']=99
        elif change=='decision': object.__setattr__(decision_holder[0],'chosen','different')
        elif change=='precondition': condition[0]=False
        elif change=='travel_deadline': object.__setattr__(budget,'deadline',time.time()+.005)
        elif change=='travel_mutation': object.__setattr__(budget,'deadline',time.time()+50)
        elif change=='receipt': cycle._active_receipt['generation_before']=999
        elif change=='cancel': task.cancel()
    # ContextVar changes must be made in the action task's own context.
    if change=='scope_object':
        original_prepare=executor._prepare_first_down
        async def prepare(check=None):
            def replace_scope():
                cycle._execution_scope.set(replace(scope_holder[0]))
            # The synchronous guard runs inside the execution task after await.
            def invalidating_check():
                replace_scope(); return check()
            return await original_prepare(invalidating_check)
        executor._prepare_first_down=prepare
    else: during_boundary(executor,mutate)
    async def run():
        executor.arm()
        try:
            with cycle.scoped_execution_scope(task_id='task',objective_revision=1,session_epoch=cycle.session_epoch,
                    deadline_epoch=time.time()+10,max_frame_age_seconds=5,is_current=is_current) as scope:
                scope_holder.append(scope)
                call=cycle.decide_and_execute(original,'fake.jpg','context','instructions',
                    [ActionCandidate('go','go',binding,precondition,guard),
                     ActionCandidate('observe','observe',{'type':'observe_only'})],travel_budget=budget)
                if change=='cancel':
                    with pytest.raises(asyncio.CancelledError): await call
                    receipt=cycle.last_receipt
                    assert receipt['dispatch_unknown'] and receipt['possible_input'] and not receipt['completed']
                    assert receipt['input_steps'][0]['kind']=='legacy_executor_dispatch'
                else:
                    result=await call
                    assert result.status=='execution_failed',result
                    assert not result.receipt['completed']
            assert backend.events==[]
        finally:
            await executor.stop();store.close()
    asyncio.run(run())


def test_authorized_continuation_uses_its_own_source_age_limit(tmp_path):
    backend=FakeBackend(); executor=SafeExecutor(backend,valid_gate)
    store=EventStore(tmp_path/'events.sqlite3');cycle=DecisionCycle(FakeSage(),executor,store)
    frame=Frame.create('fixture',800,600)
    during_boundary(executor,lambda task:object.__setattr__(frame,'captured_at',
        (datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat()))
    async def run():
        executor.arm()
        try:
            result=await cycle.execute_authorized({'type':'keypress','keycode':13,'hold_seconds':.01},
                frame=frame,chosen_option='go',authorization_id='actual-prior',request_id='request',
                candidate_set_version='fixed',max_frame_age_seconds=.5)
            assert result.status=='execution_failed' and not downs(backend)
        finally: await executor.stop();store.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['keypress','keypress_chord'])
def test_cumulative_native_checks_allow_ordinary_heartbeat_before_down(kind):
    backend=FakeBackend(); reads=[0]; pulses=[]
    def gate():
        if executor.armed and threading.current_thread() is threading.main_thread():
            reads[0]+=1
            delays = [.068,.265] if kind=='keypress' else [.068,.02,.245]
            if reads[0]<=len(delays): time.sleep(delays[reads[0]-1])
        return valid_gate()
    executor=SafeExecutor(backend,gate,heartbeat_timeout=.75,watchdog_interval=.01)
    def on_down():
        assert len(pulses)>=2  # Real ordinary timer ran during preparation.
        time.sleep(.04)
    backend.on_key_down=on_down
    async def run():
        executor.arm()
        async def pulse():
            while executor.armed:
                executor.heartbeat();pulses.append(time.monotonic());await asyncio.sleep(.15)
        task=asyncio.create_task(pulse());await asyncio.sleep(.001)
        time.sleep(.4)
        try:
            binding={'type':kind,'hold_seconds':.01,**({'keycode':13} if kind=='keypress' else {'keycodes':[13,49]})}
            assert (await executor.execute(binding))['dispatched']
            assert executor.last_watchdog_trip is None
        finally:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
            await executor.stop()
    asyncio.run(run())


