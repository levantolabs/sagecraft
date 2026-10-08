"""Offline input lease/receipt integration; no native backend or provider traffic."""
import asyncio
import json
import time

from sage_wow.grind_runner import GrindRunner
from test_grind_only import cleanup, setup


def test_runner_gate_wrapper_records_callers_and_slow_native_inspection(tmp_path):
    async def scenario():
        controller, frames, sage, backend, store, executor = setup(tmp_path)
        original = executor.inspect_gate
        def slow_gate():
            time.sleep(.055)
            return original()
        executor.inspect_gate = slow_gate
        runner = GrindRunner(controller.profile,tmp_path,sage=sage,executor=executor)
        runner.controller, runner.store = controller, store
        runner.observe_executor_gate()
        try:
            executor.arm()
            assert runner.gate_state() == 'valid'
            runner.drain_executor_timing()
            timings = [json.loads(row[0]) for row in store.connection.execute(
                "SELECT payload_json FROM events WHERE event_type='grind_gate_timing'")]
            assert {row['caller'] for row in timings} == {'_validate_gate','gate_state'}
            assert all(row['elapsed_seconds'] >= .055 and row['valid'] for row in timings)
            assert executor.armed
        finally:
            await cleanup(executor,store)
    asyncio.run(scenario())


def test_stalled_physical_action_keeps_partial_receipt_and_persists_release_timing(tmp_path):
    async def scenario():
        controller, frames, sage, backend, store, executor = setup(tmp_path)
        executor.heartbeat_timeout = .07
        runner = GrindRunner(controller.profile, tmp_path, sage=sage, executor=executor)
        runner.controller, runner.store = controller, store
        frame = frames.capture()  # Prepare the synthetic fixture before its short input lease.
        executor.arm()
        try:
            action = asyncio.create_task(controller.cycle._dispatch_with_receipt(
                {'type': 'keypress', 'keycode': 13, 'hold_seconds': .4},
                frame=frame, request_id='offline-stall',
                session_epoch=controller.cycle.session_epoch,
                candidate_set_version='offline-stall', chosen_option='offline-stall',
                authority_check=lambda: None))  # Explicit fake authority for this private-dispatch fixture.
            await asyncio.sleep(.02)
            assert ('key', 13, True) in backend.events
            executor.set_phase('fake_store_stall')
            time.sleep(.2)
            assert not executor.armed
            result = await action
            assert result.status == 'execution_failed'
            assert result.receipt['possible_input'] and not result.receipt['completed']
            assert result.receipt['generation_after'] > result.receipt['generation_before']
            assert not runner.input_reconciled()
            runner.drain_executor_timing()
            rows = store.connection.execute(
                "SELECT payload_json FROM events WHERE event_type='input_release_timing'").fetchall()
            assert len(rows) == 1
            timing = json.loads(rows[0][0])
            assert timing['execution_id'] == result.receipt['receipt_id']
            assert timing['phase'] == 'fake_store_stall'
            assert timing['release_error'] is None
            await runner.pause()
            assert runner.reason == 'partial_or_unknown_grind_input'
            assert not runner.resume()
        finally:
            await cleanup(executor, store)
    asyncio.run(scenario())
