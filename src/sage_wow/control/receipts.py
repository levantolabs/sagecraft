"""Canonical low-level execution receipt schema."""
from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4
from sage_wow.models import utc_now


def execution_receipt(
    *, request_id: str, source_frame_id: str, session_epoch: str,
    dispatch_frame_id: str | None = None,
    candidate_set_version: str, chosen_option: str, selected_binding: dict[str, Any],
    generation_before: int, generation_after: int, attempted: bool,
    input_steps: list[dict[str, Any]], execution: dict[str, Any] | None,
    error: str | None = None, receipt_id: str | None = None,
    authorization_type: str = "sage_decision", authorization_id: str | None = None,
) -> dict[str, Any]:
    """Create a JSON-safe receipt without inferring in-game effects."""
    steps = deepcopy(input_steps)
    has_possible_input = bool(steps)
    completed = bool(execution is not None and execution.get("completed", True) and error is None)
    confirmed_steps = any(step.get("status") == "completed" for step in steps)
    uncertain_steps = any(step.get("status") in {"effect_unknown", "failed_effect_unknown"} for step in steps)
    dispatched = confirmed_steps or bool(execution and execution.get("dispatched") and not steps)
    observe_only = bool(execution is not None and execution.get("kind") == "observe_only")
    if has_possible_input:
        effect_status = "unknown"
    elif observe_only:
        effect_status = "not_applicable"
    else:
        effect_status = "not_observed"
    return {
        "receipt_id": receipt_id or str(uuid4()),
        "occurred_at": utc_now(),
        "request_id": request_id,
        "source_frame_id": source_frame_id,
        "dispatch_frame_id": dispatch_frame_id or source_frame_id,
        "session_epoch": session_epoch,
        "candidate_set_version": candidate_set_version,
        "chosen_option": chosen_option,
        "authorization_type": authorization_type,
        "authorization_id": authorization_id,
        "selected_binding": deepcopy(selected_binding),
        "generation_before": generation_before,
        "generation_after": generation_after,
        "attempted": (has_possible_input or bool(execution and execution.get("dispatched") and not observe_only))
        and attempted,
        "dispatched": dispatched,
        "possible_input": has_possible_input,
        "dispatch_unknown": uncertain_steps,
        "completed": completed,
        "effect_status": effect_status,
        "input_steps": steps,
        "execution": deepcopy(execution),
        "error": error,
    }
