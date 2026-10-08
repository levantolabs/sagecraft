from __future__ import annotations

from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
import sys

from sage_wow.platform.macos import system


class NativeString(str):
    """Stand-in for an Objective-C string wrapper returned by fake Quartz."""


def _fake_quartz(monkeypatch):
    quartz = ModuleType("Quartz")
    quartz.kCGWindowListOptionOnScreenOnly = 1
    quartz.kCGWindowListExcludeDesktopElements = 2
    quartz.kCGNullWindowID = 0
    quartz.kCGWindowBounds = "bounds"
    quartz.kCGWindowNumber = "number"
    quartz.kCGWindowOwnerName = "owner"
    quartz.kCGWindowOwnerPID = "pid"
    quartz.kCGWindowName = "title"
    quartz.kCGWindowLayer = "layer"
    quartz.kCGWindowIsOnscreen = "onscreen"
    quartz.CGWindowListCopyWindowInfo = lambda *_args: [{
        "bounds": {"X": 1.4, "Y": 2, "Width": 800, "Height": 600},
        "number": 88, "owner": NativeString("World of Warcraft"), "pid": 42,
        "title": NativeString("Game"), "layer": 0, "onscreen": True,
        "raw_bounds": {"X": 1.4, "Y": 2., "Width": 800., "Height": 600.},
    }]
    quartz.CGGetActiveDisplayList = lambda count, *_: (0, (1,) if count else (), 1)
    quartz.CGDisplayBounds = lambda _: SimpleNamespace(
        origin=SimpleNamespace(x=0, y=0), size=SimpleNamespace(width=1920, height=1080))
    monkeypatch.setitem(sys.modules, "Quartz", quartz)


def test_frontmost_snapshot_is_copied_inside_autorelease_pool(monkeypatch):
    events = []

    @contextmanager
    def pool():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    objc = ModuleType("objc")
    objc.autorelease_pool = pool
    monkeypatch.setitem(sys.modules, "objc", objc)
    _fake_quartz(monkeypatch)

    snapshot = system._frontmost_window_snapshot()

    assert events == ["enter", "exit"]
    assert snapshot == [{
        "window_id": 88, "owner": "World of Warcraft", "owner_pid": 42,
        "title": "Game", "bounds": {"X": 1, "Y": 2, "Width": 800, "Height": 600},
        "layer": 0, "onscreen": True,
        "raw_bounds": {"X": 1.4, "Y": 2., "Width": 800., "Height": 600.},
    }]
    row = snapshot[0]
    assert type(row["owner"]) is str and type(row["title"]) is str
    assert type(row["window_id"]) is int and type(row["owner_pid"]) is int
    assert all(type(value) is int for value in row["bounds"].values())


def test_frontmost_snapshot_falls_back_when_pyobjc_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "objc", None)
    _fake_quartz(monkeypatch)

    snapshot = system._frontmost_window_snapshot()

    assert snapshot[0]["window_id"] == 88
    assert snapshot[0]["owner"] == "World of Warcraft"
