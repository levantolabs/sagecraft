import asyncio
from uuid import uuid4
import pytest

from sage_wow.agent.cycle import ActionCandidate, DecisionCycle, build_candidate_set
from sage_wow.control.executor import GateSnapshot, SafeExecutor
from sage_wow.models import Frame
from sage_wow.platform.macos.geometry import Rect
from sage_wow.sage.client import SageDecision
from sage_wow.storage import EventStore


class FakeSage:
    def __init__(self, chosen="wait", gate=None):
        self.chosen = chosen
        self.gate = gate
        self.calls = 0

    async def decide_image_choice(self, path, text, instructions, candidates, envelope, reasoning):
        self.calls += 1
        if self.gate:
            await self.gate.wait()
        by_id = {item.option: item for item in candidates.options}
        option = by_id.get(self.chosen)
        return SageDecision(envelope, self.chosen, option.binding if option else None, 0.8, (), "mock", 50,
                            {"ran": False}, {}, 60, tuple(by_id))


class FakeExecutor:
    def __init__(self, *, fail=False):
        self.calls = []
        self.physical_step_callback = None
        self.fail = fail

    def set_physical_step_callback(self, callback):
        self.physical_step_callback = callback

    async def execute(self, binding, *, frame_size=None, execution_id=None):
        self.calls.append((binding, frame_size))
        if binding.get("type") != "observe_only" and self.physical_step_callback:
            step = {
                "step_id": str(uuid4()), "execution_id": execution_id, "kind": "key_down",
                "details": {"keycode": binding.get("keycode")}, "attempted": True,
                "status": "effect_unknown",
            }
            self.physical_step_callback(step)
            if not self.fail:
                step["status"] = "completed"
        if self.fail:
            raise RuntimeError("injected partial failure")
        return {"dispatched": True, "kind": binding["type"]}


def candidate_set():
    return [
        ActionCandidate("wait", "Wait for the current transition.", {"type": "wait", "seconds": 0.05}),
        ActionCandidate("inspect_quests", "Open the quest log.", {"type": "keypress", "keycode": 12, "hold_seconds": 0.08}),
    ]


def frame():
    return Frame.create("fixture", 800, 600)


def test_candidate_version_includes_binding_and_dispatches_local_binding(tmp_path):
    candidates = candidate_set()
    changed = [candidates[0], ActionCandidate("inspect_quests", "Open the quest log.", {"type": "keypress", "keycode": 13})]
    assert build_candidate_set(candidates).version != build_candidate_set(changed).version
    store = EventStore(tmp_path / "events.sqlite3")
    sage, executor = FakeSage(), FakeExecutor()
    cycle = DecisionCycle(sage, executor, store)
    result = asyncio.run(cycle.decide_and_execute(frame(), "frame.jpg", "context", "choose", candidates))
    assert result.status == "dispatched"
    assert executor.calls == [({"type": "wait", "seconds": 0.05}, (800, 600))]
    assert result.receipt["receipt_id"]
    assert result.receipt["generation_after"] == result.receipt["generation_before"] + 1
    assert result.receipt["effect_status"] == "unknown"
    assert result.receipt["input_steps"][0]["status"] == "completed"
    assert store.recent()[-1]["event_type"] == "action_dispatched"
    assert any(row["event_type"] == "execution_receipt" for row in store.recent())
    store.close()


