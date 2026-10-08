from __future__ import annotations

import asyncio
import time
import math
import inspect
import threading
import sys
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from sage_wow.platform.macos.geometry import Rect
from sage_wow.control.camera_commands import camera_zoom_command
from sage_wow.control.ui_commands import CHAT_VISIBILITY_COMMANDS
from sage_wow.control.target_names import plain_target_name
from sage_wow.control.periodic_gate import PeriodicGatePoller


class InputBackend(Protocol):
    def key(self, keycode: int, down: bool) -> None: ...
    def mouse_button(self, x: int, y: int, button: int, down: bool) -> None: ...
    def text(self, value: str) -> None: ...
    def release_all(self) -> None: ...


@dataclass(frozen=True)
class GateSnapshot:
    window_id: int | None
    calibrated_window_id: int | None
    foreground: bool
    bounds: Rect | None
    calibrated_bounds: Rect | None
    calibrated: bool
    client_identity_verified: bool = False
    # Diagnostic evidence travels with the exact observation, including worker
    # failures. It never changes gate validity or snapshot equality.
    identity_evidence: dict[str, Any] | None = field(default=None, compare=False, hash=False)

    @property
    def valid(self) -> bool:
        return (
            self.window_id is not None and self.window_id == self.calibrated_window_id
            and self.foreground and self.calibrated and self.client_identity_verified
            and self.bounds is not None and self.calibrated_bounds == self.bounds
            and self.bounds.width > 0 and self.bounds.height > 0
        )


class ExecutionRejected(RuntimeError):
    pass


