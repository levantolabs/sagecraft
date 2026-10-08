import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sage_wow.control.executor import ExecutionRejected, GateSnapshot, SafeExecutor
from sage_wow.platform.macos.geometry import Rect
from sage_wow.platform.macos.input import InputError, QuartzInput


class FakeBackend:
    def __init__(self):
        self.events = []
        self.releases = 0
        self.on_key_down = None

    def key(self, keycode, down):
        self.events.append(("key", keycode, down))
        if down and self.on_key_down:
            self.on_key_down()

    def mouse_button(self, x, y, button, down):
        self.events.append(("mouse", x, y, button, down))

    def release_all(self):
        self.releases += 1

    def text(self, value):
        self.events.append(("text", value))


class HoverBackend(FakeBackend):
    def mouse_move(self, x, y):
        self.events.append(("move", x, y))


def valid_gate():
    rect = Rect(-1200, 40, 1000, 600)
    return GateSnapshot(10, 10, True, rect, rect, True, True)


def test_live_native_input_backend_is_denied_by_test_guard(monkeypatch):
    monkeypatch.setattr("sage_wow.platform.macos.input.sys.platform", "darwin")
    with pytest.raises(InputError, match="SAGE_WOW_DISABLE_LIVE_INPUT"):
        QuartzInput()


def test_native_input_ownership_lock_excludes_second_process(tmp_path, monkeypatch):
    monkeypatch.delenv("SAGE_WOW_DISABLE_LIVE_INPUT", raising=False)
    monkeypatch.setattr("sage_wow.platform.macos.input.Path.home", lambda: tmp_path)
    monkeypatch.setattr("sage_wow.platform.macos.input.sys.platform", "darwin")
    monkeypatch.setitem(sys.modules, "Quartz", object())
    owner = QuartzInput()
    helper = r"""
import builtins
import sys
from pathlib import Path
import sage_wow.platform.macos.input as native
native.sys.platform = "darwin"
native.Path.home = lambda: Path(sys.argv[1])
original_import = builtins.__import__
def reject_quartz(name, *args, **kwargs):
    if name == "Quartz":
        raise AssertionError("ownership failure must happen before native import")
    return original_import(name, *args, **kwargs)
builtins.__import__ = reject_quartz
try:
    native.QuartzInput()
except native.InputError as exc:
    if "owns native input" not in str(exc):
        raise
else:
    raise AssertionError("second process acquired native input")
"""
    env = os.environ.copy()
    env.pop("SAGE_WOW_DISABLE_LIVE_INPUT", None)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, env.get("PYTHONPATH"))))
    try:
        result = subprocess.run([sys.executable, "-c", helper, str(tmp_path)], env=env, text=True,
                                capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
    finally:
        owner.close()


def test_click_maps_calibrated_retina_image_pixels_and_releases_button():
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate)

    async def run():
        executor.arm()
        executor.heartbeat()
        result = await executor.execute({"type": "click", "image_x": 1500, "image_y": 600, "button": 0}, frame_size=(2000, 1200))
        await executor.stop()
        return result

    result = asyncio.run(run())
    assert (result["desktop_x"], result["desktop_y"]) == (-450, 340)
    assert backend.events == [("mouse", -450, 340, 0, True), ("mouse", -450, 340, 0, False)]


def test_guarded_hover_moves_only_and_bounds_tooltip_pause():
    backend = HoverBackend()
    executor = SafeExecutor(backend, valid_gate)

    async def run():
        executor.arm()
        executor.heartbeat()
        result = await executor.execute({"type": "hover", "image_x": 1500, "image_y": 600,
                                         "pause_seconds": .01}, frame_size=(2000, 1200))
        await executor.stop()
        return result

    result = asyncio.run(run())
    assert result["kind"] == "hover" and result["dispatched"]
    assert (result["desktop_x"], result["desktop_y"]) == (-450, 340)
    assert backend.events == [("move", -450, 340)]


def test_hover_rejects_out_of_frame_points():
    executor = SafeExecutor(HoverBackend(), valid_gate)

    async def run():
        executor.arm()
        executor.heartbeat()
        with pytest.raises(ExecutionRejected, match="outside the captured image"):
            await executor.execute({"type": "hover", "image_x": 2000, "image_y": 4}, frame_size=(2000, 1200))
        await executor.stop()

    asyncio.run(run())


