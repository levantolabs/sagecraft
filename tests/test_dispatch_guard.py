import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from sage_wow.agent.cycle import ActionCandidate, DecisionCycle, DispatchValidation
from sage_wow.models import Frame
from sage_wow.sage.client import SageDecision
from sage_wow.storage import EventStore


class FakeSage:
    async def decide_image_choice(self, path, text, instructions, candidates, envelope, reasoning):
        option = candidates.options[0]
        return SageDecision(envelope, option.option, option.binding, .9, (), "test", 1,
                            {"ran": False}, {}, 1, tuple(x.option for x in candidates.options))


class FakeExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, binding, *, frame_size=None):
        self.calls.append((binding, frame_size))
        return {"dispatched": False, "kind": "wait"}


def make_frame(**kwargs):
    return Frame.create("selected-window", 800, 600, image_data=b"image", **kwargs)


def run_cycle(tmp_path, guard, *, precondition=lambda: True, binding=None, scope=None, holder=None):
    store = EventStore(tmp_path / "events.sqlite3")
    executor = FakeExecutor()
    cycle = DecisionCycle(FakeSage(), executor, store, session_epoch="epoch-a")
    if holder is not None:
        holder["cycle"] = cycle
    original = make_frame()
    candidate = ActionCandidate("wait", "Wait briefly", binding or {"type": "wait", "seconds": .01},
                                precondition, guard)
    alternate = ActionCandidate("inspect", "Observe", {"type": "observe_only"})

    async def run():
        if scope is None:
            return await cycle.decide_and_execute(original, "source.png", "context", "choose", [candidate, alternate]), cycle
        with cycle.scoped_execution_scope(**scope(cycle)):
            result = await cycle.decide_and_execute(original, "source.png", "context", "choose", [candidate, alternate])
            return result, cycle

    result, cycle = asyncio.run(run())
    events = store.recent()
    store.close()
    return result, cycle, executor, events, original


def test_dispatch_guard_uses_new_frame_but_receipt_keeps_sage_source(tmp_path):
    fresh = []

    async def guard(source):
        fresh.append(make_frame())
        assert source.frame_id != fresh[0].frame_id
        return DispatchValidation(True, fresh[0], "same scene", {"stable": True})

    result, _, executor, events, source = run_cycle(tmp_path, guard)
    assert result.status == "dispatched", result.detail
    assert executor.calls[0][1] == (fresh[0].width, fresh[0].height)
    assert result.receipt["source_frame_id"] == source.frame_id
    assert result.receipt["dispatch_frame_id"] == fresh[0].frame_id
    event = next(e for e in events if e["event_type"] == "dispatch_guard_checked")
    assert event["payload"]["approved"] is True
    assert event["payload"]["dispatch_frame_id"] == fresh[0].frame_id
    assert event["payload"]["session_epoch"] == "epoch-a"


