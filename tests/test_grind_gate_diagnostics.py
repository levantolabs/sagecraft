"""Exact gate failures survive restoration; all backends/providers are fake."""
import asyncio
from dataclasses import asdict, replace
import json
import sqlite3
import threading

import pytest

from sage_wow.control.executor import GateSnapshot, SafeExecutor, ExecutionRejected
from sage_wow.grind_runner import GrindRunner
from sage_wow.platform.macos.geometry import Rect
from test_grind_focus import attached, eventually
from test_grind_only import Backend, Frames, Freeze, Sage, cleanup, profile


def stored_events(directory,kind):
    with sqlite3.connect(f'file:{directory / "events.sqlite3"}?mode=ro',uri=True) as db:
        return [json.loads(row[0]) for row in db.execute(
            'SELECT payload_json FROM events WHERE event_type=? ORDER BY rowid',(kind,))]


@pytest.mark.parametrize('failure,expected',[
    ('geometry',{'bounds_match'}),
    ('missing_window',{'window_present','window_id_matches','foreground','calibrated',
        'client_identity_verified','bounds_present','bounds_match','positive_dimensions'}),
    ('identity',{'client_identity_verified'}),
])
def test_worker_failure_is_durable_even_when_every_runner_recheck_is_valid(tmp_path,failure,expected):
    async def run():
        p=profile(tmp_path);frames=Frames(tmp_path);backend=Backend()
        rect=Rect(0,0,500,300);good=GateSnapshot(7,7,True,rect,rect,True,True)
        bad={'geometry':replace(good,bounds=Rect(1,0,500,300)),
            'missing_window':GateSnapshot(None,7,False,None,rect,False,False),
            'identity':replace(good,client_identity_verified=False,
                identity_evidence={'reason':'bundle_path_missing','window':{'owner_pid':42},
                    'lookups':[{'application_present':True}],
                    'detected_bundle_id':'test.game','detected_path':None})}[failure]
        fault_thread=[None];loop_checks=[]
        def inspect():
            if threading.get_ident()==fault_thread[0]:return bad
            loop_checks.append(True);return good
        entered=asyncio.Event()
        async def blocked():entered.set();await asyncio.Event().wait()
        sage=Sage([],blocked)
        executor=SafeExecutor(backend,inspect,heartbeat_timeout=30,watchdog_interval=.01)
        directory=tmp_path/'run'
        runner=GrindRunner(p,directory,sage=sage,executor=executor,
            capture=frames.capture,ocr=frames.ocr,freeze_factory=Freeze)
        task=asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(entered.wait(),2)
            event_thread=threading.get_ident();written=[];append=runner.store.append
            def append_on_loop(event):
                assert threading.get_ident()==event_thread
                written.append(event.event_type);append(event)
            runner.store.append=append_on_loop
            def trip_worker():
                fault_thread[0]=threading.get_ident()
                try:
                    with pytest.raises(ExecutionRejected):executor._validate_gate()
                finally:fault_thread[0]=None
            await asyncio.to_thread(trip_worker)
            assert executor.inspect_gate()==good
            await asyncio.wait_for(task,2)
            status=json.loads((directory/'status.json').read_text())
            assert status['state']=='STOPPED' and status['reason']=='focus_or_calibration_invalid'
            proof=status['gate_failure'];observation=proof['observation']
            assert proof['source']=='executor_disarmed_gate_check'
            assert proof['executor_disarm_reason']=='focus_or_calibration_invalid'
            assert proof['invalid_gate']==observation['snapshot']==asdict(bad)
            if failure=='identity':
                assert proof['invalid_gate']['identity_evidence']==bad.identity_evidence
                assert proof['invalid_gate']['identity_evidence']['reason']=='bundle_path_missing'
            assert observation['caller']=='_validate_gate'
            assert observation['thread_name']!=threading.current_thread().name
            assert not observation['valid'] and not observation['valid_with_foreground']
            assert set(observation['failed_predicates'])==expected
            assert observation['observed_at']<=proof['stopped_at']
            assert observation['monotonic_at']>0
            assert stored_events(directory,'grind_gate_failure')==[proof]
            assert stored_events(directory,'session_stopped')[-1]['gate_failure']==proof
            assert 'grind_gate_failure' in written and loop_checks
            assert len(sage.calls)==1 and not executor.armed
            assert not any(event[0] in {'key','text'} for event in backend.events)
        finally:
            if not task.done():runner.request_stop();await asyncio.wait_for(task,2)
    asyncio.run(run())