def test_lost_focus_releases_a_held_key_and_disarms():
    backend = FakeBackend()
    active = {"foreground": True}
    original = valid_gate()

    def gate():
        return GateSnapshot(original.window_id, original.calibrated_window_id, active["foreground"], original.bounds,
                            original.calibrated_bounds, original.calibrated, original.client_identity_verified)

    backend.on_key_down = lambda: active.update(foreground=False)
    executor = SafeExecutor(backend, gate)

    async def run():
        executor.arm()
        executor.heartbeat()
        with pytest.raises(ExecutionRejected, match="lost focus"):
            await executor.execute({"type": "keypress", "keycode": 13, "hold_seconds": 0.01})

    asyncio.run(run())
    assert ("key", 13, False) in backend.events
    assert not executor.armed
    assert backend.releases >= 2


def test_watchdog_releases_and_disarms_when_supervisor_heartbeat_stops():
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=0.08, watchdog_interval=0.01)

    async def run():
        executor.arm()
        await asyncio.sleep(0.13)
        return executor.armed, executor.disarm_reason

    armed, reason = asyncio.run(run())
    assert not armed
    assert reason == "controller_heartbeat_lost"
    assert backend.releases >= 2


@pytest.mark.parametrize('stall_kind', ['cpu', 'io'])
def test_watchdog_releases_held_key_during_synchronous_event_loop_stall(stall_kind, record_property):
    class HeldKeyBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.held = set()
            self.released_at = None
            self.key_down = threading.Event()

        def key(self, keycode, down):
            super().key(keycode, down)
            if down:
                self.held.add(keycode)
                self.key_down.set()
            else:
                self.held.discard(keycode)

        def release_all(self):
            super().release_all()
            if self.held:
                self.held.clear()
                self.released_at = time.monotonic()

    backend = HeldKeyBackend()
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=0.07, watchdog_interval=0.01)

    async def run():
        executor.arm()
        action = asyncio.create_task(executor.execute(
            {"type": "keypress", "keycode": 13, "hold_seconds": 0.4}))
        await asyncio.sleep(0.02)
        assert backend.key_down.is_set()
        assert backend.held == {13}
        executor.set_phase('fake_'+stall_kind+'_stall')
        # This blocks the same loop that runs the controller pulse and the
        # focus watchdog. The independent heartbeat watcher must release now.
        if stall_kind == 'io':
            time.sleep(0.25)
        else:
            until = time.monotonic() + .25
            while time.monotonic() < until:
                sum(range(1000))
        resumed_at = time.monotonic()
        assert backend.released_at is not None
        assert backend.released_at < resumed_at
        assert not backend.held
        assert not executor.armed
        assert executor.disarm_reason == "controller_heartbeat_lost"
        trip = executor.last_watchdog_trip
        assert trip['source'] == 'watchdog_thread'
        assert trip['phase'] == 'fake_'+stall_kind+'_stall'
        assert .07 <= trip['heartbeat_to_release_seconds'] < .20
        assert trip['release_started_at_monotonic'] <= backend.released_at <= trip['release_attempt_completed_at_monotonic']
        assert trip['held_inputs'][0]['code'] == 13
        executor.heartbeat(loop_lag_seconds=.25)
        assert not executor.armed  # Late pulse cannot restore input authority.
        with pytest.raises(ExecutionRejected, match='still active'):
            executor.arm()
        with pytest.raises(ExecutionRejected):
            await action
        before = list(backend.events)
        with pytest.raises(ExecutionRejected):
            await executor.execute({'type':'keypress','keycode':14,'hold_seconds':.01})
        assert backend.events == before
        watcher = executor._heartbeat_watch_thread
        await executor.stop()
        assert not watcher.is_alive()

    asyncio.run(run())
    record_property('heartbeat_to_release_seconds', executor.last_watchdog_trip['heartbeat_to_release_seconds'])
    record_property('release_duration_seconds', executor.last_watchdog_trip['release_duration_seconds'])