@pytest.mark.parametrize("frame_factory, expected", [
    (lambda src: src, "source frame again"),
    (lambda src: Frame.create("other-window", 800, 600, image_data=b"new"), "source or geometry changed"),
    (lambda src: Frame("future", (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
                       src.source, src.width, src.height, image_data=b"new"), "in the future"),
])
def test_dispatch_guard_rejects_invalid_continuity_frame_without_input(tmp_path, frame_factory, expected):
    async def guard(source):
        return DispatchValidation(True, frame_factory(source))

    result, _, executor, _, _ = run_cycle(tmp_path, guard)
    assert result.status == "dispatch_guard_rejected"
    assert expected in result.detail
    assert executor.calls == []
    assert result.receipt is None


@pytest.mark.parametrize("guard, expected", [
    (lambda _: _raises("ValueError"), "continuity guard failed (ValueError)"),
    (lambda _: _slow_guard(), "timed out"),
])
def test_dispatch_guard_failure_or_timeout_is_logged_and_no_input(tmp_path, guard, expected):
    async def wrapped(source):
        value = guard(source)
        if isinstance(value, str):
            raise ValueError(value)
        return await value

    result, _, executor, events, _ = run_cycle(tmp_path, wrapped)
    assert result.status == "dispatch_guard_rejected"
    assert expected in result.detail
    assert executor.calls == []
    logged = next(e["payload"] for e in events if e["event_type"] == "dispatch_guard_checked")
    assert expected.split("(")[0].split()[0].lower() in logged["detail"].lower()


def _raises(message):
    return message


async def _slow_guard():
    await asyncio.sleep(4)
    return DispatchValidation(False)


@pytest.mark.parametrize("change, expected", [
    ("epoch", "session epoch changed"),
    ("generation", "input generation changed"),
    ("scope", "no longer current"),
])
def test_dispatch_guard_rechecks_session_generation_and_scope(tmp_path, change, expected):
    state = {"current": True}
    holder = {}

    async def guard(source):
        cycle = holder["cycle"]
        if change == "epoch":
            cycle.invalidate("test guard invalidation")
        elif change == "generation":
            cycle._input_generation += 1
        else:
            state["current"] = False
        return DispatchValidation(True, make_frame())

    scope_factory = (lambda cycle: {
        "task_id": "task", "objective_revision": 1, "session_epoch": "epoch-a",
        "deadline_epoch": datetime.now(timezone.utc).timestamp() + 10,
        "input_generation": cycle.input_generation, "max_frame_age_seconds": 10,
        "is_current": lambda: state["current"],
    }) if change == "scope" else None
    result, cycle, executor, events, _ = run_cycle(tmp_path, guard, scope=scope_factory, holder=holder)
    assert result.status == "dispatch_guard_rejected"
    assert expected in result.detail
    assert executor.calls == []
    logged = next(e["payload"] for e in events if e["event_type"] == "dispatch_guard_checked")
    assert logged["session_epoch"] == "epoch-a"
    if change == "epoch":
        assert logged["current_session_epoch"] != "epoch-a"


def test_dispatch_guard_rejects_binding_mutation_and_precondition_invalidation(tmp_path):
    holder = {}
    precondition_calls = {"count": 0}
    binding = {"type": "wait", "seconds": .01}

    async def mutate_guard(source):
        binding["seconds"] = .2
        return DispatchValidation(True, make_frame())

    mutated, _, executor, _, _ = run_cycle(tmp_path / "mutated", mutate_guard, binding=binding)
    assert mutated.status == "dispatch_guard_rejected"
    assert "binding changed" in mutated.detail
    assert executor.calls == []

    def invalidate_precondition():
        precondition_calls["count"] += 1
        if precondition_calls["count"] == 2:
            holder["cycle"].invalidate("precondition changed epoch")
        return True

    async def fresh_guard(_):
        return DispatchValidation(True, make_frame())

    result, _, executor, _, _ = run_cycle(tmp_path / "precondition", fresh_guard,
                                           precondition=invalidate_precondition, holder=holder)
    assert result.status == "dispatch_guard_rejected"
    assert "session epoch changed during dispatch validation" in result.detail
    assert executor.calls == []


def test_dispatch_guard_deadline_expiring_during_guard_is_rejected(tmp_path):
    async def guard(_):
        await asyncio.sleep(.1)
        return DispatchValidation(True, make_frame())

    def scope_factory(cycle):
        return {
            "task_id": "task", "objective_revision": 1, "session_epoch": "epoch-a",
            "deadline_epoch": datetime.now(timezone.utc).timestamp() + .03,
            "input_generation": cycle.input_generation, "max_frame_age_seconds": 10,
            "is_current": lambda: True,
        }

    result, _, executor, events, _ = run_cycle(tmp_path, guard, scope=scope_factory)
    assert result.status == "dispatch_guard_rejected"
    assert "deadline expired" in result.detail
    assert executor.calls == []
    logged = next(e["payload"] for e in events if e["event_type"] == "dispatch_guard_checked")
    assert logged["session_epoch"] == "epoch-a"
    assert "deadline expired" in logged["detail"]