def test_focus_only_observation_still_soft_pauses_without_hard_gate_failure(tmp_path):
    async def run():
        p=profile(tmp_path);frames=Frames(tmp_path);backend=Backend()
        rect=Rect(0,0,500,300);good=GateSnapshot(7,7,True,rect,rect,True,True);gate=[good]
        entered=asyncio.Event()
        async def blocked():entered.set();await asyncio.Event().wait()
        sage=Sage([],blocked)
        executor=SafeExecutor(backend,lambda:gate[0],heartbeat_timeout=30,watchdog_interval=.01)
        directory=tmp_path/'run'
        runner=GrindRunner(p,directory,sage=sage,executor=executor,
            capture=frames.capture,ocr=frames.ocr,freeze_factory=Freeze)
        task=asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(entered.wait(),2)
            gate[0]=replace(good,foreground=False)
            await eventually(lambda:runner.state=='PAUSED_FOCUS')
            observed=runner.invalid_gate_observation
            assert observed['snapshot']==asdict(gate[0])
            assert observed['failed_predicates']==['foreground']
            assert observed['valid_with_foreground'] and not observed['valid']
            assert not runner.stopping and runner.gate_failure is None and not executor.armed
            runner.request_stop();await asyncio.wait_for(task,2)
            assert not stored_events(directory,'grind_gate_failure')
            status=json.loads((directory/'status.json').read_text())
            assert status['reason']=='operator_stop' and status['gate_failure'] is None
        finally:
            if not task.done():runner.request_stop();await asyncio.wait_for(task,2)
    asyncio.run(run())


def test_unknown_gate_stop_does_not_invent_a_snapshot_from_valid_recheck(tmp_path):
    async def run():
        runner,c,frames,sage,backend,store,executor=attached(tmp_path)
        try:
            assert executor.inspect_gate().valid and runner.invalid_gate is None
            runner.request_stop('focus_or_calibration_invalid',source='executor_disarmed_gate_check')
            runner.state='STOPPED';await runner.status()
            proof=json.loads((tmp_path/'status.json').read_text())['gate_failure']
            assert proof['invalid_gate'] is None and proof['observation'] is None
            assert proof['source']=='executor_disarmed_gate_check'
            assert runner.stopping and c.stopped
        finally:await cleanup(executor,store)
    asyncio.run(run())


def test_later_focus_only_failure_does_not_replace_latched_hard_snapshot(tmp_path):
    async def run():
        runner,c,frames,sage,backend,store,executor=attached(tmp_path)
        try:
            good=executor.inspect_gate();gate=[replace(good,calibrated=False)]
            executor.inspect_gate=lambda:gate[0];runner.observe_executor_gate()
            await asyncio.to_thread(executor.inspect_gate)
            first=runner.invalid_gate_observation
            gate[0]=replace(good,foreground=False)
            await asyncio.to_thread(executor.inspect_gate)
            assert runner.invalid_gate_observation is first
            assert runner.invalid_gate==replace(good,calibrated=False)
            runner.request_stop('focus_or_calibration_invalid',source='resume_latched_gate')
            assert runner.gate_failure['observation']==first
            assert runner.gate_failure['invalid_gate']==asdict(replace(good,calibrated=False))
        finally:await cleanup(executor,store)
    asyncio.run(run())


def test_diagnostic_write_failure_cannot_prevent_existing_stop_transition(tmp_path):
    async def run():
        runner,c,frames,sage,backend,store,executor=attached(tmp_path)
        try:
            good=executor.inspect_gate()
            executor.inspect_gate=lambda:replace(good,calibrated=False)
            runner.observe_executor_gate();executor.inspect_gate()
            def unavailable(*args):raise OSError('offline diagnostic write failure')
            runner.event=unavailable
            with pytest.raises(OSError,match='diagnostic write failure'):
                runner.request_stop('focus_or_calibration_invalid',source='runner_gate_check')
            assert runner.stopping and c.stopped
            assert runner.reason=='focus_or_calibration_invalid' and runner.gate_failure
            runner.state='STOPPED';await runner.status()
            assert json.loads((tmp_path/'status.json').read_text())['gate_failure']==runner.gate_failure
        finally:await cleanup(executor,store)
    asyncio.run(run())