def test_late_heartbeat_cannot_mask_expiry_when_watcher_is_not_scheduled(monkeypatch):
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=.07)
    # Deliberately delay the watcher: loop-side lease renewal must still fail.
    monkeypatch.setattr(executor, '_watch_heartbeat', lambda stop: stop.wait())
    async def run():
        executor.arm()
        executor._last_heartbeat -= .1
        executor.heartbeat()
        assert not executor.armed
        assert executor.last_watchdog_trip['source'] == 'late_heartbeat'
        with pytest.raises(ExecutionRejected):
            await executor.execute({'type':'keypress','keycode':13,'hold_seconds':.01})
        assert not backend.events
        await executor.stop()
    asyncio.run(run())


def test_stalled_receipt_callback_releases_first_key_and_blocks_later_chord_down():
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=.07, watchdog_interval=.01)
    steps = []
    def receipt(step):
        steps.append(step)
        if step['kind'] == 'key_down' and step['details']['keycode'] == 49:
            time.sleep(.2)  # Synchronous persistence before the next primitive.
            assert not executor.armed
    executor.set_physical_step_callback(receipt)
    async def run():
        executor.arm()
        with pytest.raises(ExecutionRejected) as caught:
            await executor.execute({'type':'keypress_chord','keycodes':[13,49],'hold_seconds':.01})
        assert [('key',13,True)] == [event for event in backend.events if event[-1] is True]
        assert caught.value.input_steps == steps
        assert steps[1]['status'] == 'blocked_no_input'
        assert executor.last_watchdog_trip['held_inputs'][0]['code'] == 13
        await executor.stop()
    asyncio.run(run())


def test_independent_release_failure_is_retained_and_does_not_restore_authority():
    class ReleaseFailure(FakeBackend):
        def release_all(self):
            super().release_all()
            if self.releases > 1:
                raise RuntimeError('fake release failed')
    backend = ReleaseFailure()
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=.04, watchdog_interval=.005)
    async def run():
        executor.arm()
        time.sleep(.12)
        assert not executor.armed
        trip = executor.drain_watchdog_events()[0]
        assert trip['reason'] == 'controller_heartbeat_lost'
        assert trip['release_error'] == 'RuntimeError'
        assert trip['release_attempt_completed_at_monotonic'] >= trip['release_started_at_monotonic']
        executor.heartbeat()
        assert not executor.armed
        watcher = executor._heartbeat_watch_thread
        with pytest.raises(RuntimeError, match='release failed'):
            await executor.stop()
        assert not watcher.is_alive()
    asyncio.run(run())


def test_watchdog_stack_identifies_stalled_gate_after_releasing_held_input():
    class HeldBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.held = False
            self.released_at = None
        def key(self, keycode, down):
            super().key(keycode, down)
            self.held = down
        def release_all(self):
            super().release_all()
            if self.held:
                self.held = False
                self.released_at = time.monotonic()
    backend = HeldBackend()
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=.07, watchdog_interval=.01)
    def blocking_native_gate():
        time.sleep(.22)  # Fake native gate stalls the event-loop thread.
        return valid_gate()
    async def run():
        executor.arm()
        action = asyncio.create_task(executor.execute({'type':'keypress','keycode':13,'hold_seconds':.4}))
        await asyncio.sleep(.02)
        executor.set_phase('awaited_ocr_is_not_the_actual_blocker')
        executor.inspect_gate_measured(blocking_native_gate,'gate_state')
        resumed = time.monotonic()
        assert not executor.armed and not backend.held
        trip = executor.last_watchdog_trip
        assert trip['gate_inflight']['caller'] == 'gate_state'
        stack = trip['event_loop_stack']
        assert 1 <= len(stack['frames']) <= 32
        assert stack['frames'][-1]['function'] == 'blocking_native_gate'
        assert all(set(frame) == {'file','line','function'} for frame in stack['frames'])
        assert backend.released_at <= stack['sampled_at_monotonic'] < resumed
        assert trip['heartbeat_to_release_seconds'] < .2
        gate = executor.drain_gate_events()[0]
        assert gate['caller'] == 'gate_state' and gate['elapsed_seconds'] >= .22
        assert executor.timing_snapshot()['gate_checks']['gate_state']['count'] == 1
        with pytest.raises(ExecutionRejected):
            await action
        await executor.stop()
    asyncio.run(run())


