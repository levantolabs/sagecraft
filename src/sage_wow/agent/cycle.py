from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import inspect
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from sage_wow.control.executor import SafeExecutor, ExecutionRejected
from sage_wow.agent.candidates import CandidateConfigurationError, compile_candidates
from sage_wow.control.receipts import execution_receipt
from sage_wow.models import Event, Frame
from sage_wow.sage.client import CandidateSet, DecisionEnvelope, SageClient, SageDecision
from sage_wow.storage import EventStore


@dataclass(frozen=True)
class ActionCandidate:
    option: str
    description: str
    binding: dict[str, Any]
    precondition: Callable[[], bool] = lambda: True
    dispatch_guard: Callable[[Frame], Awaitable["DispatchValidation"]] | None = None


@dataclass(frozen=True)
class DispatchValidation:
    """Continuity veto after a Sage choice; never carries a new action choice."""

    approved: bool
    dispatch_frame: Frame | None = None
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CycleResult:
    status: str
    decision: SageDecision | None = None
    execution: dict[str, Any] | None = None
    detail: str | None = None
    receipt: dict[str, Any] | None = None

    @property
    def no_input_abstention(self) -> bool:
        """A current Sage null choice, distinct from failed/uncertain execution."""
        return (self.status == 'needs_more_evidence' and self.decision is not None
                and self.decision.chosen is None and self.execution is None
                and self.receipt is None)


@dataclass
class CycleExecutionScope:
    """Temporary V2 task lease enforced at request and dispatch boundaries."""

    task_id: str
    objective_revision: int
    session_epoch: str
    deadline_epoch: float
    expected_input_generation: int
    max_frame_age_seconds: float
    is_current: Callable[[], bool]
    scope_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class TravelDecisionBudget:
    """Explicit grinding-only source-to-input lease; shared callers omit it."""

    source_at: float
    deadline: float
    validation_reserve: float = 3.0
    question_kind: str = 'travel'

    def remaining(self):
        return self.deadline - time.time()

    @property
    def expired_status(self):
        return 'travel_deadline_expired' if self.question_kind=='travel' else 'grind_timing_expired'

    @property
    def expired_event(self):
        return 'travel_decision_expired' if self.question_kind=='travel' else 'grind_timing_expired'


def grinding_input_seconds(binding):
    """Minimum executor sleeps for the standalone grinder's physical choices."""
    kind=binding.get('type')
    if kind=='cast_guarded':return .30
    if kind=='keypress_sequence':return .13*len(binding.get('keycodes',[]))
    return float(binding.get('hold_seconds',0)) if kind in {'keypress','keypress_chord'} else 0.


def build_candidate_set(candidates: list[ActionCandidate], *, max_options: int = 20,
                        reserved_options: int = 0) -> CandidateSet:
    return compile_candidates(candidates, max_options=max_options, reserved_options=reserved_options)


