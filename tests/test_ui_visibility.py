import asyncio

import pytest

from sage_wow.control.executor import ExecutionRejected, GateSnapshot, SafeExecutor
from sage_wow.control.ui_commands import CHAT_VISIBILITY_COMMANDS
from sage_wow.platform.macos.geometry import Rect


class FakeBackend:
    def __init__(self):
        self.events = []
        self.releases = 0

    def key(self, keycode, down):
        self.events.append(("key", keycode, down))

    def text(self, value):
        self.events.append(("text", value))

    def mouse_button(self, *args):
        self.events.append(("mouse", *args))

    def release_all(self):
        self.releases += 1


def valid_gate():
    bounds = Rect(0, 0, 1496, 967)
    return GateSnapshot(88196, 88196, True, bounds, bounds, True, True)


@pytest.mark.parametrize("action,command", sorted(CHAT_VISIBILITY_COMMANDS.items()))
def test_executor_submits_only_fixed_reversible_chat_visibility_commands(action, command):
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate)

    async def run():
        executor.arm()
        result = await executor.execute({"type": "ui_visibility", "action": action})
        await executor.stop()
        return result

    result = asyncio.run(run())
    assert result["dispatched"] is True
    assert result["kind"] == "ui_visibility"
    assert result["action"] == action
    assert result["command"] == command
    assert result["result"].startswith("unverified")
    assert ("text", command) in backend.events
    prefix = [] if action == 'enable_combat_log' else [("key", 53, True), ("key", 53, False)]
    assert [event for event in backend.events if event[0] in {"key", "text"}] == prefix + [
        ("key", 36, True), ("key", 36, False), ("text", command),
        ("key", 36, True), ("key", 36, False),
    ]


@pytest.mark.parametrize("binding", [
    {"type": "ui_visibility", "action": "say_hello"},
    {"type": "ui_visibility", "action": "/run ChatFrame1:Hide()"},
    {"type": "ui_visibility", "action": "hide_chat_overlay", "command": "/say hi"},
])
def test_executor_rejects_non_allowlisted_or_injected_ui_command(binding):
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate)

    async def run():
        executor.arm()
        try:
            with pytest.raises(ExecutionRejected, match="fixed allowlisted|unsupported fields"):
                await executor.execute(binding)
        finally:
            await executor.stop()

    asyncio.run(run())
    assert not any(event[0] == "text" for event in backend.events)
    assert not any(event[0] == "key" for event in backend.events)


@pytest.mark.parametrize("interrupt", ["stop", "focus"])
def test_ui_command_cancels_text_entry_then_stops_before_command_text_on_gate_failure(interrupt):
    class InterruptingBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.executor = None
            self.focus_lost = False

        def key(self, keycode, down):
            super().key(keycode, down)
            if keycode == 53 and not down:
                if interrupt == "stop":
                    self.executor._disarm("operator_stop")
                else:
                    self.focus_lost = True

    backend = InterruptingBackend()
    executor = SafeExecutor(backend, lambda: valid_gate() if not backend.focus_lost else
                            GateSnapshot(None, 88196, False, None, None, False, False))
    backend.executor = executor

    async def run():
        executor.arm()
        with pytest.raises(ExecutionRejected):
            await executor.execute({"type": "ui_visibility", "action": "hide_chat_overlay"})
        await executor.stop()

    asyncio.run(run())
    assert ("key", 53, True) in backend.events
    assert not any(event[0] == "text" for event in backend.events)
    assert not any(event == ("key", 36, True) for event in backend.events)


