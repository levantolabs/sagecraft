"""Fixed previous-target motor: fake input/capture, no network or native runner."""
import asyncio
import socket
from types import SimpleNamespace

import pytest
from sage_wow.agent.cycle import DecisionCycle
from sage_wow.agent.grind_perception import carry_target_observation
from sage_wow.control.executor import ExecutionRejected, GateSnapshot, SafeExecutor
from sage_wow.models import Frame
from sage_wow.storage import EventStore
from test_executor import FakeBackend, valid_gate

BINDING={'type':'target_previous','expected_target_name':'Ragged Young Wolf'}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('SAGE_WOW_DISABLE_LIVE_INPUT','1')
    monkeypatch.setenv('SAGE_WOW_DISABLE_LIVE_CAPTURE','1')
    monkeypatch.setattr(socket.socket,'connect',lambda *args:pytest.fail('unintended network'))


def authorized(cycle):
    return cycle.execute_authorized(BINDING,frame=Frame.create('offline',400,300),
        chosen_option='target_recent_corpse',authorization_id='prior-sage-loot',
        request_id='offline-request',candidate_set_version='fixture')


def test_previous_target_dispatches_only_fixed_command_and_records_all_five_native_steps(tmp_path):
    backend=FakeBackend();executor=SafeExecutor(backend,valid_gate,heartbeat_timeout=3)
    store=EventStore(tmp_path/'receipts.sqlite3');cycle=DecisionCycle(SimpleNamespace(),executor,store)
    async def run():
        executor.arm()
        try:return await authorized(cycle)
        finally:await executor.stop()
    try:result=asyncio.run(run())
    finally:store.close()
    assert result.status=='dispatched' and result.receipt['completed']
    assert result.receipt['generation_after']==5
    assert [step['kind'] for step in result.receipt['input_steps']]==['key_down','key_up','text','key_down','key_up']
    assert all(step['status']=='completed' for step in result.receipt['input_steps'])
    assert [event[1] for event in backend.events if event[0]=='text']==['/targetlasttarget [noexists]']
    assert result.execution['selection_only'] and result.execution['selection_result'].startswith('unverified')
    cache={'generation':0};controller=SimpleNamespace(target_observation=cache)
    assert not carry_target_observation(controller,result.receipt)
    assert cache=={'generation':0}


@pytest.mark.parametrize('change',[{'expected_target_name':None},{'expected_target_name':''},
    {'expected_target_name':' Ragged Young Wolf'},{'expected_target_name':'Wolf\n/cast Smite'},
    {'expected_target_name':'Wolf; /targetenemy'},{'command':'/targetenemy'},
    {'selector':'enemy'},{'start_attack':True}])
def test_previous_target_rejects_invalid_data_or_extra_fields_before_native_input(change):
    backend=FakeBackend();executor=SafeExecutor(backend,valid_gate)
    async def run():
        executor.arm()
        try:
            with pytest.raises(ExecutionRejected):await executor.execute({**BINDING,**change})
        finally:await executor.stop()
    asyncio.run(run());assert not backend.events


def test_previous_target_partial_native_failure_is_unreconciled_and_does_not_cast(tmp_path):
    backend=FakeBackend()
    def fail_text(value):
        backend.events.append(('text',value));raise RuntimeError('injected text effect unknown')
    backend.text=fail_text
    executor=SafeExecutor(backend,valid_gate,heartbeat_timeout=3)
    store=EventStore(tmp_path/'partial.sqlite3');cycle=DecisionCycle(SimpleNamespace(),executor,store)
    async def run():
        executor.arm()
        try:return await authorized(cycle)
        finally:await executor.stop()
    try:result=asyncio.run(run())
    finally:store.close()
    assert result.status=='execution_failed'
    assert result.receipt['possible_input'] and result.receipt['dispatch_unknown']
    assert not result.receipt['completed'] and result.receipt['error']
    assert result.receipt['generation_after']==3
    assert [event[1] for event in backend.events if event[0]=='text']==['/targetlasttarget [noexists]']
    assert not executor.armed


def test_previous_target_focus_loss_before_submission_remains_failure(tmp_path):
    backend=FakeBackend();base=valid_gate();active={'yes':True}
    def gate():return GateSnapshot(base.window_id,base.calibrated_window_id,active['yes'],
        base.bounds,base.calibrated_bounds,True,True)
    def text(value):
        backend.events.append(('text',value));active['yes']=False
    backend.text=text
    executor=SafeExecutor(backend,gate,heartbeat_timeout=3)
    store=EventStore(tmp_path/'focus.sqlite3');cycle=DecisionCycle(SimpleNamespace(),executor,store)
    async def run():
        executor.arm()
        try:return await authorized(cycle)
        finally:await executor.stop()
    try:result=asyncio.run(run())
    finally:store.close()
    assert result.status=='execution_failed' and not result.receipt['completed']
    assert len(result.receipt['input_steps'])==3
    assert not executor.armed
