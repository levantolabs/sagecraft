from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from sage_wow.models import Frame
from sage_wow.perception import ocr
from sage_wow.platform.macos import capture


@pytest.fixture
def native_pool(monkeypatch):
    state = {"active": False, "entered": 0, "exited": 0}

    @contextmanager
    def pool():
        assert not state["active"]
        state["active"] = True
        state["entered"] += 1
        try:
            yield
        finally:
            state["active"] = False
            state["exited"] += 1

    monkeypatch.setitem(__import__("sys").modules, "objc", SimpleNamespace(autorelease_pool=pool))
    monkeypatch.setattr(capture.sys, "platform", "darwin")
    return state


def test_capture_entrypoint_drains_pool_after_materializing_frame(monkeypatch, native_pool):
    frame = Frame.create("test", 100, 100)

    def implementation(_window_id, _output_dir):
        assert native_pool["active"]
        return frame

    monkeypatch.setattr(capture, "_capture_window", implementation)
    assert capture.capture_window(17) is frame
    assert native_pool == {"active": False, "entered": 1, "exited": 1}


def test_capture_entrypoint_drains_pool_when_capture_raises(monkeypatch, native_pool):
    def implementation(*_args):
        assert native_pool["active"]
        raise capture.CaptureError("capture failed")

    monkeypatch.setattr(capture, "_capture_window", implementation)
    with pytest.raises(capture.CaptureError):
        capture.capture_window(17)
    assert native_pool == {"active": False, "entered": 1, "exited": 1}


def test_ocr_entrypoint_returns_only_materialized_values_inside_pool(monkeypatch, native_pool):
    observations = [ocr.TextObservation("Quest", 0.9, {"x": 1, "y": 2, "width": 3, "height": 4})]

    def implementation(_path, _language, _accurate):
        assert native_pool["active"]
        return observations

    monkeypatch.setattr(ocr, "_recognize_text", implementation)
    assert ocr.recognize_text(__import__("pathlib").Path("frame.jpg")) == observations
    assert native_pool == {"active": False, "entered": 1, "exited": 1}

