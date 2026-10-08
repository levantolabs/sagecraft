import asyncio

import pytest

from sage_wow.control.camera_commands import CAMERA_ZOOM_COMMANDS, camera_zoom_command
from sage_wow.control.executor import ExecutionRejected, GateSnapshot, SafeExecutor
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


@pytest.mark.parametrize("binding,expected", [
    ({"type": "camera_zoom", "direction": "in", "steps": 1}, "/run CameraZoomIn(1)"),
    ({"type": "camera_zoom", "direction": "out", "steps": 1}, "/run CameraZoomOut(1)"),
])
def test_camera_commands_resolve_only_fixed_single_steps(binding, expected):
    assert camera_zoom_command(binding) == expected


def test_camera_zoom_api_allowlist_has_only_two_fixed_commands():
    assert CAMERA_ZOOM_COMMANDS == {
        ("in", 1): "/run CameraZoomIn(1)",
        ("out", 1): "/run CameraZoomOut(1)",
    }


@pytest.mark.parametrize("direction,command", [
    ("in", "/run CameraZoomIn(1)"),
    ("out", "/run CameraZoomOut(1)"),
])
def test_executor_camera_zoom_sends_one_fixed_command_through_existing_gated_path(direction, command):
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate)

    async def run():
        executor.arm()
        result = await executor.execute({"type": "camera_zoom", "direction": direction, "steps": 1})
        await executor.stop()
        return result

    result = asyncio.run(run())
    assert result == {
        "dispatched": True, "kind": "camera_zoom", "direction": direction,
        "steps": 1, "command": command,
        "result": "unverified; inspect a fresh screenshot",
    }
    assert backend.events.count(("text", command)) == 1
    assert [event for event in backend.events if event[0] in {"key", "text"}] == [
        ("key", 53, True), ("key", 53, False),
        ("key", 36, True), ("key", 36, False), ("text", command),
        ("key", 36, True), ("key", 36, False),
    ]


@pytest.mark.parametrize("binding", [
    {"type": "camera_zoom", "direction": "in", "steps": 2},
    {"type": "camera_zoom", "direction": "out", "steps": True},
    {"type": "camera_zoom", "direction": "in", "steps": 1, "command": "/run /say unsafe"},
    {"type": "camera_zoom", "direction": "/run something", "steps": 1},
])
def test_executor_camera_zoom_rejects_unbounded_or_injected_bindings(binding):
    backend = FakeBackend()
    executor = SafeExecutor(backend, valid_gate)

    async def run():
        executor.arm()
        try:
            with pytest.raises(ExecutionRejected, match="allowlisted zoom pulse"):
                await executor.execute(binding)
        finally:
            await executor.stop()

    asyncio.run(run())
    assert not any(event[0] == "text" for event in backend.events)
    assert not any(event[0] == "key" for event in backend.events)