class DecisionCycle:
    """One image decision at a time; semantic invalidation precedes all dispatch."""

    def __init__(self, sage: SageClient, executor: SafeExecutor, store: EventStore,
                 *, session_epoch: str | None = None, tactical_max_age_seconds: float = 2.0):
        self.sage = sage
        self.executor = executor
        self.store = store
        self.session_epoch = session_epoch or str(uuid4())
        self.tactical_max_age_seconds = tactical_max_age_seconds
        self._inflight = asyncio.Lock()
        self._input_generation = 0
        self._active_receipt: dict[str, Any] | None = None
        self._execution_scope: ContextVar[CycleExecutionScope | None] = ContextVar(
            f"sage_cycle_scope_{id(self)}", default=None,
        )
        self._receipt_sequence = 0
        self._last_receipt: dict[str, Any] | None = None
        self.navigation = None  # Ephemeral Sage-authorized motor skill; never restored across restart.
        self.visual_advance = None  # Separate Sage visual contract; no coordinate steering authority.
        try:
            row = self.store.connection.execute(
                "SELECT count(*) FROM events WHERE event_type='execution_receipt'"
            ).fetchone()
            self._receipt_sequence = int(row[0]) if row else 0
        except Exception:
            self._receipt_sequence = 0
        try:
            execute_method = getattr(self.executor, "execute")
            execute_parameters = tuple(inspect.signature(execute_method).parameters.values())
            self._executor_accepts_execution_id = any(
                parameter.name == "execution_id" or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in execute_parameters
            )
            self._executor_accepts_pre_dispatch_guard = any(
                parameter.name == 'pre_dispatch_guard' or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in execute_parameters
            )
        except (AttributeError, TypeError, ValueError):
            self._executor_accepts_execution_id = False
            self._executor_accepts_pre_dispatch_guard = False
        register = getattr(self.executor, "set_physical_step_callback", None)
        if callable(register):
            register(self._on_physical_step)

    @property
    def input_generation(self) -> int:
        """Count of physical input primitive attempts observed by this cycle."""
        return self._input_generation

    @property
    def receipt_sequence(self) -> int:
        """Durable receipt sequence, initialized from existing event history."""
        return self._receipt_sequence

    @property
    def last_receipt(self) -> dict[str, Any] | None:
        """Deep snapshot for timeout recovery; callers cannot mutate cycle state."""
        return deepcopy(self._last_receipt)

    @contextmanager
    def scoped_execution_scope(
        self, *, task_id: str, objective_revision: int, session_epoch: str,
        deadline_epoch: float, input_generation: int | None = None,
        max_frame_age_seconds: float = 10.0,
        is_current: Callable[[], bool],
    ):
        """Apply a temporary V2 task lease to all DecisionCycle paths in scope.

        The scope follows the async context (including TacticalGameplay's early
        helpers), rejects stale task/session/input state, and is restored even
        when an adapter raises or is cancelled.
        """
        if (not isinstance(task_id, str) or not task_id
                or isinstance(objective_revision, bool) or not isinstance(objective_revision, int)
                or objective_revision < 1 or not isinstance(session_epoch, str) or not session_epoch
                or isinstance(deadline_epoch, bool) or not isinstance(deadline_epoch, (int, float))
                or not math.isfinite(float(deadline_epoch))
                or isinstance(max_frame_age_seconds, bool)
                or not isinstance(max_frame_age_seconds, (int, float))
                or not math.isfinite(float(max_frame_age_seconds)) or max_frame_age_seconds <= 0
                or not callable(is_current)):
            raise ValueError("invalid DecisionCycle execution scope")
        expected_generation = self._input_generation if input_generation is None else input_generation
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 0:
            raise ValueError("scope input_generation must be a non-negative integer")
        scope = CycleExecutionScope(task_id, objective_revision, session_epoch,
                                    float(deadline_epoch), expected_generation,
                                    float(max_frame_age_seconds), is_current)
        token = self._execution_scope.set(scope)
        try:
            yield scope
        finally:
            self._execution_scope.reset(token)

    def _scope_error(self, frame: Frame) -> str | None:
        scope = self._execution_scope.get()
        if scope is None:
            return None
        if time.time() >= scope.deadline_epoch:
            return "task/session deadline expired"
        if self.session_epoch != scope.session_epoch:
            return "session epoch changed"
        if self._input_generation != scope.expected_input_generation:
            return "input generation changed outside this scoped execution"
        age = _frame_age_seconds(frame)
        if age > scope.max_frame_age_seconds:
            return f"source frame is stale ({age:.3f}s)"
        try:
            if not scope.is_current():
                return "task, parent review, pause, or session scope is no longer current"
        except Exception as exc:
            return f"scope validator failed closed ({type(exc).__name__})"
        return None

    def _scope_rejected(self, frame: Frame, phase: str, reason: str) -> CycleResult:
        scope = self._execution_scope.get()
        self.store.append(Event.create("scoped_decision_rejected", {
            "phase": phase, "reason": reason, "frame_id": frame.frame_id,
            "scope_id": scope.scope_id if scope else None,
            "task_id": scope.task_id if scope else None,
            "objective_revision": scope.objective_revision if scope else None,
            "session_epoch": scope.session_epoch if scope else self.session_epoch,
            "input_generation": self._input_generation,
        }))
        return CycleResult("scope_rejected", detail=reason)

    @staticmethod
    def _validate_dispatch_frame(original: Frame, fresh: Any, *, max_age_seconds: float = 1.5) -> str | None:
        if not isinstance(fresh, Frame):
            return "continuity guard did not return a Frame"
        if fresh.frame_id == original.frame_id:
            return "continuity guard returned the source frame again"
        if (fresh.source != original.source or fresh.width != original.width
                or fresh.height != original.height):
            return "continuity frame source or geometry changed"
        if fresh.image_path is None and fresh.image_data is None:
            return "continuity frame has no image payload"
        try:
            captured = datetime.fromisoformat(fresh.captured_at)
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
            original_captured = datetime.fromisoformat(original.captured_at)
            if original_captured.tzinfo is None:
                original_captured = original_captured.replace(tzinfo=timezone.utc)
            if captured.timestamp() < original_captured.timestamp():
                return "continuity frame predates the Sage source frame"
            now = time.time()
            if captured.timestamp() > now + 0.25:
                return "continuity frame timestamp is in the future"
            age = now - captured.timestamp()
        except (TypeError, ValueError, OverflowError):
            return "continuity frame timestamp is invalid"
        if age > max_age_seconds:
            return f"continuity frame is stale ({age:.3f}s)"
        return None

    def _log_dispatch_guard(self, *, request_id: str, frame: Frame, authorization_epoch: str,
                            chosen_option: str, approved: bool, detail: str,
                            elapsed_ms: float, evidence: Any,
                            dispatch_frame: Frame | None = None) -> None:
        scope = self._execution_scope.get()
        safe_evidence: dict[str, Any]
        if isinstance(evidence, dict):
            try:
                encoded = json.dumps(evidence, sort_keys=True, allow_nan=False,
                                     separators=(",", ":"))
                if len(encoded.encode("utf-8")) <= 4096:
                    safe_evidence = json.loads(encoded)
                else:
                    safe_evidence = {"truncated": True,
                                     "encoded_bytes": len(encoded.encode("utf-8"))}
            except (TypeError, ValueError):
                safe_evidence = {"invalid_evidence": True}
        else:
            safe_evidence = {}
        self.store.append(Event.create("dispatch_guard_checked", {
            "request_id": request_id, "source_frame_id": frame.frame_id,
            "dispatch_frame_id": dispatch_frame.frame_id if dispatch_frame else None,
            "chosen": chosen_option, "approved": bool(approved),
            "detail": str(detail or "")[:500],
            "elapsed_ms": round(max(0.0, elapsed_ms), 3),
            "evidence": safe_evidence,
            "scope_id": scope.scope_id if scope else None,
            "task_id": scope.task_id if scope else None,
            "objective_revision": scope.objective_revision if scope else None,
            "session_epoch": authorization_epoch,
            "current_session_epoch": self.session_epoch,
            "input_generation": self._input_generation,
        }))

    def _on_physical_step(self, step: dict[str, Any]) -> None:
        self._input_generation += 1
        step["input_generation"] = self._input_generation
        step["monotonic_at"] = time.monotonic()
        active = self._active_receipt
        bound = active is not None and step.get("execution_id") == active["receipt_id"]
        if bound:
            active["input_steps"].append(step)
        self.store.append(Event.create("physical_input_attempted", {
            "receipt_id": active["receipt_id"] if bound else None,
            "request_id": active["request_id"] if bound else None,
            "source_frame_id": active["source_frame_id"] if bound else None,
            "session_epoch": active["session_epoch"] if bound else self.session_epoch,
            "execution_id": step.get("execution_id"), "bound_to_active_receipt": bool(bound),
            "step": step,
        }))

    def _make_receipt(self, context: dict[str, Any], *, execution: dict[str, Any] | None,
                      error: str | None = None) -> dict[str, Any]:
        receipt = execution_receipt(
            request_id=context["request_id"], source_frame_id=context["source_frame_id"],
            dispatch_frame_id=context.get("dispatch_frame_id"),
            session_epoch=context["session_epoch"], candidate_set_version=context["candidate_set_version"],
            chosen_option=context["chosen_option"], selected_binding=context["selected_binding"],
            generation_before=context["generation_before"], generation_after=self._input_generation,
            attempted=True, input_steps=context["input_steps"], execution=execution,
            error=error, receipt_id=context["receipt_id"],
            authorization_type=context.get("authorization_type", "sage_decision"),
            authorization_id=context.get("authorization_id"),
        )
        if context.get("authorization_reference"):
            receipt["authorization_reference"] = context["authorization_reference"]
            receipt["transaction_id"] = context.get("authorization_id")
        scope = self._execution_scope.get()
        if scope is not None:
            receipt.update({
                "scope_id": scope.scope_id,
                "scope_task_id": scope.task_id,
                "scope_objective_revision": scope.objective_revision,
                "scope_session_epoch": scope.session_epoch,
                "scope_input_generation": context["generation_before"],
            })
        return receipt

    def _persist_receipt(self, receipt: dict[str, Any]) -> None:
        next_sequence = self._receipt_sequence + 1
        receipt["receipt_sequence"] = next_sequence
        self.store.append(Event.create("execution_receipt", receipt))
        self._receipt_sequence = next_sequence
        self._last_receipt = deepcopy(receipt)
        scope = self._execution_scope.get()
        if (scope is not None and receipt.get("scope_id") == scope.scope_id
                and receipt.get("possible_input")):
            after = receipt.get("generation_after")
            if isinstance(after, int) and not isinstance(after, bool):
                scope.expected_input_generation = after

    def _record_legacy_steps(self, context: dict[str, Any], *, completed_steps=None,
                             affirmative_dispatch: bool = False, uncertain: bool = False) -> None:
        """Conservatively account for older executors lacking primitive callbacks."""
        if context["input_steps"]:
            return
        count = len(completed_steps or [])
        if count:
            statuses = ["completed"] * count
        elif affirmative_dispatch or uncertain:
            statuses = ["effect_unknown"]
        else:
            statuses = []
        for index, status in enumerate(statuses, 1):
            self._on_physical_step({
                "step_id": str(uuid4()), "execution_id": context["receipt_id"],
                "kind": "legacy_executor_dispatch", "details":{"legacy_step_index": index},
                "attempted": True, "status": status,
            })

    async def _dispatch_with_receipt(self, binding: dict[str, Any], *, frame: Frame,
                                     request_id: str, session_epoch: str,
                                     candidate_set_version: str, chosen_option: str,
                                     authorization_type: str = "sage_decision",
                                     authorization_id: str | None = None,
                                     authorization_reference: str | None = None,
                                     source_frame_id: str | None = None,
                                     authority_check: Callable[[], str | None] | None = None,
                                     authority_fence: Callable[[], str | None] | None = None,
                                     source_max_age_seconds: float | None = None) -> CycleResult:
        if getattr(getattr(self, 'evidence_archive', None), 'failed', None):
            await self.executor.stop('evidence_retention_failed')
            return CycleResult('evidence_retention_failed')
        try:
            stable_binding = json.loads(json.dumps(binding, sort_keys=True, allow_nan=False))
        except (TypeError, ValueError) as exc:
            return CycleResult("candidate_configuration_error", detail=f"selected binding is not JSON-safe: {exc}")
        scope = self._execution_scope.get()
        scope_error = self._scope_error(frame)
        if scope_error:
            return self._scope_rejected(frame, "pre_dispatch", scope_error)
        receipt_id = str(uuid4())
        context = {
            "receipt_id": receipt_id, "request_id": request_id,
            "source_frame_id": source_frame_id or frame.frame_id,
            "dispatch_frame_id": frame.frame_id, "session_epoch": session_epoch,
            "candidate_set_version": candidate_set_version, "chosen_option": chosen_option,
            "selected_binding": stable_binding, "generation_before": self._input_generation,
            "input_steps": [], "authorization_type": authorization_type,
            "authorization_id": authorization_id, "authorization_reference": authorization_reference,
        }
        self._active_receipt = context
        context_snapshot = deepcopy(context)
        scope_snapshot = dict(vars(scope)) if scope is not None else None
        frame_snapshot = deepcopy(frame)
        binding_snapshot = deepcopy(stable_binding)
        max_age = (source_max_age_seconds if source_max_age_seconds is not None else
                   scope.max_frame_age_seconds if scope is not None else self.tactical_max_age_seconds)

        def shared_fence():
            if (self._active_receipt is not context or context != context_snapshot
                    or self._execution_scope.get() is not scope
                    or (dict(vars(scope)) if scope is not None else None) != scope_snapshot):
                return 'execution receipt or scope changed before first input'
            if self.session_epoch != session_epoch or self._input_generation != context['generation_before']:
                return 'session epoch or input generation changed before first input'
            if frame != frame_snapshot or _frame_age_seconds(frame) > max_age:
                return 'dispatch source frame changed or expired before first input'
            if stable_binding != binding_snapshot or binding != binding_snapshot:
                return 'execution binding changed before first input'
            if getattr(getattr(self, 'evidence_archive', None), 'failed', None):
                return 'evidence retention failed before first input'
            return None

        def pre_dispatch_guard():
            reason = shared_fence()
            if reason:
                return reason
            reason = self._scope_error(frame)
            if reason:
                return reason
            if authority_check is not None:
                reason = authority_check()
                if inspect.isawaitable(reason):
                    if inspect.iscoroutine(reason):
                        reason.close()
                    return 'request authority check must be synchronous'
                if reason is not None:
                    return reason if isinstance(reason, str) and reason else 'invalid request authority check result'
            elif authorization_type not in {'prior_sage_continuation', 'user_authorized_observation',
                                             'user_requested_ui_reset'}:
                return 'request-specific first input authority is unavailable'
            # Both the request precondition and is_current callback may mutate
            # state synchronously. The captured fields are never refreshed.
            reason = shared_fence() or self._scope_error(frame)
            if not reason and authority_fence is not None:
                reason = authority_fence()
            return reason or shared_fence()

        try:
            execute_kwargs = {"frame_size": (frame.width, frame.height)}
            if self._executor_accepts_execution_id:
                execute_kwargs["execution_id"] = receipt_id
            if stable_binding.get('type') in {'keypress', 'keypress_chord'}:
                if self._executor_accepts_pre_dispatch_guard:
                    execute_kwargs['pre_dispatch_guard'] = pre_dispatch_guard
                elif isinstance(self.executor, SafeExecutor):
                    raise ExecutionRejected('live executor lacks first input authority guard support')
            remaining = None if scope is None else scope.deadline_epoch - time.time()
            if remaining is not None and remaining <= 0:
                self._active_receipt = None
                return self._scope_rejected(frame, "pre_dispatch", "task/session deadline expired")
            execution_call = self.executor.execute(deepcopy(stable_binding), **execute_kwargs)
            if scope is None:
                execution = await execution_call
            else:
                execution = await asyncio.wait_for(execution_call, timeout=remaining)
        except asyncio.CancelledError as exc:
            self._record_legacy_steps(context, completed_steps=getattr(exc, "completed_steps", []),
                                      uncertain=(stable_binding.get("type") not in {"observe_only", "wait"}
                                                 and not bool(context["input_steps"])))
            execution = {"dispatched": bool(context["input_steps"]), "completed": False,
                         "completed_steps": deepcopy(getattr(exc, "completed_steps", [])),
                         "input_steps": context["input_steps"], "error_type": "CancelledError"}
            receipt = self._make_receipt(context, execution=execution, error="CancelledError")
            self._persist_receipt(receipt)
            self._active_receipt = None
            raise
        except Exception as exc:
            self._record_legacy_steps(context, completed_steps=getattr(exc, "completed_steps", []),
                                      affirmative_dispatch=bool(getattr(exc, "dispatched", False)),
                                      uncertain=(isinstance(exc, asyncio.TimeoutError)
                                                 and stable_binding.get("type") not in {"observe_only", "wait"}))
            steps = context["input_steps"]
            execution = {"dispatched": bool(steps) or bool(getattr(exc, "completed_steps", [])),
                         "completed": False, "completed_steps": getattr(exc, "completed_steps", []),
                         "input_steps": steps, "error_type": type(exc).__name__}
            receipt = self._make_receipt(context, execution=execution, error=str(exc)[:500])
            self._persist_receipt(receipt)
            self.store.append(Event.create("action_execution_failed", {
                "request_id": request_id, "frame_id": frame.frame_id, "chosen": chosen_option,
                "error_type": type(exc).__name__, "error": str(exc)[:500],
                "authorization_type": authorization_type, "authorization_id": authorization_id,
                "execution": execution,
            }))
            self._active_receipt = None
            return CycleResult("execution_failed", execution=execution, detail=str(exc), receipt=receipt)
        if stable_binding.get("type") != "observe_only" and execution.get("dispatched"):
            self._record_legacy_steps(context, affirmative_dispatch=True)
        receipt = self._make_receipt(context, execution=execution)
        self._persist_receipt(receipt)
        self.store.append(Event.create("action_dispatched", {
            "request_id": request_id, "frame_id": frame.frame_id, "chosen": chosen_option,
            "candidate_set_version": candidate_set_version, "authorization_type": authorization_type,
            "authorization_id": authorization_id, "execution": execution,
        }))
        self._active_receipt = None
        return CycleResult("dispatched", execution=execution, receipt=receipt)

    async def execute_observation(self, binding: dict[str, Any], *, frame: Frame,
                                  transaction_id: str, authorization_reference: str,
                                  chosen_option: str) -> CycleResult:
        """Fixed user-authorized HUD observation, independent of Sage decisions.

        Only hide/restore of the historically verified chord is accepted. All
        ordinary scope, input generation, executor and receipt guards remain.
        """
        expected={'type':'keypress_chord','keycodes':[58,6],'hold_seconds':.08}
        if (binding!=expected or chosen_option not in {'grind_hud_hide','grind_hud_restore','grind_hud_restore_correction'}
                or not transaction_id or not authorization_reference or self._execution_scope.get() is None):
            return CycleResult('authorization_invalid',detail='HUD observation provenance/binding/scope unavailable')
        if self._inflight.locked():return CycleResult('decision_in_flight')
        async with self._inflight:
            if self._scope_error(frame):
                return self._scope_rejected(frame,'observation_dispatch',self._scope_error(frame))
            return await self._dispatch_with_receipt(binding,frame=frame,
                request_id='observation:'+transaction_id,session_epoch=self.session_epoch,
                candidate_set_version='grind-clean-world-v1',chosen_option=chosen_option,
                authorization_type='user_authorized_observation',authorization_id=transaction_id,
                authorization_reference=authorization_reference)

    async def execute_authorized(self, binding: dict[str, Any], *, frame: Frame,
                                 chosen_option: str, authorization_id: str, request_id: str,
                                 candidate_set_version: str, source_frame_id: str | None = None,
                                 session_epoch: str | None = None,
                                 max_frame_age_seconds: float | None = None) -> CycleResult:
        """Execute a fixed continuation explicitly authorized by an earlier Sage decision.

        This path makes no new model call and records distinct provenance. The
        caller must supply the originating request/branch identity and only a
        binding already authorized by that prior decision.
        """
        if not all(isinstance(value, str) and value for value in
                   (chosen_option, authorization_id, request_id, candidate_set_version)):
            return CycleResult("authorization_invalid", detail="continuation provenance is incomplete")
        if getattr(getattr(self, 'evidence_archive', None), 'failed', None):
            await self.executor.stop('evidence_retention_failed')
            return CycleResult('evidence_retention_failed')
        if self._inflight.locked():
            return CycleResult("decision_in_flight")
        async with self._inflight:
            epoch = session_epoch or self.session_epoch
            if epoch != self.session_epoch:
                return CycleResult("invalidated", detail="continuation belongs to a superseded session")
            max_age = max_frame_age_seconds if max_frame_age_seconds is not None else self.tactical_max_age_seconds
            if _frame_age_seconds(frame) > max_age:
                return CycleResult("stale_frame", detail="continuation dispatch frame is stale")
            if source_frame_id is not None and not isinstance(source_frame_id, str):
                return CycleResult("authorization_invalid", detail="source_frame_id must be a string")
            return await self._dispatch_with_receipt(
                binding, frame=frame, request_id=request_id, session_epoch=epoch,
                candidate_set_version=candidate_set_version, chosen_option=chosen_option,
                authorization_type="prior_sage_continuation", authorization_id=authorization_id,
                source_frame_id=source_frame_id, source_max_age_seconds=max_age,
            )

    async def execute_ui_reset(self, *, frame: Frame, authorization_id: str) -> CycleResult:
        """User-approved mechanical Escape cleanup, separately attributed from Sage."""
        if not authorization_id or self._execution_scope.get() is None:
            return CycleResult('authorization_invalid', detail='UI reset requires current bounded task authority')
        if self._inflight.locked():
            return CycleResult('decision_in_flight')
        async with self._inflight:
            if self._scope_error(frame):
                return CycleResult('invalidated', detail=self._scope_error(frame))
            if getattr(getattr(self, 'evidence_archive', None), 'failed', None):
                return CycleResult('evidence_retention_failed')
            return await self._dispatch_with_receipt(
                {'type':'keypress','keycode':53,'hold_seconds':.08}, frame=frame,
                request_id=authorization_id, session_epoch=self.session_epoch,
                candidate_set_version='user-ui-reset-v1', chosen_option='ui_reset_escape',
                authorization_type='user_requested_ui_reset', authorization_id=authorization_id)

    async def execute_target_opener(self, *, frame: Frame, target_name: str,
                                    target_receipt: dict, authorization_id: str) -> CycleResult:
        """User-authorized one-cast continuation of a completed Sage Tab.

        This is deliberately not recorded as a new Sage casting decision.
        The controller supplies freshly checked current target eligibility.
        """
        from sage_wow.control.target_names import plain_target_name
        receipt = target_receipt
        if (not self._execution_scope.get() or not plain_target_name(target_name)
                or not authorization_id or authorization_id != receipt.get('receipt_id')
                or receipt.get('chosen_option') != 'target_enemy'
                or receipt.get('selected_binding', {}).get('type') != 'keypress'
                or receipt.get('authorization_type') != 'sage_decision'
                or not receipt.get('completed') or not receipt.get('possible_input')
                or receipt.get('dispatch_unknown') or receipt.get('error')
                or receipt.get('session_epoch') != self.session_epoch
                or receipt.get('generation_after') != self.input_generation):
            return CycleResult('authorization_invalid', detail='Completed current Sage targeting receipt required')
        if self._inflight.locked():
            return CycleResult('decision_in_flight')
        async with self._inflight:
            if self._scope_error(frame):
                return self._scope_rejected(frame, 'target_opener', self._scope_error(frame))
            return await self._dispatch_with_receipt(
                {'type': 'cast_guarded', 'spell': 'Smite', 'expected_target_name': target_name},
                frame=frame, request_id=receipt['request_id'], session_epoch=self.session_epoch,
                candidate_set_version='user-target-opener-v1', chosen_option='target_opening_smite',
                authorization_type='user_authorized_target_opener', authorization_id=authorization_id)

    def register_navigation(self, result: CycleResult, frame: Frame) -> bool:
        """Freeze the exact contract selected by Sage; never accepts a bare binding."""
        from sage_wow.agent.coordinate_navigation import CoordinateNavigator, NavigationAuthorization, position
        decision, receipt, scope = result.decision, result.receipt, self._execution_scope.get()
        if (result.status != 'dispatched' or decision is None or decision.chosen not in {'navigate_quest_waypoint', 'navigate_quest_segment'}
                or not scope or self._scope_error(frame) or not receipt
                or self._last_receipt != receipt or receipt.get('authorization_type') != 'sage_decision'
                or not receipt.get('completed') or receipt.get('possible_input') or receipt.get('attempted')
                or receipt.get('request_id') != decision.envelope.request_id
                or receipt.get('source_frame_id') != frame.frame_id
                or receipt.get('scope_task_id') != scope.task_id
                or receipt.get('scope_objective_revision') != scope.objective_revision
                or receipt.get('scope_session_epoch') != scope.session_epoch
                or receipt.get('generation_after') != self.input_generation
                or receipt.get('session_epoch') != self.session_epoch):
            return False
        binding = receipt.get('selected_binding', {})
        contract = binding.get('coordinate_navigation')
        if not isinstance(contract, dict) or binding.get('type') != 'observe_only':
            return False
        destination = position(contract.get('waypoint'))
        keys = contract.get('keys', {})
        if (destination is None or not isinstance(contract.get('zone'), str) or not contract['zone'].strip()
                or not isinstance(contract.get('provenance'), str) or not contract['provenance']
                or contract.get('version') not in {1, 2} or contract.get('seconds') != (30 if contract.get('version') == 2 else 20)
                or contract.get('ineffective_steps') != 3 or contract.get('max_steps') != 12
                or contract.get('forward_seconds') != .8 or contract.get('turn_seconds') != .3
                or contract.get('tolerance') != .2 or contract.get('calibration_probes_authorized') is not True
                or not isinstance(keys, dict) or set(keys) != {'forward', 'turn_left', 'turn_right'}
                or any(isinstance(keys.get(k), bool) or not isinstance(keys.get(k), int)
                       or not 0 <= keys[k] <= 255 for k in ('forward', 'turn_left', 'turn_right'))
                or len(set(keys.values())) != 3):
            return False
        segments = contract['version'] == 2
        if (decision.chosen != ('navigate_quest_segment' if segments else 'navigate_quest_waypoint')
                or (segments and (contract.get('policy') != 'segments'
                    or contract.get('segment_caps') != {'hold_seconds': 2., 'path_points': .6, 'source_frame_seconds': 6.}
                    or not isinstance(contract.get('calibration_scope'), str) or not contract['calibration_scope']))):
            return False
        authorization = NavigationAuthorization(str(uuid4()), decision.envelope.request_id,
            frame.frame_id, scope.task_id, scope.objective_revision, self.session_epoch,
            self.input_generation, time.time(), min(time.time()+contract['seconds'], scope.deadline_epoch), destination,
            contract['zone'], contract['provenance'], keys['forward'], keys['turn_left'], keys['turn_right'],
            policy='segments' if segments else 'per_pulse', calibration_scope=contract.get('calibration_scope', ''))
        previous = self.navigation
        self.navigation = CoordinateNavigator(authorization)
        # Reuse response observations only. A new skill always starts without a
        # heading or visual permission; its own displacement must establish them.
        if segments and previous and self._navigation_calibration_compatible(previous, authorization):
            self.navigation.motion.speed_upper = previous.motion.speed_upper
            self.navigation.turn_rate = previous.turn_rate
            self.store.append(Event.create('coordinate_calibration_reused', {
                'authorization_id': authorization.authorization_id,
                'previous_authorization_id': previous.authorization.authorization_id,
                'source_receipt_id': previous.motion.receipt_id,
                'speed_upper': previous.motion.speed_upper, 'turn_rate': previous.turn_rate,
                'heading_reused': False, 'authority_reused': False,
                'provenance': 'compatible recent measured response; fresh displacement required; input generation cannot exclude undetected external rotation'}))
        if segments and previous and getattr(self, 'cognition_review', False):
            self._reuse_partial_navigation_evidence(previous, self.navigation)
        self.store.append(Event.create('coordinate_navigation_authorized', {
            **authorization.__dict__, 'contract': deepcopy(contract),
            'source': 'actual Sage waypoint-tool choice; bounded deterministic motor authority'}))
        return True

    def _reuse_partial_navigation_evidence(self, previous, current):
        """Evidence continuity across a no-input inspection/task renewal, not authority."""
        a, b = previous.authorization, current.authorization
        gate = getattr(self.executor, 'inspect_gate', lambda: None)()
        compatible = bool(gate and gate.valid and previous.previous_step is None
            and previous.expected_generation == self.input_generation
            and a.session_epoch == b.session_epoch and a.waypoint == b.waypoint
            and a.zone.casefold() == b.zone.casefold() and a.calibration_scope == b.calibration_scope
            and a.provenance == b.provenance
            and (a.forward_key,a.left_key,a.right_key) == (b.forward_key,b.left_key,b.right_key)
            and previous.stop_reason in {None, 'navigation_deadline', 'navigation_insufficient_time_for_pulse',
                'navigation_step_budget', 'navigation_sage_requested_inspection', 'navigation_sage_clearance_uncertain',
                'navigation_sage_clearance_reassess_task', 'navigation_fresh_permission_rejected_or_expired'}
            and previous.last_position is not None and 0 <= time.time()-previous.last_captured_at <= 10
            and not previous.motion.invalid_reason)
        if not compatible:
            return False
        for name in ('last_position','heading_origin','heading','acquisition_probes','ineffective',
                     'turn_rate','turn_measurement','last_motion_measurement','last_captured_at','last_frame_id',
                     'last_turn','oscillations','initial_distance'):
            setattr(current, name, deepcopy(getattr(previous, name)))
        current.motion = deepcopy(previous.motion)
        self.store.append(Event.create('coordinate_partial_evidence_reused', {
            'authorization_id': b.authorization_id, 'previous_authorization_id': a.authorization_id,
            'samples': current.motion.samples, 'source_frame_id': previous.last_frame_id,
            'source_captured_at': previous.last_captured_at, 'authority_reused': False,
            'source': 'same-scope, same-input-generation evidence; fresh Sage permission still required'}))
        return True

    def _navigation_calibration_compatible(self, previous, authorization):
        a = previous.authorization
        allowed_stops = {None, 'navigation_deadline', 'navigation_insufficient_time_for_pulse',
                         'navigation_step_budget'}
        gate = getattr(self.executor, 'inspect_gate', lambda: None)()
        return bool(gate and gate.valid and previous.stop_reason in allowed_stops
            and previous.previous_step is None and previous.ineffective == 0
            and previous.expected_generation == self.input_generation
            and a.session_epoch == authorization.session_epoch
            and a.zone.casefold() == authorization.zone.casefold()
            and a.provenance == authorization.provenance and a.waypoint == authorization.waypoint
            and a.calibration_scope == authorization.calibration_scope
            and (a.forward_key, a.left_key, a.right_key) ==
                (authorization.forward_key, authorization.left_key, authorization.right_key)
            and 0 <= time.time()-previous.motion.measured_at <= 30)

    def register_navigation_permission(self, result, frame, step):
        """A fresh Sage sub-choice; never accepts an inherited/bare permission."""
        from sage_wow.agent.coordinate_segments import SegmentGrant
        n, scope, receipt = self.navigation, self._execution_scope.get(), result.receipt
        if (not n or n.authorization.policy != 'segments' or not scope or self._scope_error(frame)
                or not self.navigation_active_for(scope.task_id, scope.objective_revision)
                or result.status != 'dispatched' or not result.decision or not receipt
                or receipt != self._last_receipt or receipt.get('authorization_type') != 'sage_decision'
                or not receipt.get('completed') or receipt.get('possible_input')
                or receipt.get('request_id') != result.decision.envelope.request_id
                or receipt.get('source_frame_id') != frame.frame_id
                or receipt.get('scope_task_id') != scope.task_id
                or receipt.get('scope_objective_revision') != scope.objective_revision
                or receipt.get('scope_session_epoch') != scope.session_epoch
                or receipt.get('generation_after') != self.input_generation):
            return False
        chosen = result.decision.chosen
        binding = receipt.get('selected_binding', {})
        captured = datetime.fromisoformat(frame.captured_at.replace('Z', '+00:00')).timestamp()
        prepared = (frame.frame_id, captured, self.input_generation)
        if (n._prepared != prepared or time.time() >= n.authorization.deadline
                or binding != {'type': 'observe_only', 'navigation_permission': {
                    'authorization_id': n.authorization.authorization_id,
                    'step': step.__dict__, 'kind': chosen}}):
            return False
        planned = n.plan()
        if not planned or planned.action != step.action:
            return False
        if chosen == 'approve_segment':
            if step.action != 'forward' or not n.motion.eligible(time.time()) or time.time() >= captured+6:
                return False
            n.segment = SegmentGrant(receipt['receipt_id'], receipt['request_id'], frame.frame_id,
                captured, n.authorization.authorization_id, self.input_generation,
                n.motion.heading, n.motion.speed_upper, n.last_position, n.motion.angular_uncertainty)
        elif chosen in {'approve_probe', 'approve_turn'}:
            if (chosen == 'approve_probe' and (step.action != 'forward' or step.seconds != .8)):
                return False
            if chosen == 'approve_turn' and (step.action == 'forward' or step != planned):
                return False
            n.step_permission = {'receipt_id': receipt['receipt_id'], 'frame_id': frame.frame_id,
                'generation': self.input_generation, 'step': step, 'deadline': min(captured+10, n.authorization.deadline)}
            n.segment = None
        else:
            return False
        self.store.append(Event.create('coordinate_navigation_permission', {
            'authorization_id': n.authorization.authorization_id, 'choice': chosen,
            'receipt_id': receipt['receipt_id'], 'source_frame_id': frame.frame_id,
            'segment': n.segment.context() if n.segment else None,
            'step': step.__dict__, 'motion': n.motion.context(),
            'source': 'new Sage permission; not inferred from measurements'}))
        return True

    def navigation_active_for(self, task_id: str, objective_revision: int) -> bool:
        navigation = self.navigation
        if navigation is None or navigation.stop_reason:
            return False
        a = navigation.authorization
        return (a.task_id == task_id and a.objective_revision == objective_revision
                and a.session_epoch == self.session_epoch)

    def visual_advance_active_for(self, task_id: str, objective_revision: int) -> bool:
        grant = self.visual_advance
        return bool(grant and not grant.stop_reason
            and grant.authorization.task_id == task_id
            and grant.authorization.objective_revision == objective_revision
            and grant.authorization.session_epoch == self.session_epoch)

    def bounded_travel_active_for(self, task_id: str, objective_revision: int) -> bool:
        return self.navigation_active_for(task_id, objective_revision) or self.visual_advance_active_for(task_id, objective_revision)

    def stop_navigation(self, reason: str) -> CycleResult:
        navigation = self.navigation
        if navigation:
            navigation.stop(reason)
            self.store.append(Event.create('coordinate_navigation_handback', {
                'authorization_id': navigation.authorization.authorization_id,
                'request_id': navigation.authorization.request_id, 'reason': reason,
                'motor_steps': navigation.steps, 'ineffective_steps': navigation.ineffective,
                'waypoint': navigation.authorization.waypoint, 'progress_claimed': False}))
        return CycleResult('navigation_handback', detail=reason)

    async def execute_navigation_step(self, *, frame: Frame, observation) -> CycleResult:
        """One deterministic pulse under registered waypoint authority, distinct from fixed continuations."""
        if getattr(getattr(self, 'evidence_archive', None), 'failed', None):
            await self.executor.stop('evidence_retention_failed')
            return CycleResult('evidence_retention_failed')
        if self._inflight.locked():
            return CycleResult('decision_in_flight')
        async with self._inflight:
            navigation = self.navigation
            scope = self._execution_scope.get()
            if not navigation or not scope or not self.navigation_active_for(scope.task_id, scope.objective_revision):
                return self.stop_navigation('navigation_authorization_not_current')
            error = self._scope_error(frame)
            if error:
                return self.stop_navigation(error)
            captured = datetime.fromisoformat(frame.captured_at)
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
            if observation.frame_id != frame.frame_id or observation.captured_at != captured.timestamp():
                return self.stop_navigation('navigation_observation_frame_mismatch')
            if observation.input_generation != self.input_generation:
                return self.stop_navigation('navigation_input_generation_changed')
            step = navigation.propose(observation, time.time())
            if step is None:
                return self.stop_navigation(navigation.stop_reason or 'navigation_no_step')
            a = navigation.authorization
            dispatch_deadline = min(a.deadline, scope.deadline_epoch)
            permission_id = None
            if a.policy == 'segments':
                from sage_wow.agent.coordinate_navigation import NavigationStep
                grant, single = navigation.segment, navigation.step_permission
                if single and (single['frame_id'], single['generation']) == (frame.frame_id, self.input_generation):
                    step = single['step']
                    dispatch_deadline = min(dispatch_deadline, single['deadline'])
                    permission_id = single['receipt_id']
                elif grant and step.action == 'forward':
                    seconds = grant.pulse(time.time(), self.input_generation, navigation.motion.heading,
                        angular_uncertainty=navigation.motion.angular_uncertainty or 90)
                    if seconds is None:
                        return self.stop_navigation('navigation_segment_permission_exhausted')
                    step = NavigationStep('forward', seconds, 'sage_authorized_coordinate_segment')
                    dispatch_deadline = min(dispatch_deadline, grant.deadline)
                    permission_id = grant.receipt_id
                else:
                    return self.stop_navigation('navigation_fresh_sage_permission_required')
            if dispatch_deadline-time.time() <= step.seconds:
                return self.stop_navigation('navigation_insufficient_time_for_pulse')
            self.store.append(Event.create('coordinate_navigation_motor_proposal', {
                'authorization_id': a.authorization_id, 'frame_id': frame.frame_id,
                'dispatch_deadline': dispatch_deadline, 'permission_receipt_id': permission_id,
                'observation': observation.__dict__, 'step': step.__dict__,
                'turn_rate': navigation.turn_rate, 'heading': navigation.heading,
                'heading_metric': 'observed displacement in coordinate space; not physical compass calibration'}))
            original_deadline = scope.deadline_epoch
            scope.deadline_epoch = dispatch_deadline
            segment = navigation.segment
            permission = navigation.step_permission
            def navigation_snapshot():
                return deepcopy({
                    'state': {k: v for k, v in vars(navigation).items() if k not in {'motion', 'segment'}},
                    'motion': vars(navigation.motion),
                    'segment': vars(navigation.segment) if navigation.segment is not None else None,
                    'observation': observation, 'step': step,
                })
            sealed_navigation = navigation_snapshot()
            def navigation_fence():
                if (self.navigation is not navigation or navigation.authorization is not a
                        or navigation.segment is not segment or navigation.step_permission is not permission
                        or navigation_snapshot() != sealed_navigation
                        or not self.navigation_active_for(scope.task_id, scope.objective_revision)):
                    return 'navigation authority changed before first input'
                if dispatch_deadline-time.time() <= step.seconds:
                    return 'navigation deadline has insufficient time for approved pulse'
                return None
            try:
                # Keep the same scope and generation accounting; only tighten
                # its motor timeout to the separately accepted tool deadline.
                result = await self._dispatch_with_receipt(navigation.binding(step), frame=frame,
                    request_id=a.request_id, session_epoch=a.session_epoch,
                    candidate_set_version='coordinate-navigation-v2' if a.policy == 'segments' else 'coordinate-navigation-v1', chosen_option=step.action,
                    authorization_type='sage_coordinate_navigation', authorization_id=a.authorization_id,
                    source_frame_id=frame.frame_id, authority_check=navigation_fence,
                    authority_fence=navigation_fence)
            finally:
                scope.deadline_epoch = original_deadline
            if a.policy == 'segments':
                if navigation.segment:
                    navigation.segment.record(step, result.receipt or {}, navigation.last_position, frame.frame_id)
                navigation.step_permission = None
            navigation.record_dispatch(step, result.receipt or {}, time.time())
            return result

    def invalidate(self, reason: str) -> str:
        previous = self.session_epoch
        self.session_epoch = str(uuid4())
        self.store.append(Event.create("decision_context_invalidated", {
            "previous_session_epoch": previous,
            "session_epoch": self.session_epoch,
            "reason": reason,
        }))
        return self.session_epoch

    async def decide_and_execute(
        self,
        frame: Frame,
        image_path,
        prompt: str,
        instructions: str,
        candidates: list[ActionCandidate],
        *,
        reasoning: str = "off",
        max_frame_age_seconds: float | None = None,
        travel_budget: TravelDecisionBudget | None = None,
        propagate_capture_interruptions: bool = False,
        on_request_started: Callable[[str], None] | None = None,
    ) -> CycleResult:
        def expired(phase, required=0.0):
            if travel_budget is None or travel_budget.remaining() > required:
                return None
            evidence={'phase':phase,'source_at':travel_budget.source_at,
                'deadline':travel_budget.deadline,'remaining_seconds':travel_budget.remaining(),
                'age_seconds':time.time()-travel_budget.source_at,'required_seconds':required,
                'question_kind':travel_budget.question_kind}
            self.store.append(Event.create(travel_budget.expired_event,evidence))
            return CycleResult(travel_budget.expired_status,detail=f'Grinding source deadline expired or insufficient budget at {phase}')
        rejection=expired('before_request_setup')
        if rejection:return rejection
        if getattr(getattr(self, 'evidence_archive', None), 'failed', None):
            await self.executor.stop('evidence_retention_failed')
            return CycleResult('evidence_retention_failed')
        if self._inflight.locked():
            return CycleResult("decision_in_flight")
        async with self._inflight:
            try:
                candidate_set = build_candidate_set(candidates)
            except CandidateConfigurationError as exc:
                payload = exc.as_dict()
                self.store.append(Event.create("candidate_configuration_error", {
                    "frame_id": frame.frame_id, "session_epoch": self.session_epoch, **payload,
                }))
                return CycleResult("candidate_configuration_error", detail=str(exc))
            request_preconditions = {item.option: item.precondition for item in candidates}
            request_dispatch_guards = {item.option: item.dispatch_guard for item in candidates}
            epoch = self.session_epoch
            envelope = DecisionEnvelope.create(frame.frame_id, epoch, candidate_set)
            request_generation = self._input_generation
            age = _frame_age_seconds(frame)
            max_age = max_frame_age_seconds if max_frame_age_seconds is not None else self.tactical_max_age_seconds
            scope = self._execution_scope.get()
            if scope is not None:
                max_age = min(max_age, scope.max_frame_age_seconds)
            if age > max_age:
                self.store.append(Event.create("decision_skipped_stale_frame", {
                    "frame_id": frame.frame_id, "age_seconds": age, "max_age_seconds": max_age,
                }))
                return CycleResult("stale_frame", detail=f"frame age {age:.3f}s exceeds {max_age:.3f}s")
            scope_error = self._scope_error(frame)
            if scope_error:
                rejection=expired('before_sage_request')
                if rejection:return rejection
                return self._scope_rejected(frame, "before_sage_request", scope_error)
            archive = getattr(self, 'evidence_archive', None)
            if archive:
                try:
                    archive.frame(frame, epoch, request_generation)
                    image_path = archive.request_image(image_path, request_id=envelope.request_id,
                        frame_id=frame.frame_id, epoch=epoch)
                except (OSError, ValueError) as exc:
                    await self.executor.stop('evidence_retention_failed')
                    self.store.append(Event.create('evidence_retention_failed', {'error':str(exc)[:300],
                        'request_id':envelope.request_id,'frame_id':frame.frame_id}))
                    return CycleResult('evidence_retention_failed')
            rejection=expired('before_sage_request',travel_budget.validation_reserve if travel_budget else 0)
            if rejection:return rejection
            self.store.append(Event.create("sage_request_started", {
                "request_id": envelope.request_id, "frame_id": frame.frame_id,
                "session_epoch": epoch, "candidate_set_version": candidate_set.version,
                "candidate_options": list(envelope.candidate_options),
                "action_bindings": envelope.action_bindings,
                "context": prompt, "instructions": instructions, "reasoning_mode": reasoning,
                "decision_image_path": str(image_path),
            }))
            if on_request_started is not None:
                try:
                    started = on_request_started(envelope.request_id)
                    if inspect.isawaitable(started):
                        if inspect.iscoroutine(started):started.close()
                        raise TypeError('Request accounting callback must be synchronous')
                except Exception as exc:
                    self.store.append(Event.create('sage_request_failed', {
                        'request_id': envelope.request_id, 'frame_id': frame.frame_id,
                        'error_type': type(exc).__name__, 'error': 'Request accounting callback rejected'}))
                    self.store.append(Event.create('decision_start_rejected', {
                        'request_id': envelope.request_id, 'error_type': type(exc).__name__}))
                    return CycleResult('decision_start_rejected', detail='Request accounting rejected; no provider or input')
            try:
                remaining = None if scope is None else scope.deadline_epoch - time.time()
                provider_deadline = None
                sage_task = None
                if travel_budget is not None:
                    rejection=expired('before_sage_request',travel_budget.validation_reserve)
                    if rejection:return rejection
                    remaining=min(float(getattr(self.sage,'timeout_seconds',12)),
                        travel_budget.remaining()-travel_budget.validation_reserve)
                    provider_deadline=min(time.time()+remaining,travel_budget.deadline-travel_budget.validation_reserve)
                    self.store.append(Event.create('travel_provider_budget',{
                        'source_at':travel_budget.source_at,'deadline':travel_budget.deadline,
                        'provider_allowance_seconds':remaining,'validation_reserve_seconds':travel_budget.validation_reserve}))
                if remaining is not None and remaining <= 0:
                    return self._scope_rejected(frame, "before_sage_request", "task/session deadline expired")
                sage_call = self.sage.decide_image_choice(
                    image_path, prompt, instructions, deepcopy(candidate_set), deepcopy(envelope), reasoning,
                )
                if provider_deadline is not None:
                    remaining=provider_deadline-time.time()
                    if remaining<=0:
                        rejection=expired('before_provider_await',travel_budget.validation_reserve)
                        sage_call.close()
                        return rejection or CycleResult(travel_budget.expired_status,detail='Grinding provider allowance expired before await')
                if scope is None and travel_budget is None:
                    decision = await sage_call
                else:
                    if travel_budget is not None and travel_budget.question_kind!='travel':
                        sage_task=asyncio.ensure_future(sage_call)
                    decision = await asyncio.wait_for(sage_task if sage_task is not None else sage_call, timeout=remaining)
            except asyncio.TimeoutError:
                if sage_task is not None and sage_task.done() and not sage_task.cancelled() and isinstance(sage_task.exception(),TimeoutError):
                    self.store.append(Event.create('sage_request_failed',{'request_id':envelope.request_id,
                        'frame_id':frame.frame_id,'error_type':'TimeoutError','error':str(sage_task.exception())[:300]}))
                    return CycleResult('decision_failed',detail='TimeoutError')
                if travel_budget is not None:
                    self.store.append(Event.create(travel_budget.expired_event,{
                        'phase':'sage_request','source_at':travel_budget.source_at,'deadline':travel_budget.deadline,
                        'provider_allowance_seconds':remaining,'remaining_seconds':travel_budget.remaining(),
                        'age_seconds':time.time()-travel_budget.source_at,'question_kind':travel_budget.question_kind}))
                    return CycleResult(travel_budget.expired_status,detail='Grinding provider allowance expired; validation reserve retained')
                self.store.append(Event.create("sage_request_failed", {
                    "request_id": envelope.request_id, "frame_id": frame.frame_id,
                    "error_type": "ScopeDeadlineExpired", "error": "scoped Sage request reached its deadline",
                }))
                return CycleResult("scope_deadline_expired", detail="scoped Sage request reached its deadline")
            except Exception as exc:
                from sage_wow.agent.evidence import EvidenceRetentionError
                if isinstance(exc, EvidenceRetentionError):
                    await self.executor.stop('evidence_retention_failed')
                    self.store.append(Event.create('evidence_retention_failed', {
                        'request_id':envelope.request_id,'frame_id':frame.frame_id,'error':str(exc)[:300]}))
                    return CycleResult('evidence_retention_failed')
                self.store.append(Event.create("sage_request_failed", {
                    "request_id": envelope.request_id,
                    "frame_id": frame.frame_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }))
                return CycleResult("decision_failed", detail=type(exc).__name__)
            self.store.append(Event.create("sage_decision", decision.event_payload()))
            if provider_deadline is not None and time.time()>=provider_deadline:
                self.store.append(Event.create(travel_budget.expired_event,{
                    'phase':'late_sage_response','source_at':travel_budget.source_at,'deadline':travel_budget.deadline,
                    'provider_allowance_seconds':remaining,'remaining_seconds':travel_budget.remaining(),
                    'age_seconds':time.time()-travel_budget.source_at,'question_kind':travel_budget.question_kind}))
                return CycleResult(travel_budget.expired_status,detail='Grinding provider returned after its allowance; answer discarded')
            rejection=expired('after_sage_response')
            if rejection:return rejection
            if self.session_epoch != epoch:
                self.store.append(Event.create("sage_response_discarded", {
                    "request_id": envelope.request_id, "reason": "semantic_context_changed",
                    "old_epoch": epoch, "current_epoch": self.session_epoch,
                }))
                return CycleResult("invalidated", decision=decision)
            if _frame_age_seconds(frame) > max_age:
                self.store.append(Event.create("sage_response_discarded", {
                    "request_id": envelope.request_id, "reason": "frame_expired_during_request",
                    "frame_id": frame.frame_id,
                }))
                return CycleResult("stale_response", decision=decision)
            if self._input_generation != request_generation:
                self.store.append(Event.create("sage_response_discarded", {
                    "request_id": envelope.request_id, "reason": "input_generation_changed",
                    "generation_at_request": request_generation,
                    "current_input_generation": self._input_generation,
                }))
                return CycleResult("stale_input_generation", decision=decision,
                                   detail="physical input changed while Sage was deciding")
            scope_error = self._scope_error(frame)
            if scope_error:
                self.store.append(Event.create("sage_response_discarded", {
                    "request_id": envelope.request_id, "reason": scope_error,
                    "frame_id": frame.frame_id, "scope_id": scope.scope_id if scope else None,
                }))
                return self._scope_rejected(frame, "after_sage_response", scope_error)
            if decision.chosen is None:
                return CycleResult("needs_more_evidence", decision=decision)
            selected = next((item for item in candidates if item.option == decision.chosen), None)
            selected_snapshot = next((item for item in candidate_set.options if item.option == decision.chosen), None)
            if selected is None or selected_snapshot is None or selected_snapshot.binding != decision.selected_binding:
                return CycleResult("candidate_binding_mismatch", decision=decision)
            selected_binding_baseline = deepcopy(selected_snapshot.binding)
            try:
                current_binding = json.loads(json.dumps(selected.binding, sort_keys=True, allow_nan=False))
            except (TypeError, ValueError):
                current_binding = None
            if current_binding != selected_binding_baseline:
                self.store.append(Event.create("candidate_binding_mutated", {
                    "request_id": envelope.request_id, "chosen": selected.option,
                    "candidate_set_version": candidate_set.version,
                }))
                return CycleResult("candidate_binding_mismatch", decision=decision,
                                   detail="candidate binding changed after the Sage request was created")
            if not request_preconditions[selected.option]():
                rejection=expired('selected_precondition')
                if rejection:return rejection
                self.store.append(Event.create("action_rejected_precondition", {
                    "request_id": envelope.request_id, "frame_id": frame.frame_id,
                    "chosen": selected.option, "candidate_set_version": candidate_set.version,
                }))
                return CycleResult("precondition_failed", decision=decision)
            dispatch_frame = frame
            dispatch_guard = request_dispatch_guards.get(selected.option)
            if dispatch_guard is not None:
                guard_started = time.monotonic()
                validation: DispatchValidation | None = None
                guard_detail = ""
                guard_evidence: Any = {}
                guard_dispatch_frame: Frame | None = None
                guard_timeout = 3.0
                if scope is not None:
                    guard_timeout = min(guard_timeout, max(0.0, scope.deadline_epoch - time.time()))
                try:
                    if guard_timeout <= 0:
                        raise asyncio.TimeoutError("task/session deadline expired before continuity check")
                    guard_call = dispatch_guard(frame)
                    if not inspect.isawaitable(guard_call):
                        raise TypeError("dispatch_guard must return an awaitable DispatchValidation")
                    validation = await asyncio.wait_for(guard_call, timeout=guard_timeout)
                    if not isinstance(validation, DispatchValidation) or type(validation.approved) is not bool:
                        raise TypeError("dispatch_guard returned an invalid validation result")
                    guard_detail = str(validation.detail or "")[:500]
                    guard_evidence = validation.evidence
                    if not isinstance(guard_evidence, dict):
                        raise TypeError("dispatch_guard evidence must be an object")
                    if validation.approved:
                        guard_dispatch_frame = validation.dispatch_frame
                        frame_error = self._validate_dispatch_frame(frame, guard_dispatch_frame)
                        if frame_error:
                            if travel_budget is not None and travel_budget.question_kind!='travel' and frame_error.startswith('continuity frame is stale'):
                                self.store.append(Event.create(travel_budget.expired_event,{'phase':'dispatch_capture_age',
                                    'question_kind':travel_budget.question_kind,'source_at':travel_budget.source_at,
                                    'deadline':travel_budget.deadline,'remaining_seconds':travel_budget.remaining(),
                                    'age_seconds':time.time()-travel_budget.source_at,'validation_reason':frame_error}))
                                return CycleResult(travel_budget.expired_status,detail=frame_error)
                            guard_detail = frame_error
                            self._log_dispatch_guard(
                                request_id=envelope.request_id, frame=frame, authorization_epoch=epoch,
                                chosen_option=selected.option,
                                approved=False, detail=guard_detail, elapsed_ms=(time.monotonic()-guard_started)*1000,
                                evidence=guard_evidence,
                            )
                            return CycleResult("dispatch_guard_rejected", decision=decision, detail=guard_detail)
                        dispatch_frame = guard_dispatch_frame
                    else:
                        rejection=expired('continuity_check')
                        if rejection:return rejection
                        guard_detail = guard_detail or "continuity guard rejected dispatch"
                        self._log_dispatch_guard(
                            request_id=envelope.request_id, frame=frame, authorization_epoch=epoch,
                                chosen_option=selected.option,
                            approved=False, detail=guard_detail, elapsed_ms=(time.monotonic()-guard_started)*1000,
                            evidence=guard_evidence,
                        )
                        return CycleResult("dispatch_guard_rejected", decision=decision, detail=guard_detail)
                except asyncio.CancelledError:
                    self._log_dispatch_guard(
                        request_id=envelope.request_id, frame=frame, authorization_epoch=epoch,
                                chosen_option=selected.option,
                        approved=False, detail="continuity guard cancelled", elapsed_ms=(time.monotonic()-guard_started)*1000,
                        evidence=guard_evidence,
                    )
                    raise
                except asyncio.TimeoutError as exc:
                    if travel_budget is not None and travel_budget.question_kind!='travel':
                        self.store.append(Event.create(travel_budget.expired_event,{
                            'phase':'continuity_allowance','question_kind':travel_budget.question_kind,
                            'source_at':travel_budget.source_at,'deadline':travel_budget.deadline,
                            'remaining_seconds':travel_budget.remaining(),'age_seconds':time.time()-travel_budget.source_at}))
                        return CycleResult(travel_budget.expired_status,detail='Grinding continuity-check allowance expired')
                    rejection=expired('continuity_check')
                    if rejection:return rejection
                    guard_detail = ("task/session deadline expired during continuity check"
                                    if scope is not None and time.time() >= scope.deadline_epoch
                                    else (str(exc)[:500] or "continuity guard timed out"))
                    self._log_dispatch_guard(
                        request_id=envelope.request_id, frame=frame, authorization_epoch=epoch,
                                chosen_option=selected.option,
                        approved=False, detail=guard_detail,
                        elapsed_ms=(time.monotonic()-guard_started)*1000,
                        evidence=guard_evidence,
                    )
                    return CycleResult("dispatch_guard_rejected", decision=decision, detail=guard_detail)
                except Exception as exc:
                    if propagate_capture_interruptions:
                        from sage_wow.platform.macos.capture import CaptureError, ForegroundCaptureInterrupted
                        self._log_dispatch_guard(
                            request_id=envelope.request_id,frame=frame,authorization_epoch=epoch,
                            chosen_option=selected.option,approved=False,
                            detail=('Foreground capture interrupted' if isinstance(exc,ForegroundCaptureInterrupted)
                                else f'Grinding continuity source/capture failure ({type(exc).__name__}): {exc}')[:500],
                            elapsed_ms=(time.monotonic()-guard_started)*1000,
                            evidence={'failure_type':type(exc).__name__,
                                'source_at':travel_budget.source_at if travel_budget else None,
                                'deadline':travel_budget.deadline if travel_budget else None})
                        if isinstance(exc,CaptureError):raise
                        # This opt-in belongs to the standalone grinder: generic
                        # capture or immutable-source analysis failures are hard.
                        raise CaptureError(f'Grinding continuity failed ({type(exc).__name__}): {exc}') from exc
                    guard_detail = f"continuity guard failed ({type(exc).__name__})"
                    self._log_dispatch_guard(
                        request_id=envelope.request_id, frame=frame, authorization_epoch=epoch,
                                chosen_option=selected.option,
                        approved=False, detail=guard_detail,
                        elapsed_ms=(time.monotonic()-guard_started)*1000,
                        evidence=guard_evidence,
                    )
                    return CycleResult("dispatch_guard_rejected", decision=decision, detail=guard_detail)

                # The guard only supplies a fresh frame for continuity. It may
                # never revise Sage's choice or binding, and every decision and
                # scope invariant is checked again after that asynchronous gap.
                rejection=expired('after_continuity_check')
                if rejection:return rejection
                post_guard_reason = self._validate_dispatch_frame(frame, guard_dispatch_frame)
                if not post_guard_reason:
                    try:
                        binding_after_guard = json.loads(json.dumps(selected.binding, sort_keys=True, allow_nan=False))
                        selected_wire = json.loads(json.dumps(envelope.action_bindings.get(selected.option), sort_keys=True, allow_nan=False))
                        decision_binding = json.loads(json.dumps(decision.selected_binding, sort_keys=True, allow_nan=False))
                    except (TypeError, ValueError):
                        binding_after_guard = selected_wire = decision_binding = None
                    if (decision.chosen != selected.option or binding_after_guard != selected_binding_baseline
                            or selected_wire != selected_binding_baseline
                            or decision_binding != selected_binding_baseline):
                        post_guard_reason = "Sage selection or its candidate binding changed during continuity validation"
                if not post_guard_reason and _frame_age_seconds(frame) > max_age:
                    post_guard_reason = "the original Sage source frame expired during continuity validation"
                if not post_guard_reason and self.session_epoch != epoch:
                    post_guard_reason = "session epoch changed during continuity validation"
                if not post_guard_reason and self._input_generation != request_generation:
                    post_guard_reason = "input generation changed during continuity validation"
                if not post_guard_reason:
                    post_guard_reason = self._scope_error(frame) or ""
                if not post_guard_reason:
                    try:
                        if not request_preconditions[selected.option]():
                            post_guard_reason = "candidate precondition changed during continuity validation"
                    except Exception as exc:
                        post_guard_reason = f"candidate precondition failed ({type(exc).__name__})"
                # A precondition callback may synchronously invalidate state.
                # Recheck all continuity and task-scope invariants after it.
                if not post_guard_reason and _frame_age_seconds(frame) > max_age:
                    post_guard_reason = "the original Sage source frame expired during dispatch validation"
                if not post_guard_reason and self.session_epoch != epoch:
                    post_guard_reason = "session epoch changed during dispatch validation"
                if not post_guard_reason and self._input_generation != request_generation:
                    post_guard_reason = "input generation changed during dispatch validation"
                if not post_guard_reason:
                    post_guard_reason = self._scope_error(frame) or ""
                if not post_guard_reason:
                    dispatch_error = self._validate_dispatch_frame(frame, dispatch_frame)
                    if dispatch_error:
                        post_guard_reason = dispatch_error
                if not post_guard_reason:
                    try:
                        binding_after_precondition = json.loads(json.dumps(selected.binding, sort_keys=True, allow_nan=False))
                    except (TypeError, ValueError):
                        binding_after_precondition = None
                    if binding_after_precondition != selected_binding_baseline:
                        post_guard_reason = "candidate binding changed while rechecking dispatch preconditions"
                if post_guard_reason:
                    if travel_budget is not None and travel_budget.question_kind!='travel' and post_guard_reason.startswith('continuity frame is stale'):
                        self.store.append(Event.create(travel_budget.expired_event,{'phase':'dispatch_capture_age',
                            'question_kind':travel_budget.question_kind,'source_at':travel_budget.source_at,
                            'deadline':travel_budget.deadline,'remaining_seconds':travel_budget.remaining(),
                            'age_seconds':time.time()-travel_budget.source_at,'validation_reason':post_guard_reason}))
                        return CycleResult(travel_budget.expired_status,decision=decision,detail=post_guard_reason)
                    self._log_dispatch_guard(
                        request_id=envelope.request_id, frame=frame, authorization_epoch=epoch,
                                chosen_option=selected.option,
                        approved=False, detail=post_guard_reason,
                        elapsed_ms=(time.monotonic()-guard_started)*1000,
                        evidence=guard_evidence, dispatch_frame=guard_dispatch_frame,
                    )
                    return CycleResult("dispatch_guard_rejected", decision=decision, detail=post_guard_reason)
                self._log_dispatch_guard(
                    request_id=envelope.request_id, frame=frame, authorization_epoch=epoch,
                                chosen_option=selected.option,
                    approved=True, detail=guard_detail or "continuity verified",
                    elapsed_ms=(time.monotonic()-guard_started)*1000,
                    evidence=guard_evidence, dispatch_frame=dispatch_frame,
                )
            scope_error = self._scope_error(frame)
            if scope_error:
                rejection=expired('immediately_before_dispatch')
                if rejection:return rejection
                return self._scope_rejected(frame, "immediately_before_dispatch", scope_error)
            rejection=expired('immediately_before_dispatch',grinding_input_seconds(selected_binding_baseline) if travel_budget else 0.)
            if rejection:return rejection
            original_snapshot, dispatch_snapshot = deepcopy(frame), deepcopy(dispatch_frame)
            envelope_snapshot = deepcopy(envelope)
            authorized_option = selected.option
            budget_snapshot = deepcopy(travel_budget)
            def decision_fence():
                if frame != original_snapshot or dispatch_frame != dispatch_snapshot:
                    return 'original or dispatch frame changed before first input'
                if _frame_age_seconds(frame) > max_age:
                    return 'original Sage source frame expired before first input'
                if dispatch_guard is not None:
                    error = self._validate_dispatch_frame(frame, dispatch_frame)
                    if error:
                        return error
                if self.session_epoch != epoch or self._input_generation != request_generation:
                    return 'decision session or generation changed before first input'
                if envelope != envelope_snapshot or decision.envelope != envelope_snapshot:
                    return 'decision request envelope changed before first input'
                try:
                    bindings = [json.loads(json.dumps(value, sort_keys=True, allow_nan=False)) for value in
                                (selected.binding, envelope.action_bindings.get(authorized_option), decision.selected_binding)]
                except (TypeError, ValueError):
                    return 'decision binding is no longer JSON-safe'
                if (selected.option != authorized_option or decision.chosen != authorized_option
                        or any(value != selected_binding_baseline for value in bindings)):
                    return 'Sage choice or candidate binding changed before first input'
                if travel_budget != budget_snapshot:
                    return 'original decision budget changed before first input'
                if travel_budget is not None and travel_budget.remaining() <= grinding_input_seconds(selected_binding_baseline):
                    return 'original decision deadline has insufficient time for input'
                return None
            def decision_check():
                reason = decision_fence()
                if reason:
                    return reason
                if not request_preconditions[authorized_option]():
                    return 'candidate precondition changed before first input'
                return decision_fence()
            dispatched = await self._dispatch_with_receipt(
                selected_binding_baseline, frame=dispatch_frame, request_id=envelope.request_id,
                session_epoch=epoch, candidate_set_version=candidate_set.version,
                chosen_option=selected.option,
                source_frame_id=frame.frame_id,
                authority_check=decision_check, authority_fence=decision_fence,
                source_max_age_seconds=max_age,
            )
            return CycleResult(dispatched.status, decision, dispatched.execution,
                               dispatched.detail, dispatched.receipt)


def _frame_age_seconds(frame: Frame) -> float:
    captured = datetime.fromisoformat(frame.captured_at)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    return max(0.0, time.time() - captured.timestamp())