def test_candidate_configuration_error_is_typed_and_logged_without_model_call(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    sage, executor = FakeSage(), FakeExecutor()
    cycle = DecisionCycle(sage, executor, store)
    items = candidate_set() + [ActionCandidate("wait", "duplicate", {"type": "observe_only"})]
    result = asyncio.run(cycle.decide_and_execute(frame(), "frame.jpg", "context", "choose", items))
    assert result.status == "candidate_configuration_error"
    assert sage.calls == 0 and executor.calls == []
    event = next(row for row in store.recent() if row["event_type"] == "candidate_configuration_error")
    assert event["payload"]["code"] == "duplicate_option_ids"
    store.close()


def test_nested_binding_mutation_during_request_is_rejected(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    binding = {"type": "keypress", "keycode": 12, "hold_seconds": .08,
               "metadata": {"keys": [1, 2]}}
    candidates = [ActionCandidate("choose", "A choice", binding),
                  ActionCandidate("wait", "Wait", {"type": "observe_only"})]

    class MutatingSage(FakeSage):
        async def decide_image_choice(self, path, text, instructions, candidate_set, envelope, reasoning):
            self.calls += 1
            binding["metadata"]["keys"][0] = 99
            choice = next(option for option in candidate_set.options if option.option == "choose")
            return SageDecision(envelope, "choose", choice.binding, .9, (), "mock", 1,
                                {"ran": False}, {}, 1, tuple(o.option for o in candidate_set.options))

    executor = FakeExecutor()
    result = asyncio.run(DecisionCycle(MutatingSage(), executor, store).decide_and_execute(
        frame(), "frame.jpg", "context", "choose", candidates))
    assert result.status == "candidate_binding_mismatch"
    assert executor.calls == []
    store.close()


def test_execution_failure_returns_durable_partial_receipt(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    executor = FakeExecutor(fail=True)
    cycle = DecisionCycle(FakeSage("inspect_quests"), executor, store)
    result = asyncio.run(cycle.decide_and_execute(frame(), "frame.jpg", "context", "choose", candidate_set()))
    assert result.status == "execution_failed"
    assert result.receipt["attempted"] and result.receipt["possible_input"]
    assert result.receipt["dispatch_unknown"] and not result.receipt["dispatched"]
    assert result.receipt["completed"] is False
    assert result.receipt["generation_after"] == 1
    assert result.receipt["input_steps"][0]["input_generation"] == 1
    assert any(row["event_type"] == "execution_receipt" for row in store.recent())
    store.close()


def test_observe_only_does_not_advance_input_generation(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    executor = FakeExecutor()
    cycle = DecisionCycle(FakeSage("inspect_quests"), executor, store)
    candidates = [ActionCandidate("inspect_quests", "Observe", {"type": "observe_only"}),
                  ActionCandidate("wait", "Wait", {"type": "observe_only"})]
    result = asyncio.run(cycle.decide_and_execute(frame(), "frame.jpg", "context", "choose", candidates))
    assert result.status == "dispatched"
    assert cycle.input_generation == 0
    assert result.receipt["generation_before"] == result.receipt["generation_after"] == 0
    assert result.receipt["effect_status"] == "not_applicable"
    store.close()


def test_prior_sage_continuation_uses_receipt_without_fresh_model_call(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    sage, executor = FakeSage(), FakeExecutor()
    cycle = DecisionCycle(sage, executor, store)
    result = asyncio.run(cycle.execute_authorized(
        {"type": "keypress", "keycode": 12, "hold_seconds": .08}, frame=frame(),
        chosen_option="close_owned_panel", authorization_id="branch-7/step-3",
        request_id="sage-request-7", candidate_set_version="set-7", source_frame_id="source-frame-7",
    ))
    assert result.status == "dispatched"
    assert sage.calls == 0
    assert result.receipt["authorization_type"] == "prior_sage_continuation"
    assert result.receipt["authorization_id"] == "branch-7/step-3"
    assert result.receipt["source_frame_id"] == "source-frame-7"
    assert result.receipt["dispatch_frame_id"] != "source-frame-7"
    store.close()


def test_cancelled_execution_persists_receipt_before_propagating_cancellation(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")

    class CancelledExecutor(FakeExecutor):
        async def execute(self, binding, *, frame_size=None, execution_id=None):
            step = {"step_id": str(uuid4()), "execution_id": execution_id, "kind": "text",
                    "details": {"length": 12}, "attempted": True, "status": "effect_unknown"}
            self.physical_step_callback(step)
            raise asyncio.CancelledError()

    executor = CancelledExecutor()
    cycle = DecisionCycle(FakeSage("inspect_quests"), executor, store)
    async def cancelled_in_scope():
        import time
        with cycle.scoped_execution_scope(
            task_id="cancelled-child", objective_revision=1, session_epoch=cycle.session_epoch,
            deadline_epoch=time.time() + 30, input_generation=0, is_current=lambda: True,
        ):
            await cycle.decide_and_execute(frame(), "frame.jpg", "context", "choose", candidate_set())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled_in_scope())
    receipts = [row["payload"] for row in store.recent() if row["event_type"] == "execution_receipt"]
    assert len(receipts) == 1
    assert receipts[0]["error"] == "CancelledError"
    assert receipts[0]["dispatch_unknown"] is True
    assert receipts[0]["generation_after"] == 1
    assert receipts[0]["scope_task_id"] == "cancelled-child"
    assert receipts[0]["receipt_sequence"] == 1
    assert cycle.receipt_sequence == 1
    assert cycle.last_receipt["receipt_id"] == receipts[0]["receipt_id"]
    store.close()


def test_scoped_cycle_fails_closed_when_task_scope_is_invalid_before_request(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    sage, executor = FakeSage("inspect_quests"), FakeExecutor()
    cycle = DecisionCycle(sage, executor, store, session_epoch="scope-epoch")
    with cycle.scoped_execution_scope(
        task_id="child-1", objective_revision=3, session_epoch="scope-epoch",
        deadline_epoch=__import__("time").time() + 60, input_generation=0,
        is_current=lambda: False,
    ):
        result = asyncio.run(cycle.decide_and_execute(
            frame(), "frame.jpg", "context", "choose", candidate_set()))
    assert result.status == "scope_rejected"
    assert sage.calls == 0 and executor.calls == []
    event = next(row for row in store.recent() if row["event_type"] == "scoped_decision_rejected")
    assert event["payload"]["task_id"] == "child-1"
    store.close()


def test_scope_change_while_sage_waits_rejects_before_dispatch(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    valid = [True]

    class InvalidatingSage(FakeSage):
        async def decide_image_choice(self, path, text, instructions, candidates, envelope, reasoning):
            decision = await super().decide_image_choice(path, text, instructions, candidates, envelope, reasoning)
            valid[0] = False
            return decision

    sage, executor = InvalidatingSage("inspect_quests"), FakeExecutor()
    cycle = DecisionCycle(sage, executor, store, session_epoch="scope-epoch")
    with cycle.scoped_execution_scope(
        task_id="child-2", objective_revision=1, session_epoch="scope-epoch",
        deadline_epoch=__import__("time").time() + 60, input_generation=0,
        is_current=lambda: valid[0],
    ):
        result = asyncio.run(cycle.decide_and_execute(
            frame(), "frame.jpg", "context", "choose", candidate_set()))
    assert result.status == "scope_rejected"
    assert executor.calls == []
    assert any(row["event_type"] == "sage_response_discarded" for row in store.recent())
    store.close()


def test_scope_deadline_bounds_sage_await_without_creating_an_input_receipt(tmp_path):
    import time

    class SlowSage(FakeSage):
        async def decide_image_choice(self, *args, **kwargs):
            self.calls += 1
            await asyncio.sleep(.2)
            return None

    store = EventStore(tmp_path / "events.sqlite3")
    sage, executor = SlowSage("inspect_quests"), FakeExecutor()
    cycle = DecisionCycle(sage, executor, store, session_epoch="scope-epoch")

    async def scenario():
        with cycle.scoped_execution_scope(
            task_id="child-slow", objective_revision=1, session_epoch="scope-epoch",
            deadline_epoch=time.time() + .02, input_generation=0, is_current=lambda: True,
        ):
            return await cycle.decide_and_execute(frame(), "frame.jpg", "ctx", "choose", candidate_set())

    result = asyncio.run(scenario())
    assert result.status == "scope_deadline_expired"
    assert sage.calls == 1 and executor.calls == []
    assert cycle.input_generation == 0 and cycle.receipt_sequence == 0
    assert cycle.last_receipt is None
    store.close()


def test_expired_between_dispatch_guard_and_executor_creation_calls_no_executor(tmp_path, monkeypatch):
    import sage_wow.agent.cycle as cycle_module

    store = EventStore(tmp_path / "events.sqlite3")
    executor = FakeExecutor()
    cycle = DecisionCycle(FakeSage(), executor, store, session_epoch="scope-epoch")
    deadline = 1000.0
    clock = iter((deadline - .1, deadline + .1))
    monkeypatch.setattr(cycle_module.time, "time", lambda: next(clock, deadline + 1))
    with cycle.scoped_execution_scope(
        task_id="expires-before-executor", objective_revision=1, session_epoch="scope-epoch",
        deadline_epoch=deadline, input_generation=0, is_current=lambda: True,
    ):
        result = asyncio.run(cycle.execute_authorized(
            {"type": "keypress", "keycode": 12, "hold_seconds": .08}, frame=frame(),
            chosen_option="close_owned_panel", authorization_id="branch-1/step-1",
            request_id="prior-sage-request", candidate_set_version="set-1",
        ))
    assert result.status == "scope_rejected"
    assert executor.calls == []
    assert cycle.receipt_sequence == 0 and cycle.last_receipt is None
    store.close()


def test_expired_between_sage_guard_and_call_does_not_start_model_request(tmp_path, monkeypatch):
    import sage_wow.agent.cycle as cycle_module

    store = EventStore(tmp_path / "events.sqlite3")
    sage, executor = FakeSage(), FakeExecutor()
    cycle = DecisionCycle(sage, executor, store, session_epoch="scope-epoch")
    deadline = 1000.0
    clock = iter((deadline - .1, deadline - .05, deadline + .1))
    monkeypatch.setattr(cycle_module.time, "time", lambda: next(clock, deadline + 1))
    with cycle.scoped_execution_scope(
        task_id="expires-before-sage", objective_revision=1, session_epoch="scope-epoch",
        deadline_epoch=deadline, input_generation=0, is_current=lambda: True,
    ):
        result = asyncio.run(cycle.decide_and_execute(
            frame(), "frame.jpg", "ctx", "choose", candidate_set()))
    assert result.status == "scope_rejected"
    assert sage.calls == 0 and executor.calls == []
    assert cycle.receipt_sequence == 0 and cycle.last_receipt is None
    store.close()


def test_old_receipt_sequence_does_not_advance_on_timed_out_next_request(tmp_path):
    import time

    class SlowAfterFirst(FakeSage):
        async def decide_image_choice(self, *args, **kwargs):
            self.calls += 1
            await asyncio.Event().wait()

    store = EventStore(tmp_path / "events.sqlite3")
    sage, executor = FakeSage("inspect_quests"), FakeExecutor()
    cycle = DecisionCycle(sage, executor, store, session_epoch="scope-epoch")
    first = asyncio.run(cycle.decide_and_execute(
        frame(), "frame.jpg", "ctx", "choose", candidate_set()))
    before_sequence = cycle.receipt_sequence
    before_id = cycle.last_receipt["receipt_id"]
    slow_sage = SlowAfterFirst()
    cycle.sage = slow_sage

    async def next_child():
        with cycle.scoped_execution_scope(
            task_id="next-child", objective_revision=1, session_epoch="scope-epoch",
            deadline_epoch=time.time() + .02, input_generation=cycle.input_generation,
            is_current=lambda: True,
        ):
            return await cycle.decide_and_execute(frame(), "frame.jpg", "ctx", "choose", candidate_set())

    timed_out = asyncio.run(next_child())
    assert first.receipt and timed_out.status == "scope_deadline_expired"
    assert cycle.receipt_sequence == before_sequence
    assert cycle.last_receipt["receipt_id"] == before_id
    assert timed_out.receipt is None
    assert len(executor.calls) == 1
    store.close()


def test_scoped_input_generation_snapshot_rejects_unrelated_input_before_sage(tmp_path):
    import time

    store = EventStore(tmp_path / "events.sqlite3")
    sage, executor = FakeSage("inspect_quests"), FakeExecutor()
    cycle = DecisionCycle(sage, executor, store, session_epoch="scope-epoch")
    cycle._on_physical_step({"step_id": "external", "execution_id": None, "kind": "key_down",
                             "details": {}, "attempted": True, "status": "effect_unknown"})
    with cycle.scoped_execution_scope(
        task_id="child-stale-input", objective_revision=1, session_epoch="scope-epoch",
        deadline_epoch=time.time() + 60, input_generation=0, is_current=lambda: True,
    ):
        result = asyncio.run(cycle.decide_and_execute(frame(), "frame.jpg", "ctx", "choose", candidate_set()))
    assert result.status == "scope_rejected"
    assert sage.calls == 0 and executor.calls == []
    store.close()


def test_scoped_receipts_have_task_binding_monotonic_sequence_and_safe_generation_updates(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    sage, executor = FakeSage("inspect_quests"), FakeExecutor()
    cycle = DecisionCycle(sage, executor, store, session_epoch="scope-epoch")
    dispatched = [ActionCandidate("inspect_quests", "Press one verified key.",
                                  {"type": "keypress", "keycode": 12, "hold_seconds": .08}),
                  ActionCandidate("wait", "Wait.", {"type": "observe_only"})]
    with cycle.scoped_execution_scope(
        task_id="child-3", objective_revision=4, session_epoch="scope-epoch",
        deadline_epoch=__import__("time").time() + 60, input_generation=0,
        is_current=lambda: True,
    ) as scope:
        first = asyncio.run(cycle.decide_and_execute(frame(), "frame.jpg", "ctx", "choose", dispatched))
        assert scope.expected_input_generation == 1
        second = asyncio.run(cycle.decide_and_execute(frame(), "frame.jpg", "ctx", "choose", dispatched))
        assert scope.expected_input_generation == 2
    assert first.receipt["scope_task_id"] == "child-3"
    assert first.receipt["scope_objective_revision"] == 4
    assert first.receipt["receipt_sequence"] == 1
    assert second.receipt["receipt_sequence"] == 2
    assert cycle.receipt_sequence == 2
    snapshot = cycle.last_receipt
    snapshot["selected_binding"]["keycode"] = 99
    assert cycle.last_receipt["selected_binding"]["keycode"] == 12
    store.close()


def test_scope_deadline_cancels_native_hold_releases_input_and_keeps_receipt(tmp_path):
    import time

    class Backend:
        def __init__(self): self.events = []
        def key(self, keycode, down): self.events.append(("key", keycode, down))
        def mouse_button(self, *args): self.events.append(("mouse", *args))
        def mouse_move(self, *args): self.events.append(("move", *args))
        def text(self, value): self.events.append(("text", len(value)))
        def release_all(self): self.events.append(("release_all",))

    rect = Rect(0, 0, 800, 600)
    backend = Backend()
    executor = SafeExecutor(backend, lambda: GateSnapshot(7, 7, True, rect, rect, True, True),
                            heartbeat_timeout=10)
    store = EventStore(tmp_path / "events.sqlite3")
    sage = FakeSage("inspect_quests")
    cycle = DecisionCycle(sage, executor, store, session_epoch="scope-epoch")
    candidates = [ActionCandidate("inspect_quests", "Press one key.",
                                  {"type": "keypress", "keycode": 12, "hold_seconds": .5}),
                  ActionCandidate("wait", "Wait.", {"type": "observe_only"})]

    async def scenario():
        executor.arm()
        executor.heartbeat()
        with cycle.scoped_execution_scope(
            task_id="child-deadline", objective_revision=1, session_epoch="scope-epoch",
            deadline_epoch=time.time() + .04, input_generation=0,
            is_current=lambda: True,
        ):
            return await cycle.decide_and_execute(frame(), "frame.jpg", "ctx", "choose", candidates)

    result = asyncio.run(scenario())
    assert result.status == "execution_failed"
    assert result.receipt and result.receipt["possible_input"] and result.receipt["effect_status"] == "unknown"
    assert result.receipt["scope_task_id"] == "child-deadline"
    assert ("key", 12, True) in backend.events and ("key", 12, False) in backend.events
    assert backend.events[-1] == ("release_all",)
    assert cycle.last_receipt["receipt_id"] == result.receipt["receipt_id"]
    assert cycle.receipt_sequence == 1
    executor._disarm("test_complete")
    store.close()


@pytest.mark.parametrize("failure,expected_dispatched", [("move", False), ("up", True)])
def test_real_executor_receipt_reports_partial_backend_submission(tmp_path, failure, expected_dispatched):
    class Backend:
        def __init__(self): self.events=[]
        def key(self, keycode, down):
            self.events.append(("key", keycode, down))
            if failure == "up" and not down: raise RuntimeError("key-up backend failed")
        def mouse_move(self, x, y):
            self.events.append(("move", x, y))
            if failure == "move": raise RuntimeError("mouse move backend failed")
        def mouse_button(self, *args): self.events.append(("mouse", *args))
        def text(self, value): self.events.append(("text", value))
        def release_all(self): pass

    rect=Rect(0,0,800,600);backend=Backend()
    executor=SafeExecutor(backend,lambda:GateSnapshot(1,1,True,rect,rect,True,True))
    store=EventStore(tmp_path / "events.sqlite3")
    if failure == "move":
        candidates=[ActionCandidate("click","Click point",{"type":"click","image_x":2,"image_y":2,"button":0}),
                    ActionCandidate("wait","Wait",{"type":"observe_only"})]
        sage=FakeSage("click")
    else:
        candidates=[ActionCandidate("keypress","Press key",{"type":"keypress","keycode":12,"hold_seconds":.01}),
                    ActionCandidate("wait","Wait",{"type":"observe_only"})]
        sage=FakeSage("keypress")
    async def run():
        executor.arm();executor.heartbeat()
        return await DecisionCycle(sage,executor,store).decide_and_execute(
            frame(),"frame.jpg","context","choose",candidates)
    result=asyncio.run(run())
    assert result.status=="execution_failed"
    assert result.receipt["possible_input"] is True
    assert result.receipt["dispatched"] is expected_dispatched
    assert result.receipt["dispatch_unknown"] is True
    assert result.receipt["generation_after"] > result.receipt["generation_before"]
    if failure=="up":
        assert [step["status"] for step in result.receipt["input_steps"]]==[
            "completed","failed_effect_unknown"]
    else:
        assert [step["status"] for step in result.receipt["input_steps"]]==["failed_effect_unknown"]
    store.close()


def test_semantic_change_discards_late_response_without_dispatch(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    release = asyncio.Event()
    sage, executor = FakeSage(gate=release), FakeExecutor()
    cycle = DecisionCycle(sage, executor, store)
    observed = frame()

    async def run():
        pending = asyncio.create_task(cycle.decide_and_execute(observed, "frame.jpg", "context", "choose", candidate_set()))
        while sage.calls == 0:
            await asyncio.sleep(0)
        cycle.invalidate("target_replaced")
        release.set()
        return await pending

    result = asyncio.run(run())
    assert result.status == "invalidated"
    assert executor.calls == []
    assert store.recent()[-1]["event_type"] == "sage_response_discarded"
    store.close()


def test_unrelated_physical_input_during_sage_wait_invalidates_late_dispatch(tmp_path):
    store=EventStore(tmp_path/"events.sqlite3");release=asyncio.Event();executor=FakeExecutor()
    sage=FakeSage(gate=release);cycle=DecisionCycle(sage,executor,store)
    async def run():
        pending=asyncio.create_task(cycle.decide_and_execute(
            frame(),"frame.jpg","context","choose",candidate_set()))
        while sage.calls==0: await asyncio.sleep(0)
        cycle._on_physical_step({"step_id":str(uuid4()),"execution_id":"unrelated",
            "kind":"key_down","details":{"keycode":1},"attempted":True,"status":"effect_unknown"})
        release.set()
        return await pending
    result=asyncio.run(run())
    assert result.status=="stale_input_generation"
    assert executor.calls==[]
    event=next(row for row in store.recent() if row["event_type"]=="sage_response_discarded")
    assert event["payload"]["reason"]=="input_generation_changed"
    store.close()


def test_precondition_is_rechecked_after_sage_returns(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    executor = FakeExecutor()
    cycle = DecisionCycle(FakeSage("inspect_quests"), executor, store)
    current = {"alive": True}
    candidates = [
        candidate_set()[0],
        ActionCandidate("inspect_quests", "Open the quest log.", {"type": "keypress", "keycode": 12, "hold_seconds": 0.08},
                        precondition=lambda: current["alive"]),
    ]
    current["alive"] = False
    result = asyncio.run(cycle.decide_and_execute(frame(), "frame.jpg", "context", "choose", candidates))
    assert result.status == "precondition_failed"
    assert not executor.calls
    store.close()


def test_only_one_decision_selection_runs_at_once(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    release = asyncio.Event()
    sage = FakeSage(gate=release)
    executor = FakeExecutor()
    cycle = DecisionCycle(sage, executor, store)

    async def run():
        first = asyncio.create_task(cycle.decide_and_execute(frame(), "one.jpg", "context", "choose", candidate_set()))
        while sage.calls == 0:
            await asyncio.sleep(0)
        second = await cycle.decide_and_execute(frame(), "two.jpg", "context", "choose", candidate_set())
        release.set()
        return second, await first

    second, first = asyncio.run(run())
    assert second.status == "decision_in_flight"
    assert first.status == "dispatched"
    assert sage.calls == 1
    store.close()