class SafeExecutor:
    """One input owner with fail-closed focus, calibration, timeout and heartbeat gates."""

    MAX_HOLD_SECONDS = 3.0
    MAX_ACTION_SECONDS = 4.0
    CAST_BURST_MAX_SECONDS = 14.0
    CAST_BURST_GUARD_MAX_SECONDS = 2.0
    CAST_BURST_MIN_SUBMISSION_SECONDS = 0.5

    def __init__(self, backend: InputBackend, inspect_gate, *, heartbeat_timeout: float = 0.75,
                 watchdog_interval: float = 0.05, batch_guard=None, batch_step_callback=None, batch_post_guard=None,
                 batch_sleep=None, physical_step_callback=None):
        self.backend = backend
        self.inspect_gate = inspect_gate
        self.heartbeat_timeout = heartbeat_timeout
        self.watchdog_interval = watchdog_interval
        self._last_heartbeat = time.monotonic()
        self._armed = False
        self._stopped = asyncio.Event()
        self._lock = asyncio.Lock()
        self._watchdog: asyncio.Task[None] | None = None
        # The event-loop watchdog checks focus, but cannot release a held key
        # while synchronous image/store work blocks that loop. This thread
        # watches only the controller heartbeat; it shares the same backend
        # and input lock rather than becoming a second input owner.
        self._input_lock = threading.RLock()
        self._heartbeat_watch_stop: threading.Event | None = None
        self._heartbeat_watch_thread: threading.Thread | None = None
        self.last_watchdog_trip: dict[str, Any] | None = None
        self._watchdog_events: deque[dict[str, Any]] = deque(maxlen=64)
        self._loop_thread_id: int | None = None
        self._gate_inflight: dict[str, Any] | None = None
        self._gate_checks: dict[str, dict[str, Any]] = {}
        self._slow_gate_events: deque[dict[str, Any]] = deque(maxlen=64)
        self._phase = "idle"
        self._phase_started = self._last_heartbeat
        self._loop_lag = self._max_loop_lag = 0.0
        self._held_since: dict[tuple[str, int], float] = {}
        self._disarm_reason: str | None = "operator_disarmed"
        # A burst is available only when the runtime installs a fresh-frame
        # guard. Standalone executors therefore fail closed on this action.
        self.batch_guard = batch_guard
        self.batch_post_guard = batch_post_guard
        self.batch_step_callback = batch_step_callback
        self._batch_sleep = batch_sleep or asyncio.sleep
        self._physical_step_callback = physical_step_callback
        self._active_input_steps: list[dict[str, Any]] | None = None
        self._active_execution_id: str | None = None
        self._periodic_gate=PeriodicGatePoller()
        self._periodic_reader=self._periodic_observer=self._periodic_context=None
        self._gate_lease=0
        self._gate_stopping=False
        self._periodic_timeout_trip=None

    @property
    def periodic_timeout_trip(self):
        with self._input_lock:
            return deepcopy(self._periodic_timeout_trip)

    def configure_periodic_gate(self, *, reader=None, observer=None, context=None):
        """Separate raw native reads from owner-thread safety observation."""
        self._periodic_reader,self._periodic_observer,self._periodic_context=reader,observer,context

    @property
    def periodic_gate_busy(self):
        return self._periodic_gate.busy

    async def inspect_gate_periodic(self, caller):
        lease=self._gate_lease
        request_id=str(uuid4())
        context=self._periodic_context
        scope=deepcopy(context()) if context else {}
        reader=self._periodic_reader or self.inspect_gate
        observer=self._periodic_observer
        def current():
            return (lease==self._gate_lease and not self._gate_stopping
                and (context() if context else {})==scope)
        def consume(gate,error,abandoned):
            now=context() if context else {}
            same_scope={k:v for k,v in now.items() if k!='generation'}=={k:v for k,v in scope.items() if k!='generation'}
            if lease!=self._gate_lease or self._gate_stopping or not same_scope:
                with self._input_lock:
                    self._slow_gate_events.append({'caller':caller,'request_id':request_id,
                        'request_lease':lease,'current_lease':self._gate_lease,
                        'status':'retired_periodic_result','valid':getattr(gate,'valid',False),
                        'error_type':type(error).__name__ if error else None})
                return None
            if error is not None:
                self._disarm('periodic_gate_inspection_failed')
                raise ExecutionRejected('periodic gate inspection failed') from error
            if not isinstance(gate,GateSnapshot):
                self._disarm('periodic_gate_inspection_failed')
                raise ExecutionRejected('periodic gate inspection returned no snapshot')
            if observer:gate=observer(gate,caller)
            if not gate.valid:
                if lease==self._gate_lease:self._disarm('focus_or_calibration_invalid')
                return gate
            return gate if not abandoned and current() else None
        try:
            return await self._periodic_gate.read(reader,consume,current,self.heartbeat_timeout)
        except asyncio.TimeoutError as exc:
            if lease==self._gate_lease and not self._gate_stopping:
                self._disarm('periodic_gate_inspection_timeout',timeout_context={
                    'request_id':request_id,'caller':caller,'request_lease':lease,
                    'context':deepcopy(context() if context else {})})
                raise ExecutionRejected('periodic gate inspection timed out') from exc
            return None
        except ExecutionRejected:raise
        except Exception as exc:
            if lease==self._gate_lease and not self._gate_stopping:
                self._disarm('periodic_gate_inspection_failed')
                raise ExecutionRejected('periodic gate inspection failed') from exc
            return None

    def set_physical_step_callback(self, callback) -> None:
        """Install a synchronous observer called before each input primitive."""
        self._physical_step_callback = callback

    def set_phase(self, name: str) -> None:
        """Label current controller work without renewing its heartbeat lease."""
        with self._input_lock:
            self._phase, self._phase_started = name, time.monotonic()

    def timing_snapshot(self) -> dict[str, Any]:
        with self._input_lock:
            now = time.monotonic()
            return {"monotonic_at": now, "phase": self._phase,
                    "phase_elapsed_seconds": now - self._phase_started,
                    "heartbeat_age_seconds": now - self._last_heartbeat,
                    "heartbeat_timeout_seconds": self.heartbeat_timeout,
                    "loop_lag_seconds": self._loop_lag,
                    "max_loop_lag_seconds": self._max_loop_lag,
                    "gate_inflight": ({**self._gate_inflight,
                        "elapsed_seconds": now-self._gate_inflight['started_at_monotonic']}
                        if self._gate_inflight else None),
                    "gate_checks": {name:dict(values) for name,values in self._gate_checks.items()},
                    "armed": self._armed, "disarm_reason": self._disarm_reason}

    def inspect_gate_measured(self, inspect, caller: str) -> GateSnapshot:
        """Measure native inspection without holding the input/release lock."""
        started = time.monotonic()
        sample = {'caller':caller, 'started_at_monotonic':started,
                  'thread_id':threading.get_ident(), 'phase':self._phase}
        with self._input_lock:
            previous = self._gate_inflight
            self._gate_inflight = sample
        try:
            gate = inspect()
            sample['valid'] = gate.valid
            return gate
        except BaseException as exc:
            sample['error_type'] = type(exc).__name__
            raise
        finally:
            elapsed = time.monotonic()-started
            sample['elapsed_seconds'] = elapsed
            with self._input_lock:
                self._gate_inflight = previous
                totals = self._gate_checks.setdefault(caller,{'count':0,'max_seconds':0.})
                totals.update(count=totals['count']+1,last_seconds=elapsed,
                              max_seconds=max(totals['max_seconds'],elapsed))
                if elapsed >= .05:
                    self._slow_gate_events.append(sample)

    def drain_gate_events(self) -> list[dict[str, Any]]:
        with self._input_lock:
            events = list(self._slow_gate_events)
            self._slow_gate_events.clear()
            return events

    def _loop_stack_snapshot(self) -> dict[str, Any]:
        """Bounded metadata only: no locals, source loading, logging or frame retention."""
        stack = []
        frame = None
        try:
            frame = sys._current_frames().get(self._loop_thread_id)
            for _ in range(32):
                if frame is None:
                    break
                stack.append({'file':frame.f_code.co_filename[-512:],
                              'line':frame.f_lineno, 'function':frame.f_code.co_name[:128]})
                frame = frame.f_back
            return {'thread_id':self._loop_thread_id,'frames':list(reversed(stack)),
                    'truncated':frame is not None,'sampled_at_monotonic':time.monotonic()}
        except Exception as exc:
            return {'thread_id':self._loop_thread_id,'frames':[],
                    'error_type':type(exc).__name__,'sampled_at_monotonic':time.monotonic()}
        finally:
            del frame

    def drain_watchdog_events(self) -> list[dict[str, Any]]:
        """Drain bounded diagnostics on the caller's loop; never write SQLite in the watcher."""
        with self._input_lock:
            events = list(self._watchdog_events)
            self._watchdog_events.clear()
            return events

    def _physical(self, kind: str, details: dict[str, Any], operation) -> Any:
        step = {
            "step_id": str(uuid4()),
            "execution_id": self._active_execution_id,
            "kind": kind,
            "details": deepcopy(details),
            "attempted": True,
            "status": "effect_unknown",
        }
        if self._active_input_steps is not None:
            self._active_input_steps.append(step)
        if self._physical_step_callback is not None:
            # Receipt persistence can block. Keep it outside the input lock so
            # the watcher can still release; recheck authority after it returns.
            notified = self._physical_step_callback(step)
            if asyncio.iscoroutine(notified):
                raise RuntimeError("physical_step_callback must be synchronous")
        try:
            with self._input_lock:
                if kind not in {"key_up", "mouse_up"}:
                    self._expire_heartbeat("dispatch")
                    if not self._armed:
                        step["status"] = "blocked_no_input"
                        raise ExecutionRejected(self._disarm_reason or "executor is disarmed")
                started = time.monotonic()
                step["dispatch_started_at_monotonic"] = started
                held = ("key", details["keycode"]) if kind.startswith("key_") else (
                    ("mouse", details["button"]) if kind in {"mouse_down", "mouse_up"} else None)
                if held is not None and kind in {"key_down", "mouse_down"}:
                    # Keep possible downs until a release succeeds, including
                    # backends that raise after partially posting an event.
                    self._held_since[held] = started
                result = operation()
                step["dispatch_completed_at_monotonic"] = time.monotonic()
                if held is not None and kind in {"key_up", "mouse_up"}:
                    self._held_since.pop(held, None)
        except BaseException:
            if step["status"] != "blocked_no_input":
                step["status"] = "failed_effect_unknown"
            raise
        step["status"] = "completed"
        return result

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def disarm_reason(self) -> str | None:
        return self._disarm_reason

    def heartbeat(self, *, loop_lag_seconds: float = 0.0) -> None:
        with self._input_lock:
            self._loop_lag = max(0.0, loop_lag_seconds)
            self._max_loop_lag = max(self._max_loop_lag, self._loop_lag)
            # A resumed coroutine cannot erase a missed deadline even if it
            # happens to run before the independent watcher is scheduled.
            self._expire_heartbeat("late_heartbeat")
            if self._armed:
                self._last_heartbeat = time.monotonic()

    def arm(self) -> None:
        if self.periodic_gate_busy or self._gate_stopping:
            raise ExecutionRejected('periodic gate inspection is still in flight')
        if self._lock.locked():
            raise ExecutionRejected("cannot arm while an execution is still active")
        self._loop_thread_id = threading.get_ident()
        with self._input_lock:
            self.backend.release_all()
            self._held_since.clear()
        self._validate_gate()
        self._stopped.clear()
        with self._input_lock:
            if self._heartbeat_watch_stop is not None:
                self._heartbeat_watch_stop.set()
            stop = threading.Event()
            self._heartbeat_watch_stop = stop
            self._last_heartbeat = time.monotonic()
            self._gate_lease+=1
            self._armed = True
            self._disarm_reason = None
            self.last_watchdog_trip = None
            self._periodic_timeout_trip = None
            watcher = threading.Thread(target=self._watch_heartbeat, args=(stop,),
                                       name="sage-wow-input-heartbeat", daemon=True)
            self._heartbeat_watch_thread = watcher
            watcher.start()
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watch(), name="sage-wow-input-watchdog")

    async def stop(self, reason: str = "operator_stop") -> None:
        self._gate_stopping=True
        release_error = None
        try:
            try:
                self._disarm(reason)
            except BaseException as exc:
                release_error = exc
            self._stopped.set()
            async with self._lock:
                with self._input_lock:
                    try:
                        self.backend.release_all()
                        self._held_since.clear()
                    except BaseException as exc:
                        release_error = release_error or exc
        finally:
            self._stopped.set()
            # The backend may be closed immediately after stop returns. Wait
            # for the release-only worker to exit before relinquishing ownership.
            watcher = self._heartbeat_watch_thread
            if watcher is not None:
                await asyncio.to_thread(watcher.join)
                self._heartbeat_watch_thread = None
            if self._watchdog:
                self._watchdog.cancel()
                try:
                    await self._watchdog
                except asyncio.CancelledError:
                    pass
                self._watchdog = None
            await self._periodic_gate.drain(self.heartbeat_timeout)
            self._gate_stopping=False
        if release_error is not None:
            raise release_error

    async def execute(self, binding: dict[str, Any], *, frame_size: tuple[int, int] | None = None,
                      execution_id: str | None = None, pre_dispatch_guard=None) -> dict[str, Any]:
        async with self._lock:
            if self._active_input_steps is not None:
                raise RuntimeError("executor input trace is already active")
            self._active_input_steps = []
            self._active_execution_id = execution_id
            result: dict[str, Any] | None = None
            action_error: BaseException | None = None
            try:
                if not self._armed:
                    raise ExecutionRejected(self._disarm_reason or "executor is disarmed")
                self._validate_gate()
                if time.monotonic() - self._last_heartbeat > self.heartbeat_timeout:
                    self._disarm("controller_heartbeat_lost")
                    raise ExecutionRejected("controller heartbeat is stale")
                kind = binding.get("type")
                if kind == "observe_only":
                    result = {"dispatched": False, "kind": kind}
                elif kind == "keypress":
                    result = await self._keypress(binding, cooperative=True, pre_dispatch_guard=pre_dispatch_guard)
                elif kind == "target_named":
                    result = await self._target_named(binding)
                elif kind == "target_previous":
                    result = await self._target_previous(binding)
                elif kind == "ui_visibility":
                    result = await self._ui_visibility(binding)
                elif kind == "camera_zoom":
                    result = await self._camera_zoom(binding)
                elif kind == "target_and_cast":
                    result = await self._target_and_cast(binding)
                elif kind == "cast_guarded":
                    result = await self._cast_guarded(binding)
                elif kind == "cast_self_heal":
                    result = await self._cast_self_heal(binding)
                elif kind == "cast_burst":
                    result = await self._cast_burst(binding)
                elif kind == "keypress_sequence":
                    keys = binding.get("keycodes")
                    if not isinstance(keys, list) or not 1 <= len(keys) <= 3 or any(not isinstance(k, int) or k < 0 for k in keys):
                        raise ExecutionRejected("keypress_sequence requires 1..3 verified keycodes")
                    for keycode in keys:
                        if not self._armed:
                            raise ExecutionRejected("executor stopped during sequence")
                        await self._keypress({"keycode": keycode, "hold_seconds": 0.08})
                        await asyncio.sleep(0.05)
                    result = {"dispatched": True, "kind": kind, "keycodes": keys}
                elif kind == "keypress_chord":
                    result = await self._keypress_chord(binding, pre_dispatch_guard=pre_dispatch_guard)
                elif kind == "click":
                    result = await self._click(binding, frame_size)
                elif kind == "hover":
                    result = await self._hover(binding, frame_size)
                elif kind == "wait":
                    duration = self._duration(binding.get("seconds", 0))
                    try:
                        await asyncio.wait_for(self._stopped.wait(), timeout=duration)
                    except asyncio.TimeoutError:
                        self._validate_gate()
                        result = {"dispatched": False, "kind": "wait", "elapsed_seconds": duration}
                    else:
                        raise ExecutionRejected("executor stopped during wait")
                else:
                    raise ExecutionRejected(f"unsupported local action binding: {kind!r}")
                if not self._armed:
                    raise ExecutionRejected(self._disarm_reason or "executor stopped during action")
            except asyncio.TimeoutError as exc:
                # Timeouts from non-wait commands are failures, never a reason
                # to fabricate a successful wait result.
                action_error = exc
                try:
                    self._disarm("action_failed_or_interrupted")
                except BaseException as cleanup_error:
                    try:
                        exc.cleanup_error = type(cleanup_error).__name__
                    except Exception:
                        pass
            except BaseException as exc:
                action_error = exc
                try:
                    self._disarm("action_failed_or_interrupted")
                except BaseException as cleanup_error:
                    try:
                        exc.cleanup_error = type(cleanup_error).__name__
                    except Exception:
                        pass
            finally:
                # Individual key/button release edges are traced at their
                # backend call sites. This fallback can be a no-op, so it does
                # not advance input generation.
                try:
                    with self._input_lock:
                        self.backend.release_all()
                        self._held_since.clear()
                except BaseException as cleanup_error:
                    if action_error is None:
                        action_error = cleanup_error
                    else:
                        try:
                            action_error.cleanup_error = type(cleanup_error).__name__
                        except Exception:
                            pass
                finally:
                    if action_error is not None:
                        try:
                            action_error.input_steps = self._active_input_steps
                            action_error.execution_id = execution_id
                        except Exception:
                            pass
                    self._active_input_steps = None
                    self._active_execution_id = None
            if action_error is not None:
                raise action_error
            assert result is not None
            return result

    async def _prepare_first_down(self, pre_dispatch_guard=None) -> None:
        """Yield once before input, then discard all pre-yield gate authority."""
        lease = self._gate_lease
        execution_id = self._active_execution_id
        steps = self._active_input_steps
        context_reader = self._periodic_context
        context = deepcopy(context_reader()) if context_reader else None

        def current():
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError
            # Context/native/caller work must never occupy the release lock.
            if (self._periodic_context is not context_reader
                    or (context_reader() if context_reader else None) != context):
                raise ExecutionRejected('execution context changed before first input')
            with self._input_lock:
                self._expire_heartbeat('first_input_preparation')
                if (not self._armed or self._stopped.is_set() or self._gate_stopping
                        or self._gate_lease != lease or self._active_execution_id != execution_id
                        or self._active_input_steps is not steps or steps or self._held_since):
                    raise ExecutionRejected(self._disarm_reason or 'first input authority changed')

        current()
        # A positive scheduling opportunity lets due timer continuations run.
        # It neither renews the heartbeat nor promises that the lease survives.
        await asyncio.sleep(.001)
        current()
        self._validate_gate()
        if pre_dispatch_guard is not None:
            reason = pre_dispatch_guard()
            if inspect.isawaitable(reason):
                if inspect.iscoroutine(reason):
                    reason.close()
                raise ExecutionRejected('first input authority guard must be synchronous')
            if reason is not None:
                raise ExecutionRejected(str(reason) if isinstance(reason, str) and reason else
                                        'first input authority guard returned an invalid result')
        current()

    async def _keypress(self, binding: dict[str, Any], *, cooperative=False,
                        pre_dispatch_guard=None) -> dict[str, Any]:
        keycode = binding.get("keycode")
        if not isinstance(keycode, int) or keycode < 0:
            raise ExecutionRejected("keypress needs an explicitly configured non-negative macOS keycode")
        duration = self._duration(binding.get("hold_seconds", 0.08), maximum=self.MAX_HOLD_SECONDS)
        if cooperative:
            await self._prepare_first_down(pre_dispatch_guard)
        else:
            self._validate_gate()
        try:
            self._physical("key_down", {"keycode": keycode}, lambda: self.backend.key(keycode, True))
            await asyncio.sleep(duration)
            if not self._armed:
                raise ExecutionRejected("executor disarmed during key hold")
            self._validate_gate()
        finally:
            self._physical("key_up", {"keycode": keycode}, lambda: self.backend.key(keycode, False))
        return {"dispatched": True, "kind": "keypress", "keycode": keycode, "duration_seconds": duration}

    async def _keypress_chord(self, binding: dict[str, Any], *, pre_dispatch_guard=None) -> dict[str, Any]:
        """Hold exactly two configured keys together for one short guarded pulse."""
        if set(binding) != {"type", "keycodes", "hold_seconds"}:
            raise ExecutionRejected("keypress_chord accepts only type, two keycodes, and hold_seconds")
        keys = binding.get("keycodes")
        if (not isinstance(keys, list) or len(keys) != 2
                or any(isinstance(key, bool) or not isinstance(key, int) or key < 0 for key in keys)
                or keys[0] == keys[1]):
            raise ExecutionRejected("keypress_chord requires two distinct configured non-negative integer keycodes")
        duration = self._duration(binding.get("hold_seconds"), maximum=1.0)
        self._validate_gate()
        await self._prepare_first_down(pre_dispatch_guard)
        try:
            for index, keycode in enumerate(keys):
                if not self._armed:
                    raise ExecutionRejected("executor disarmed during key chord")
                if index:
                    self._validate_gate()
                self._physical("key_down", {"keycode": keycode}, lambda keycode=keycode: self.backend.key(keycode, True))
            await asyncio.sleep(duration)
            if not self._armed:
                raise ExecutionRejected("executor disarmed during key chord")
            self._validate_gate()
        finally:
            # Attempt every key-up even if one backend release fails; execute()
            # also invokes release_all() in its outer finally block.
            release_error = None
            for keycode in reversed(keys):
                try:
                    self._physical("key_up", {"keycode": keycode}, lambda keycode=keycode: self.backend.key(keycode, False))
                except BaseException as exc:
                    release_error = release_error or exc
            if release_error is not None:
                raise release_error
        return {"dispatched": True, "kind": "keypress_chord", "keycodes": keys,
                "duration_seconds": duration}

    async def _target_named(self, binding: dict[str, Any]) -> dict[str, Any]:
        name = binding.get("name")
        # A mob name is data, never an arbitrary slash command or chat message.
        if not plain_target_name(name):
            raise ExecutionRejected("target_named requires a plain mob name")
        command = "/targetexact " + name
        await self._submit_command(command)
        return {"dispatched": True, "kind": "target_named", "name": name, "command": command}

    async def _target_previous(self, binding: dict[str, Any]) -> dict[str, Any]:
        """One fixed conditional selection attempt; target identity is unverified."""
        if set(binding) != {'type', 'expected_target_name'}:
            raise ExecutionRejected('target_previous binding contains unsupported fields')
        name = binding.get('expected_target_name')
        if not plain_target_name(name):
            raise ExecutionRejected('target_previous requires a plain expected_target_name')
        command = '/targetlasttarget [noexists]'
        await self._submit_command(command)
        return {'dispatched': True, 'kind': 'target_previous', 'expected_target_name': name,
                'command': command, 'selection_only': True,
                'selection_result': 'unverified; inspect fresh selected-target evidence'}

    async def _ui_visibility(self, binding: dict[str, Any]) -> dict[str, Any]:
        """Submit one allowlisted reversible UI command; never accept raw text."""
        if set(binding) != {"type", "action"}:
            raise ExecutionRejected("ui_visibility binding contains unsupported fields")
        action = binding.get("action")
        command = CHAT_VISIBILITY_COMMANDS.get(action) if isinstance(action, str) else None
        if command is None:
            raise ExecutionRejected("ui_visibility requires a fixed allowlisted UI action")
        if action == 'enable_combat_log':
            # Logging setup requires a freshly checked normal world. Escape
            # here would open the game menu and leave it covering the world.
            await self._submit_command(command)
        else:
            await self._submit_ui_command(command)
        return {"dispatched": True, "kind": "ui_visibility", "action": action,
                "command": command, "result": "unverified; inspect a fresh screenshot"}

    async def _camera_zoom(self, binding: dict[str, Any]) -> dict[str, Any]:
        """Submit exactly one fixed camera zoom pulse; never accept arbitrary code."""
        command = camera_zoom_command(binding)
        if command is None:
            raise ExecutionRejected("camera_zoom requires one allowlisted zoom pulse")
        await self._submit_ui_command(command)
        return {"dispatched": True, "kind": "camera_zoom",
                "direction": binding["direction"], "steps": 1,
                "command": command, "result": "unverified; inspect a fresh screenshot"}

    @staticmethod
    def _guarded_cast_command(binding: dict[str, Any]) -> str:
        if binding.get('spell') != 'Smite':
            raise ExecutionRejected('guarded casting requires the verified Smite spell')
        return '/cast [harm,nodead] Smite'

    async def _cast_guarded(self, binding: dict[str, Any]) -> dict[str, Any]:
        # A target can die while Sage's screenshot decision is in flight. Let
        # WoW evaluate the condition at submission, preserving the corpse target
        # for subsequent Sage-selected looting rather than clearing it here.
        command = self._guarded_cast_command(binding)
        if self.batch_post_guard is not None and binding.get('expected_target_name'):
            result=await self._cast_burst({**binding,'count':1},single=True)
            return {**result,'kind':'cast_guarded','command':command}
        opening=[]
        if self._start_attack_requested(binding):
            await self._submit_command('/startattack [harm,nodead]')
            opening.append({'command':'/startattack [harm,nodead]','dispatched':True})
        await self._submit_command(command)
        return {'dispatched': True, 'kind': 'cast_guarded', 'spell': binding['spell'],
                'command': command, 'opening_commands':opening,
                'cast_result': 'unverified; game evaluates harm,nodead'}

    @staticmethod
    def _start_attack_requested(binding):
        value=binding.get('start_attack',False)
        if not isinstance(value,bool):raise ExecutionRejected('start_attack must be an explicit boolean')
        return value

    async def _cast_self_heal(self, binding: dict[str, Any]) -> dict[str, Any]:
        """Cast one fixed spell on the player without changing selected target."""
        if binding.get('spell') != 'Lesser Heal':
            raise ExecutionRejected('cast_self_heal requires the allowlisted Lesser Heal spell')
        command='/cast [@player] Lesser Heal'
        await self._submit_command(command)
        return {'dispatched':True,'kind':'cast_self_heal','spell':'Lesser Heal',
                'command':command,'preserves_selected_target':True,
                'cast_result':'unverified; inspect a fresh screenshot'}

    async def _cast_burst(self, binding: dict[str, Any], *, single=False) -> dict[str, Any]:
        """Run Sage-authorized Smite pulses, rechecking the visible situation between them."""
        command = self._guarded_cast_command(binding)
        start_attack = self._start_attack_requested(binding)
        count = binding.get("count")
        interval = binding.get("interval_seconds", 2.2)
        target = binding.get("expected_target_name")
        if isinstance(count, bool) or not isinstance(count, int) or count not in ((1,) if single else (2, 3)):
            raise ExecutionRejected("cast_burst count must be the integer 2 or 3")
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not math.isfinite(interval) or interval < 1.5 or interval > 3.0):
            raise ExecutionRejected("cast_burst interval_seconds must be between 1.5 and 3.0 seconds")
        if not plain_target_name(target):
            raise ExecutionRejected("cast_burst requires a plain expected_target_name")
        if self.batch_guard is None:
            raise ExecutionRejected("cast_burst requires the runtime's fresh-frame batch guard")

        completed: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        post_observations: list[dict[str, Any]] = []
        opening: list[dict[str, Any]] = []
        deadline = time.monotonic() + self.CAST_BURST_MAX_SECONDS

        def settled_cancel(reason: str) -> dict[str, Any]:
            # Perception and pacing failures do not make earlier completed
            # native commands uncertain. Machine-control protections still
            # stop execution, and unfinished primitive traces remain failures.
            self._validate_gate()
            if not self._armed or self._stopped.is_set():
                raise ExecutionRejected("executor stopped during cast_burst")
            if any(step.get("status") != "completed" for step in self._active_input_steps or []):
                raise ExecutionRejected("cast_burst has unreconciled native input")
            return {
                "dispatched": bool(completed), "kind": "cast_burst", "spell": binding["spell"],
                "expected_target_name": target, "requested_count": count,
                "completed": True, "burst_completed": False, "completed_steps": completed,
                "remaining_steps": count-len(completed),
                "guard_observations": observations, "post_cast_observations":post_observations,
                "opening_commands":opening, "cancelled": True,
                "cancel_reason": reason,
                "cast_result": "unverified; game evaluates harm,nodead",
            }

        async def pause(seconds):
            if self._batch_sleep is asyncio.sleep:
                try:await asyncio.wait_for(self._stopped.wait(),timeout=seconds)
                except asyncio.TimeoutError:pass
            else:await self._batch_sleep(seconds)
            if self._stopped.is_set() or not self._armed:
                raise ExecutionRejected("executor stopped during cast_burst pacing")
            self._validate_gate()

        async def post_guard(index,phase):
            remaining=deadline-time.monotonic()
            if remaining<=0:return settled_cancel('cast_burst deadline before post-cast observation')
            try:
                fact=await asyncio.wait_for(self.batch_post_guard(binding,index,phase),
                    timeout=min(self.CAST_BURST_GUARD_MAX_SECONDS,remaining))
            except asyncio.TimeoutError:
                fact={'continue':False,'reason':'post-cast observation timeout','guard_failure':'timeout'}
            except Exception as exc:
                fact={'continue':False,'reason':f'post-cast observation failed: {type(exc).__name__}',
                      'guard_failure':type(exc).__name__}
            if not isinstance(fact,dict) or not isinstance(fact.get('continue'),bool):
                fact={'continue':False,'reason':'post-cast observation malformed','guard_failure':'malformed_decision'}
            fact={**fact,'phase':phase,'step_index':index}
            post_observations.append(fact)
            await self._notify_batch_step(binding,index,phase,fact)
            if not fact['continue']:return settled_cancel(fact.get('reason','post-cast observation stopped remaining pulses'))
            return None

        try:
            for index in range(count):
                if not self._armed:
                    raise ExecutionRejected("executor stopped during cast_burst")
                self._validate_gate()
                remaining = deadline - time.monotonic()
                if remaining <= self.CAST_BURST_MIN_SUBMISSION_SECONDS:
                    return settled_cancel("cast_burst reached its bounded deadline before another pulse")
                try:
                    observation = await asyncio.wait_for(
                        self.batch_guard(binding, index),
                        timeout=min(self.CAST_BURST_GUARD_MAX_SECONDS,
                                    remaining - self.CAST_BURST_MIN_SUBMISSION_SECONDS))
                except asyncio.TimeoutError:
                    observation = {"continue": False, "reason": "cast_burst fresh-frame guard exceeded its remaining time budget",
                                   "guard_failure": "timeout"}
                except Exception as exc:
                    observation = {"continue": False, "reason": f"cast_burst fresh-frame guard failed: {type(exc).__name__}",
                                   "guard_failure": type(exc).__name__}
                if not isinstance(observation, dict) or not isinstance(observation.get("continue"), bool):
                    observation = {"continue": False, "reason": "cast_burst fresh-frame guard returned no valid decision",
                                   "guard_failure": "malformed_decision"}
                observations.append(observation)
                await self._notify_batch_step(binding, index, "guard_observed", observation)
                if not observation["continue"]:
                    return settled_cancel(observation.get("reason", "fresh-frame guard stopped the burst"))

                # Recheck after image analysis, immediately before each native
                # conditional cast. WoW evaluates harm,nodead at submission.
                self._validate_gate()
                if not self._armed:
                    raise ExecutionRejected("executor stopped before cast_burst pulse")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return settled_cancel("cast_burst reached its bounded deadline before command submission")
                try:
                    if index==0 and start_attack:
                        await asyncio.wait_for(self._submit_command('/startattack [harm,nodead]'),timeout=remaining)
                        opening.append({'command':'/startattack [harm,nodead]','dispatched':True})
                        remaining=deadline-time.monotonic()
                        if remaining<=self.CAST_BURST_MIN_SUBMISSION_SECONDS:
                            return settled_cancel('cast_burst deadline after opening autoattack before Smite')
                    await asyncio.wait_for(self._submit_command(command), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise ExecutionRejected("cast_burst reached its bounded deadline during command submission") from exc
                completed.append({"index": index + 1, "command": command,
                                  "submitted_at_monotonic": time.monotonic(),
                                  "guard_frame_id": observation.get("frame_id"),
                                  "dispatched": True, "cast_result": "unverified"})
                await self._notify_batch_step(binding, index, "pulse_dispatched", completed[-1])
                post_elapsed=0.0
                if self.batch_post_guard is not None:
                    post_started=time.monotonic()
                    if deadline-post_started<=.35:
                        return settled_cancel('cast_burst deadline before early post-cast observation')
                    await pause(.35)
                    canceled=await post_guard(index,'early_post_cast')
                    if canceled is not None:return canceled
                    post_elapsed=time.monotonic()-post_started
                if index + 1 < count or self.batch_post_guard is not None:
                    # Keep the authorized interval while adding sensing; no held key.
                    delay=max(0.0,float(interval)-post_elapsed)
                    remaining=deadline-time.monotonic()
                    if remaining<=delay+self.CAST_BURST_MIN_SUBMISSION_SECONDS:
                        return settled_cancel("cast_burst deadline leaves insufficient time for its next guarded pulse")
                    if delay:await pause(delay)
                    if index+1==count and self.batch_post_guard is not None:
                        canceled=await post_guard(index,'settled_post_cast')
                        if canceled is not None:return canceled

        except BaseException as exc:
            # DecisionCycle records this evidence beside the action request so
            # a partial burst cannot look like a complete one.
            try:
                exc.completed_steps = completed
                exc.guard_observations = observations
            except Exception:
                pass
            raise
        return {
            "dispatched": bool(completed), "kind": "cast_burst", "spell": binding["spell"],
            "expected_target_name": target, "requested_count": count,
            "completed": True, "burst_completed": len(completed) == count, "completed_steps": completed,
            "remaining_steps": count-len(completed),
            "guard_observations": observations, "post_cast_observations":post_observations,
            "opening_commands":opening, "cancelled": False,
            "cast_result": "unverified; game evaluates harm,nodead",
        }

    async def _notify_batch_step(self, binding: dict[str, Any], index: int,
                                 phase: str, evidence: dict[str, Any]) -> None:
        if self.batch_step_callback is None:
            return
        result = self.batch_step_callback({
            "kind": "cast_burst",
            "phase": phase,
            "step_index": index,
            "requested_count": binding["count"],
            "spell": binding["spell"],
            "expected_target_name": binding["expected_target_name"],
            "evidence": evidence,
        })
        if asyncio.iscoroutine(result):
            await result

    async def _target_and_cast(self, binding: dict[str, Any]) -> dict[str, Any]:
        name, spell = binding.get('name'), binding.get('spell')
        if not plain_target_name(name):
            raise ExecutionRejected('target_and_cast requires a plain mob name')
        cast_command = self._guarded_cast_command(binding)
        completed=[]
        try:
            for command in ('/cleartarget', '/targetexact '+name, cast_command):
                await self._submit_command(command)
                completed.append(command)
                await asyncio.sleep(.08)
        except Exception as exc:
            exc.completed_steps=list(completed)
            raise
        return {'dispatched':True,'kind':'target_and_cast','name':name,'spell':spell,
                'completed_steps':completed,'cast_result':'unverified; game evaluates harm,nodead'}

    async def _submit_command(self, command: str) -> None:
        """Only callers above construct commands; every physical step is gated."""
        for key in (36,):  # model precondition: normal world view, no active text entry
            if not self._armed:
                raise ExecutionRejected("executor stopped during targeting")
            await self._keypress({"keycode": key, "hold_seconds": .05})
            await asyncio.sleep(.08)
        self._validate_gate()
        if not self._armed:
            raise ExecutionRejected("executor stopped before command text")
        self._physical("text", {"length": len(command)}, lambda: self.backend.text(command))
        await asyncio.sleep(.12)
        self._validate_gate()
        if not self._armed:
            raise ExecutionRejected("executor stopped before command submission")
        await self._keypress({"keycode": 36, "hold_seconds": .05})

    async def _submit_ui_command(self, command: str) -> None:
        """Cancel any active text entry before a fixed, allowlisted UI command."""
        # UI/camera choices have no user-supplied text. Escape first ensures
        # that the following Enter opens the command-entry field instead of
        # submitting text that may already be in chat. Every key pulse and the
        # text dispatch independently recheck focus, calibration and stop state.
        await self._keypress({"keycode": 53, "hold_seconds": .05})  # Escape
        await asyncio.sleep(.08)
        self._validate_gate()
        if not self._armed:
            raise ExecutionRejected("executor stopped after cancelling text entry")
        await self._keypress({"keycode": 36, "hold_seconds": .05})  # Enter
        await asyncio.sleep(.08)
        self._validate_gate()
        if not self._armed:
            raise ExecutionRejected("executor stopped before fixed UI command text")
        self._physical("text", {"length": len(command)}, lambda: self.backend.text(command))
        await asyncio.sleep(.12)
        self._validate_gate()
        if not self._armed:
            raise ExecutionRejected("executor stopped before fixed UI command submission")
        await self._keypress({"keycode": 36, "hold_seconds": .05})  # Submit command

    async def _click(self, binding: dict[str, Any], frame_size: tuple[int, int] | None) -> dict[str, Any]:
        if frame_size is None:
            raise ExecutionRejected("click needs the frame dimensions used to propose its pixel coordinate")
        x, y = binding.get("image_x"), binding.get("image_y")
        width, height = frame_size
        if not all(isinstance(value, int) for value in (x, y, width, height)) or width <= 0 or height <= 0:
            raise ExecutionRejected("click coordinates and frame dimensions must be integers")
        if not (0 <= x < width and 0 <= y < height):
            raise ExecutionRejected("click coordinate is outside the captured image")
        gate = self._validate_gate()
        desktop_x, desktop_y = gate.bounds.image_to_desktop(x, y, width, height)
        button = binding.get("button", 0)
        if button not in (0, 1):
            raise ExecutionRejected("only primary and secondary mouse buttons are supported")
        if hasattr(self.backend, "mouse_move"):
            self._physical("mouse_move", {"x": desktop_x, "y": desktop_y},
                           lambda: self.backend.mouse_move(desktop_x, desktop_y))
            await asyncio.sleep(0.05)
        self._validate_gate()
        self._physical("mouse_down", {"x": desktop_x, "y": desktop_y, "button": button},
                       lambda: self.backend.mouse_button(desktop_x, desktop_y, button, True))
        await asyncio.sleep(0.08)
        if not self._armed:
            raise ExecutionRejected("executor disarmed during click")
        self._validate_gate()
        self._physical("mouse_up", {"x": desktop_x, "y": desktop_y, "button": button},
                       lambda: self.backend.mouse_button(desktop_x, desktop_y, button, False))
        return {"dispatched": True, "kind": "click", "image_x": x, "image_y": y,
                "desktop_x": desktop_x, "desktop_y": desktop_y, "button": button}

    async def _hover(self, binding: dict[str, Any], frame_size: tuple[int, int] | None) -> dict[str, Any]:
        """Move to a Sage-selected point and wait briefly for a tooltip.

        Hover has no click or activation semantics. Its pause is tightly
        bounded because it is intended only to let a current UI tooltip render;
        the next screenshot and Sage choice must decide what the tooltip means.
        """
        if frame_size is None:
            raise ExecutionRejected("hover needs the frame dimensions used to propose its pixel coordinate")
        x, y = binding.get("image_x"), binding.get("image_y")
        width, height = frame_size
        if not all(isinstance(value, int) and not isinstance(value, bool)
                   for value in (x, y, width, height)) or width <= 0 or height <= 0:
            raise ExecutionRejected("hover coordinates and frame dimensions must be integers")
        if not (0 <= x < width and 0 <= y < height):
            raise ExecutionRejected("hover coordinate is outside the captured image")
        pause = self._duration(binding.get("pause_seconds", 0.35), maximum=1.5)
        gate = self._validate_gate()
        if not hasattr(self.backend, "mouse_move"):
            raise ExecutionRejected("input backend does not support guarded mouse hover")
        desktop_x, desktop_y = gate.bounds.image_to_desktop(x, y, width, height)
        self._physical("mouse_move", {"x": desktop_x, "y": desktop_y, "purpose": "tooltip_inspection"},
                       lambda: self.backend.mouse_move(desktop_x, desktop_y))
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=pause)
        except asyncio.TimeoutError:
            if not self._armed:
                raise ExecutionRejected("executor stopped during hover")
            self._validate_gate()
        else:
            raise ExecutionRejected("executor stopped during hover")
        return {"dispatched": True, "kind": "hover", "image_x": x, "image_y": y,
                "desktop_x": desktop_x, "desktop_y": desktop_y, "pause_seconds": pause}

    def _validate_gate(self) -> GateSnapshot:
        gate = self.inspect_gate()
        if not gate.valid:
            self._disarm("focus_or_calibration_invalid")
            raise ExecutionRejected("selected window lost focus or no longer matches its calibration")
        return gate

    async def _watch(self) -> None:
        try:
            while self._armed and not self._stopped.is_set():
                await asyncio.sleep(self.watchdog_interval)
                if not self._armed:
                    return
                try:
                    gate=await self.inspect_gate_periodic('executor_watch')
                    if gate is not None and not gate.valid:return
                except ExecutionRejected:
                    return
        except asyncio.CancelledError:
            raise

    def _watch_heartbeat(self, stop: threading.Event) -> None:
        while not stop.wait(self.watchdog_interval):
            lock_requested = time.monotonic()
            with self._input_lock:
                if stop.is_set() or not self._armed:
                    return
                lock_wait = time.monotonic() - lock_requested
                if self._expire_heartbeat("watchdog_thread"):
                    if self.last_watchdog_trip is not None:
                        self.last_watchdog_trip['input_lock_wait_seconds'] = lock_wait
                    return

    def _expire_heartbeat(self, source: str) -> bool:
        """Called with the input lock held, including immediately before dispatch."""
        if not self._armed or time.monotonic() - self._last_heartbeat <= self.heartbeat_timeout:
            return False
        try:
            self._disarm("controller_heartbeat_lost", source=source)
        except BaseException:
            # The durable stop path will retry release. Retire input authority
            # even if a backend release fails; retain its diagnostic below.
            pass
        return True

    def _disarm(self, reason: str, *, source: str = "event_loop", timeout_context=None) -> None:
        with self._input_lock:
            expired_lease=self._gate_lease
            self._gate_lease+=1
            was_armed = self._armed
            if self._armed or reason != "action_failed_or_interrupted":
                self._disarm_reason = reason
            self._armed = False
            if self._heartbeat_watch_stop is not None:
                self._heartbeat_watch_stop.set()
            trip = self.timing_snapshot()
            started = time.monotonic()
            trip.update({"reason": reason, "source": source,
                         "execution_id": self._active_execution_id,
                         "detected_at_monotonic": started,
                         "last_heartbeat_at_monotonic": self._last_heartbeat,
                         "held_inputs": [{"kind": kind, "code": code,
                                          "held_seconds": started - since}
                                         for (kind, code), since in self._held_since.items()],
                         "release_started_at_monotonic": started, "release_error": None})
            try:
                self.backend.release_all()
                self._held_since.clear()
            except BaseException as exc:
                trip["release_error"] = type(exc).__name__
                raise
            finally:
                finished = time.monotonic()
                trip["release_attempt_completed_at_monotonic"] = finished
                trip["release_duration_seconds"] = finished - started
                trip["heartbeat_to_release_seconds"] = finished - self._last_heartbeat
                if was_armed:
                    if reason == "controller_heartbeat_lost":
                        # Release happens first. Stack inspection never delays
                        # the physical key/button cleanup it is diagnosing.
                        trip['event_loop_stack'] = self._loop_stack_snapshot()
                    self._watchdog_events.append(trip)
                    if reason == "controller_heartbeat_lost":
                        self.last_watchdog_trip = trip
                    if (reason=='periodic_gate_inspection_timeout' and timeout_context
                            and timeout_context.get('request_lease')==expired_lease):
                        self._periodic_timeout_trip=deepcopy({**trip,**timeout_context,
                            'execution_active':self._active_input_steps is not None or self._lock.locked(),
                            'expired_lease':expired_lease,'retired_lease':self._gate_lease})

    @classmethod
    def _duration(cls, value: Any, maximum: float | None = None) -> float:
        limit = maximum if maximum is not None else cls.MAX_ACTION_SECONDS
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0 or value > limit:
            raise ExecutionRejected(f"duration must be greater than zero and no more than {limit:g} seconds")
        return float(value)