def test_keypress_chord_holds_exact_two_keys_together_and_releases_them():
    backend=FakeBackend();executor=SafeExecutor(backend,valid_gate)
    async def run():
        executor.arm();executor.heartbeat()
        result=await executor.execute({'type':'keypress_chord','keycodes':[13,49],'hold_seconds':.01})
        await executor.stop()
        return result
    result=asyncio.run(run())
    assert result=={'dispatched':True,'kind':'keypress_chord','keycodes':[13,49],'duration_seconds':.01}
    assert backend.events[:4]==[('key',13,True),('key',49,True),('key',49,False),('key',13,False)]
    assert backend.releases>=2


def test_physical_step_callback_records_unique_primitives_and_completion_status():
    backend=FakeBackend();executor=SafeExecutor(backend,valid_gate)
    observed=[];executor.set_physical_step_callback(observed.append)
    async def run():
        executor.arm();executor.heartbeat()
        await executor.execute({'type':'keypress_chord','keycodes':[13,49],'hold_seconds':.01})
        await executor.stop()
    asyncio.run(run())
    assert [step['kind'] for step in observed]==['key_down','key_down','key_up','key_up']
    assert len({step['step_id'] for step in observed})==4
    assert all(step['attempted'] and step['status']=='completed' for step in observed)


def test_failed_primitive_and_cleanup_failure_preserve_partial_input_trace():
    class CleanupFailure(FakeBackend):
        def release_all(self):
            self.releases+=1
            if self.releases>1:
                raise RuntimeError('fake cleanup failure')
    backend=CleanupFailure();executor=SafeExecutor(backend,valid_gate)
    observed=[];executor.set_physical_step_callback(observed.append)
    async def run():
        executor.arm();executor.heartbeat()
        with pytest.raises(RuntimeError,match='fake cleanup failure') as caught:
            await executor.execute({'type':'keypress','keycode':13,'hold_seconds':.01})
        return caught.value
    error=asyncio.run(run())
    assert [step['kind'] for step in observed]==['key_down','key_up']
    assert error.input_steps is observed or error.input_steps==observed
    assert all(step['status']=='completed' for step in error.input_steps)


@pytest.mark.parametrize('failure_phase,expected',[
    ('first', ['failed_effect_unknown', 'completed']),
    ('release', ['completed', 'failed_effect_unknown']),
])
def test_backend_failure_trace_distinguishes_possible_from_confirmed_primitives(failure_phase, expected):
    class FailingKey(FakeBackend):
        def key(self,keycode,down):
            self.events.append(('key',keycode,down))
            if failure_phase=='first' and down:
                raise RuntimeError('key down failed')
            if failure_phase=='release' and not down:
                raise RuntimeError('key up failed')
    backend=FailingKey();executor=SafeExecutor(backend,valid_gate);observed=[]
    executor.set_physical_step_callback(observed.append)
    async def run():
        executor.arm();executor.heartbeat()
        with pytest.raises(RuntimeError):
            await executor.execute({'type':'keypress','keycode':13,'hold_seconds':.01})
    asyncio.run(run())
    assert [step['status'] for step in observed]==expected


def test_observe_only_has_no_physical_step_attempts():
    backend=FakeBackend();executor=SafeExecutor(backend,valid_gate);observed=[]
    executor.set_physical_step_callback(observed.append)
    async def run():
        executor.arm();executor.heartbeat()
        return await executor.execute({'type':'observe_only'})
    result=asyncio.run(run())
    assert result=={'dispatched':False,'kind':'observe_only'}
    assert observed==[]


@pytest.mark.parametrize('binding',[
    {'type':'keypress_chord','keycodes':[13,13],'hold_seconds':.1},
    {'type':'keypress_chord','keycodes':[13,49,1],'hold_seconds':.1},
    {'type':'keypress_chord','keycodes':[13,49],'hold_seconds':1.01},
    {'type':'keypress_chord','keycodes':[13,49],'hold_seconds':.1,'text':'/run arbitrary'},
])
def test_keypress_chord_rejects_ambiguous_or_oversized_binding(binding):
    backend=FakeBackend();executor=SafeExecutor(backend,valid_gate)
    async def run():
        executor.arm();executor.heartbeat()
        with pytest.raises(ExecutionRejected):await executor.execute(binding)
    asyncio.run(run())
    assert not [event for event in backend.events if event[0]=='key' and event[2] is True]
    assert backend.releases>=2


def test_keypress_chord_releases_first_key_when_focus_is_lost_before_second():
    backend=FakeBackend();active={'foreground':True};base=valid_gate()
    def gate():
        return GateSnapshot(base.window_id,base.calibrated_window_id,active['foreground'],base.bounds,
                            base.calibrated_bounds,base.calibrated,base.client_identity_verified)
    backend.on_key_down=lambda:active.update(foreground=False)
    executor=SafeExecutor(backend,gate)
    async def run():
        executor.arm();executor.heartbeat()
        with pytest.raises(ExecutionRejected,match='lost focus'):
            await executor.execute({'type':'keypress_chord','keycodes':[13,49],'hold_seconds':.5})
    asyncio.run(run())
    downs=[event for event in backend.events if event[0]=='key' and event[2] is True]
    ups=[event for event in backend.events if event[0]=='key' and event[2] is False]
    assert downs==[('key',13,True)]
    assert ('key',13,False) in ups and ('key',49,False) in ups
    assert not executor.armed and backend.releases>=2


def test_keypress_chord_releases_every_key_on_stop_during_hold():
    backend=FakeBackend();executor=SafeExecutor(backend,valid_gate,heartbeat_timeout=2)
    async def run():
        executor.arm()
        pending=asyncio.create_task(executor.execute(
            {'type':'keypress_chord','keycodes':[13,49],'hold_seconds':.6}))
        await asyncio.sleep(.05)
        await executor.stop('operator_stop')
        with pytest.raises(ExecutionRejected,match='disarmed during key chord'):
            await pending
    asyncio.run(run())
    ups=[event for event in backend.events if event[0]=='key' and event[2] is False]
    assert ('key',13,False) in ups and ('key',49,False) in ups
    assert not executor.armed and backend.releases>=3


def test_keypress_chord_releases_every_key_when_backend_raises_mid_chord():
    class RaiseOnSecondDown(FakeBackend):
        def key(self,keycode,down):
            self.events.append(('key',keycode,down))
            if keycode==49 and down:raise RuntimeError('fake backend error')
    backend=RaiseOnSecondDown();executor=SafeExecutor(backend,valid_gate)
    async def run():
        executor.arm();executor.heartbeat()
        with pytest.raises(RuntimeError,match='fake backend error'):
            await executor.execute({'type':'keypress_chord','keycodes':[13,49],'hold_seconds':.1})
    asyncio.run(run())
    ups=[event for event in backend.events if event[0]=='key' and event[2] is False]
    assert ('key',13,False) in ups and ('key',49,False) in ups
    assert not executor.armed and backend.releases>=2


def test_input_stays_disarmed_without_foreground_exact_calibration_and_client_identity():
    backend = FakeBackend()
    base = valid_gate()
    invalid = GateSnapshot(base.window_id, base.calibrated_window_id, True, base.bounds, base.calibrated_bounds, True, False)
    executor = SafeExecutor(backend, lambda: invalid)
    with pytest.raises(ExecutionRejected, match="lost focus or no longer matches"):
        executor.arm()
    assert backend.events == []


def test_stop_during_mouse_hold_rejects_click_and_releases_input():
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate)

    async def run():
        executor.arm()
        pending = asyncio.create_task(executor.execute(
            {'type': 'click', 'image_x': 50, 'image_y': 50}, frame_size=(800, 600)))
        await asyncio.sleep(0.02)
        await executor.stop('operator_stop')
        with pytest.raises(ExecutionRejected, match='disarmed during click'):
            await pending

    asyncio.run(run())
    assert not executor.armed
    assert backend.releases >= 3

def test_sequence_stops_before_second_key_if_focus_changes():
    backend=FakeBackend(); active={'ok':True}; original=valid_gate()
    def gate():
        return GateSnapshot(original.window_id,original.calibrated_window_id,active['ok'],original.bounds,original.calibrated_bounds,True,True)
    backend.on_key_down=lambda:active.update(ok=False)
    executor=SafeExecutor(backend,gate)
    async def run():
        executor.arm()
        with pytest.raises(ExecutionRejected):
            await executor.execute({'type':'keypress_sequence','keycodes':[122,20]})
        await executor.stop()
    asyncio.run(run())
    assert ('key',122,False) in backend.events
    assert ('key',20,True) not in backend.events


def test_named_target_is_parameterized_and_does_not_use_clipboard():
    backend = FakeBackend()
    backend.text = lambda value: backend.events.append(('text', value))
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=2)
    async def run():
        executor.arm()
        result = await executor.execute({'type': 'target_named', 'name': 'Young Boar'})
        await executor.stop()
        return result
    result = asyncio.run(run())
    assert result['command'] == '/targetexact Young Boar'
    assert ('text', '/targetexact Young Boar') in backend.events
    assert backend.events[-2:] == [('key', 36, True), ('key', 36, False)]


@pytest.mark.parametrize('name', ['Wolf\n/say hi', 'Wolf; /run x', '%t', '', '[target=player]', ' Wolf'])
def test_named_target_rejects_command_injection_before_input(name):
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate)
    async def run():
        executor.arm()
        with pytest.raises(ExecutionRejected):
            await executor.execute({'type': 'target_named', 'name': name})
        await executor.stop()
    asyncio.run(run())
    assert backend.events == []


def test_named_target_does_not_submit_after_focus_loss():
    backend = FakeBackend()
    active = {'ok': True}
    original = valid_gate()
    def gate():
        return GateSnapshot(original.window_id, original.calibrated_window_id, active['ok'], original.bounds, original.calibrated_bounds, True, True)
    def text(value):
        backend.events.append(('text', value))
        active['ok'] = False
    backend.text = text
    executor = SafeExecutor(backend, gate, heartbeat_timeout=2)
    async def run():
        executor.arm()
        with pytest.raises(ExecutionRejected):
            await executor.execute({'type':'target_named','name':'Ragged Young Wolf'})
        await executor.stop()
    asyncio.run(run())
    assert sum(e == ('key',36,True) for e in backend.events) == 1


def test_compound_target_cast_clears_then_targets_then_uses_game_conditions():
    backend=FakeBackend();backend.text=lambda value:backend.events.append(('text',value))
    executor=SafeExecutor(backend,valid_gate,heartbeat_timeout=3)
    async def run():
        executor.arm()
        try:return await executor.execute({'type':'target_and_cast','name':'Young Boar','spell':'Smite'})
        finally:await executor.stop()
    result=asyncio.run(run())
    assert [e[1] for e in backend.events if e[0]=='text']==[
        '/cleartarget','/targetexact Young Boar','/cast [harm,nodead] Smite']
    assert result['dispatched']
    assert 'unverified' in result['cast_result']


def test_regular_cast_rechecks_living_target_without_clearing_corpse_selection():
    backend = FakeBackend()
    backend.text = lambda value: backend.events.append(('text', value))
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=2)
    async def run():
        executor.arm()
        try:
            return await executor.execute({'type': 'cast_guarded', 'spell': 'Smite'})
        finally:
            await executor.stop()
    result = asyncio.run(run())
    assert [e[1] for e in backend.events if e[0] == 'text'] == ['/cast [harm,nodead] Smite']
    assert ('key', 19, True) not in backend.events
    assert result['dispatched'] and 'unverified' in result['cast_result']


@pytest.mark.parametrize('kind', ['cast_guarded', 'target_and_cast'])
@pytest.mark.parametrize('spell', ['Smite\n/say unsafe', 'Unverified spell'])
def test_cast_spell_is_allowlisted_before_any_input(kind, spell):
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate)
    async def run():
        executor.arm()
        try:
            with pytest.raises(ExecutionRejected):
                await executor.execute({'type': kind, 'name': 'Young Boar', 'spell': spell})
        finally:
            await executor.stop()
    asyncio.run(run())
    assert backend.events == []


def test_guarded_cast_does_not_submit_after_focus_loss():
    backend = FakeBackend()
    active = {'ok': True}
    original = valid_gate()
    def gate():
        return GateSnapshot(original.window_id, original.calibrated_window_id, active['ok'],
                            original.bounds, original.calibrated_bounds, True, True)
    def text(value):
        backend.events.append(('text', value))
        active['ok'] = False
    backend.text = text
    executor = SafeExecutor(backend, gate, heartbeat_timeout=2)
    async def run():
        executor.arm()
        try:
            with pytest.raises(ExecutionRejected):
                await executor.execute({'type': 'cast_guarded', 'spell': 'Smite'})
        finally:
            await executor.stop()
    asyncio.run(run())
    assert sum(e == ('key', 36, True) for e in backend.events) == 1
    assert not executor.armed


def test_compound_rejects_injection_before_clearing_target():
    backend=FakeBackend();executor=SafeExecutor(backend,valid_gate)
    async def run():
        executor.arm()
        try:
            with pytest.raises(ExecutionRejected):
                await executor.execute({'type':'target_and_cast','name':'Wolf\n/say unsafe','spell':'Smite'})
        finally:await executor.stop()
    asyncio.run(run())
    assert backend.events==[]


def test_compound_focus_loss_never_sends_later_cast_and_reports_partial_steps():
    backend=FakeBackend();base=valid_gate();active={'yes':True};texts=[]
    def text(value):
        texts.append(value)
        if value.startswith('/targetexact'):active['yes']=False
    backend.text=text
    def gate():return GateSnapshot(base.window_id,base.calibrated_window_id,active['yes'],base.bounds,base.calibrated_bounds,True,True)
    executor=SafeExecutor(backend,gate,heartbeat_timeout=3)
    async def run():
        executor.arm()
        try:
            with pytest.raises(ExecutionRejected) as error:
                await executor.execute({'type':'target_and_cast','name':'Young Boar','spell':'Smite'})
            assert error.value.completed_steps==['/cleartarget']
        finally:await executor.stop()
    asyncio.run(run())
    assert texts==['/cleartarget','/targetexact Young Boar']


def test_cast_burst_runs_only_guarded_pulses_without_retargeting_and_records_each_step():
    backend = FakeBackend()
    observations = []
    pacing = []
    progress = []
    async def guard(binding, index):
        observations.append((binding['expected_target_name'], index))
        return {'continue': True, 'frame_id': f'frame-{index}', 'reason': 'visible target still matches'}
    async def fake_sleep(seconds):
        pacing.append(seconds)
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=3, batch_guard=guard,
                            batch_step_callback=progress.append, batch_sleep=fake_sleep)
    async def run():
        executor.arm()
        try:
            return await executor.execute({'type': 'cast_burst', 'spell': 'Smite', 'count': 3,
                                           'interval_seconds': 1.5,
                                           'expected_target_name': 'Ragged Young Wolf'})
        finally:
            await executor.stop()
    result = asyncio.run(run())
    assert [e[1] for e in backend.events if e[0] == 'text'] == ['/cast [harm,nodead] Smite'] * 3
    assert not any(e[0] == 'text' and ('target' in e[1] or 'clear' in e[1]) for e in backend.events)
    assert observations == [('Ragged Young Wolf', 0), ('Ragged Young Wolf', 1), ('Ragged Young Wolf', 2)]
    assert pacing == [1.5, 1.5]
    assert result['completed'] and result['dispatched'] and len(result['completed_steps']) == 3
    assert [item['command'] for item in result['completed_steps']] == ['/cast [harm,nodead] Smite'] * 3
    assert [item['frame_id'] for item in result['guard_observations']] == ['frame-0', 'frame-1', 'frame-2']
    assert [item['phase'] for item in progress] == [
        'guard_observed', 'pulse_dispatched', 'guard_observed', 'pulse_dispatched',
        'guard_observed', 'pulse_dispatched']
    assert all(item['expected_target_name'] == 'Ragged Young Wolf' for item in progress)


@pytest.mark.parametrize('binding', [
    {'count': True}, {'count': 1}, {'count': 4}, {'count': 2.0},
    {'count': 2, 'interval_seconds': 0}, {'count': 2, 'interval_seconds': .001},
    {'count': 2, 'interval_seconds': 3.01},
    {'count': 2, 'interval_seconds': float('nan')},
    {'count': 2, 'expected_target_name': 'Wolf\n/say unsafe'},
])
def test_cast_burst_rejects_invalid_bounds_or_target_before_input(binding):
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate, batch_guard=lambda *_: {'continue': True})
    request = {'type': 'cast_burst', 'spell': 'Smite', 'count': 2,
               'interval_seconds': 1.7, 'expected_target_name': 'Young Boar'} | binding
    async def run():
        executor.arm()
        try:
            with pytest.raises(ExecutionRejected):
                await executor.execute(request)
        finally:
            await executor.stop()
    asyncio.run(run())
    assert backend.events == []


def test_cast_burst_requires_runtime_screenshot_guard_before_any_input():
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate)
    async def run():
        executor.arm()
        try:
            with pytest.raises(ExecutionRejected, match='fresh-frame batch guard'):
                await executor.execute({'type': 'cast_burst', 'spell': 'Smite', 'count': 2,
                                       'expected_target_name': 'Young Boar'})
        finally:
            await executor.stop()
    asyncio.run(run())
    assert backend.events == []


def test_cast_burst_stops_when_fresh_frame_guard_says_target_changed():
    backend = FakeBackend()
    async def guard(binding, index):
        return {'continue': index == 0, 'frame_id': f'frame-{index}',
                'reason': 'target changed' if index else 'target matched'}
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=3, batch_guard=guard)
    async def run():
        executor.arm()
        try:
            return await executor.execute({'type': 'cast_burst', 'spell': 'Smite', 'count': 3,
                                           'interval_seconds': 1.5,
                                           'expected_target_name': 'Young Boar'})
        finally:
            await executor.stop()
    result = asyncio.run(run())
    assert [e[1] for e in backend.events if e[0] == 'text'] == ['/cast [harm,nodead] Smite']
    assert result['cancelled'] and result['completed'] and not result['burst_completed'] and len(result['completed_steps']) == 1
    assert result['cancel_reason'] == 'target changed'


def test_cast_burst_focus_loss_before_second_pulse_preserves_partial_provenance():
    backend = FakeBackend()
    base = valid_gate()
    active = {'yes': True}
    def gate():
        return GateSnapshot(base.window_id, base.calibrated_window_id, active['yes'],
                            base.bounds, base.calibrated_bounds, True, True)
    async def guard(binding, index):
        if index == 1:
            active['yes'] = False
        return {'continue': True, 'frame_id': f'frame-{index}'}
    executor = SafeExecutor(backend, gate, heartbeat_timeout=3, batch_guard=guard)
    async def run():
        executor.arm()
        with pytest.raises(ExecutionRejected) as error:
            await executor.execute({'type': 'cast_burst', 'spell': 'Smite', 'count': 3,
                                    'interval_seconds': 1.5, 'expected_target_name': 'Young Boar'})
        await executor.stop()
        return error.value
    error = asyncio.run(run())
    assert len(error.completed_steps) == 1
    assert error.guard_observations[-1]['frame_id'] == 'frame-1'
    assert [e[1] for e in backend.events if e[0] == 'text'] == ['/cast [harm,nodead] Smite']
    assert not executor.armed


def test_cast_burst_stop_during_pacing_does_not_send_next_pulse():
    backend = FakeBackend()
    async def guard(binding, index):
        return {'continue': True, 'frame_id': f'frame-{index}'}
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=3, batch_guard=guard)
    async def run():
        executor.arm()
        pending = asyncio.create_task(executor.execute({'type': 'cast_burst', 'spell': 'Smite', 'count': 3,
                                                        'interval_seconds': 1.5,
                                                        'expected_target_name': 'Young Boar'}))
        await asyncio.sleep(.5)
        await executor.stop('operator_stop')
        with pytest.raises(ExecutionRejected, match='stopped during cast_burst pacing') as error:
            await pending
        return error.value
    error = asyncio.run(run())
    assert len(error.completed_steps) == 1
    assert len([e for e in backend.events if e[0] == 'text']) == 1


def test_cast_burst_deadline_aborts_before_next_pulse_and_does_not_leak_timeout_error():
    backend = FakeBackend()
    async def guard(binding, index):
        return {'continue': True, 'frame_id': f'frame-{index}'}
    async def fake_sleep(seconds):
        return None
    executor = SafeExecutor(backend, valid_gate, heartbeat_timeout=3, batch_guard=guard,
                            batch_sleep=fake_sleep)
    executor.CAST_BURST_MAX_SECONDS = .65
    async def run():
        executor.arm()
        try:
            return await executor.execute({'type': 'cast_burst', 'spell': 'Smite', 'count': 3,
                                           'interval_seconds': 1.5,
                                           'expected_target_name': 'Young Boar'})
        finally:
            await executor.stop()
    result = asyncio.run(run())
    assert result['completed'] and result['cancelled'] and not result['burst_completed']
    assert 'deadline' in result['cancel_reason']
    assert len(result['completed_steps']) == 1
    assert len([e for e in backend.events if e[0] == 'text']) == 1
