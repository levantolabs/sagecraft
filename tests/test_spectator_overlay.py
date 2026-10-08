"""Native shell boundaries using fake renderer/gates; no native UI or game IO."""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from sage_wow.control.executor import GateSnapshot
from sage_wow.platform.macos.geometry import Rect
from sage_wow.platform.macos.spectator_overlay import (
    DisplayGate, OverlayBridge, ProfileWindowProbe, cocoa_bounds, safe_state_directory, update_script,
)


class Renderer:
    def __init__(self):
        self.scripts = []
        self.callbacks = []
        self.events = []

    def submit(self, script, callback):
        self.scripts.append(script)
        self.callbacks.append(callback)

    def hide(self): self.events.append(("hide",))
    def show(self): self.events.append(("show",))
    def set_frame(self, bounds): self.events.append(("frame", bounds))


def snapshot(**changes):
    return {"source": "/offline/session-one", "session_id": "one", "source_revision": 1,
            "window_id": 6184, "state": "PLAYING", **changes}


def rig():
    now = [100.]
    gate = [DisplayGate(True, "valid", (0, 0, 1496, 967))]
    renderer = Renderer()
    bridge = OverlayBridge(renderer, lambda _: gate[0], clock=lambda: now[0])
    return now, gate, renderer, bridge


def test_waits_for_acknowledged_current_render_before_showing():
    now, gate, renderer, bridge = rig()
    bridge.tick(snapshot(), observed_at=now[0])
    assert not bridge.visible and not any(e[0] == "show" for e in renderer.events)
    renderer.callbacks[-1](True, None)
    bridge.tick(snapshot(), observed_at=now[0])
    assert bridge.visible and renderer.events[-1] == ("show",)
    assert ("frame", (0, 0, 1496, 967)) in renderer.events


def test_hides_on_focus_loss_while_javascript_is_pending_and_does_not_show_from_callback():
    now, gate, renderer, bridge = rig()
    bridge.tick(snapshot(), observed_at=now[0]); renderer.callbacks[-1](True, None)
    bridge.tick(snapshot(), observed_at=now[0])
    assert bridge.visible and bridge.pending
    gate[0] = DisplayGate(False, "game_not_foreground")
    bridge.tick(snapshot(), observed_at=now[0])
    assert not bridge.visible and renderer.events[-1] == ("hide",)
    renderer.callbacks[-1](True, None)
    assert not bridge.visible
    bridge.tick(snapshot(), observed_at=now[0])
    assert not bridge.visible and bridge.reason == "game_not_foreground"


@pytest.mark.parametrize("changes", [{"session_id": "two"}, {"source": "/offline/two"},
                                      {"window_id": 99}, {"source_revision": 2}])
def test_late_previous_source_render_cannot_expose_old_session(changes):
    now, gate, renderer, bridge = rig()
    bridge.tick(snapshot(), observed_at=now[0]); prior = renderer.callbacks[-1]
    bridge.tick(snapshot(**changes), observed_at=now[0])
    assert not bridge.visible
    prior(True, None)
    bridge.tick(snapshot(**changes), observed_at=now[0])
    assert not bridge.visible and bridge.rendered_identity is None
    renderer.callbacks[-1](True, None)
    bridge.tick(snapshot(**changes), observed_at=now[0])
    assert bridge.visible


@pytest.mark.parametrize("result,error", [(False, None), (None, None), (None, "web failure")])
def test_page_not_ready_or_failed_bridge_never_shows(result, error):
    now, gate, renderer, bridge = rig()
    bridge.tick(snapshot(), observed_at=now[0])
    renderer.callbacks[-1](result, error)
    bridge.tick(snapshot(), observed_at=now[0])
    assert not bridge.visible


def test_bridge_accepts_objective_c_boolean_value_without_singleton_assumption():
    class NativeTrue:
        def __eq__(self, other): return other is True
    now, gate, renderer, bridge = rig()
    bridge.tick(snapshot(), observed_at=now[0])
    renderer.callbacks[-1](NativeTrue(), None)
    bridge.tick(snapshot(), observed_at=now[0])
    assert bridge.visible


def test_timeout_retires_old_callback_and_delayed_feed_hides_last_render():
    now, gate, renderer, bridge = rig()
    bridge.tick(snapshot(), observed_at=now[0]); first = renderer.callbacks[-1]
    now[0] += 2.1
    bridge.tick(snapshot(), observed_at=now[0]); second = renderer.callbacks[-1]
    assert first is not second and not bridge.visible
    first(True, None)
    assert bridge.rendered_identity is None
    second(True, None)
    bridge.tick(snapshot(), observed_at=now[0]); assert bridge.visible
    now[0] += 2.1
    bridge.tick(snapshot(), observed_at=now[0]-2.1)
    assert not bridge.visible and bridge.reason == "feed_unavailable_or_delayed"


def test_unavailable_or_stale_feed_does_not_invent_new_gameplay():
    now, gate, renderer, bridge = rig()
    data = snapshot(state="STALE", decision=None)
    bridge.tick(data, observed_at=now[0]); renderer.callbacks[-1](True, None)
    bridge.tick(data, observed_at=now[0])
    assert bridge.visible  # Frontend explicitly receives the STALE state.
    assert json.dumps(json.dumps("STALE"))[1:-1] in renderer.scripts[-1]
    bridge.tick(None, observed_at=now[0])
    assert not bridge.visible


def test_bridge_serializes_strings_as_data_and_rejects_non_json_numbers():
    data = snapshot(decision='</script>"; window.attack(); // \u2028')
    script = update_script(data)
    encoded = script.split("JSON.parse(", 1)[1].rsplit(")), true)", 1)[0]
    assert json.loads(json.loads(encoded)) == data
    with pytest.raises(ValueError): update_script(snapshot(age=float("nan")))


def profile_probe(tmp_path):
    path = tmp_path / "profile.yaml"; path.write_text("offline profile")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    gate = [GateSnapshot(6184, 6184, True, Rect(20, 30, 1496, 967), Rect(20, 30, 1496, 967), True, True)]
    calls = []
    def inspect(profile): calls.append(profile); return gate[0]
    probe = ProfileWindowProbe(load_profile=lambda _: SimpleNamespace(window_id=6184),
                               current_gate=inspect, primary_top=lambda: 1200.)
    return path, gate, calls, probe, snapshot(profile_path=str(path), profile_sha256=digest)


def test_exact_profile_identity_rechecked_each_tick_and_display_tracks_bounds(tmp_path):
    path, gate, calls, probe, data = profile_probe(tmp_path)
    assert probe(data).bounds == (20., 203., 1496., 967.)
    gate[0] = replace(gate[0], bounds=Rect(-1500, -100, 1300, 800), calibrated=False)
    assert probe(data).bounds == (-1500., 500., 1300., 800.)
    assert len(calls) == 2  # Profile parsing is cached; native authority is not.
    assert not gate[0].valid  # Display permission is never input calibration.


@pytest.mark.parametrize("case", ["focus", "identity", "wrong_window", "no_bounds",
                                 "profile_hash", "missing_hash", "missing_profile", "invalid_pointer"])
def test_native_gate_hides_on_unconfirmed_identity_focus_or_source(tmp_path, case):
    path, gate, calls, probe, data = profile_probe(tmp_path)
    if case == "focus": gate[0] = replace(gate[0], foreground=False)
    elif case == "identity": gate[0] = replace(gate[0], client_identity_verified=False)
    elif case == "wrong_window": gate[0] = replace(gate[0], window_id=9)
    elif case == "no_bounds": gate[0] = replace(gate[0], bounds=None)
    elif case == "profile_hash": path.write_text("changed profile")
    elif case == "missing_hash": data["profile_sha256"] = None
    elif case == "missing_profile": data["profile_path"] = None
    elif case == "invalid_pointer": data["window_id"] = None
    assert not probe(data).visible


def test_explicit_fallback_profile_does_not_override_invalid_pointer(tmp_path):
    path, gate, calls, _, data = profile_probe(tmp_path)
    probe = ProfileWindowProbe(profile_path=path, load_profile=lambda _: SimpleNamespace(window_id=6184),
                               current_gate=lambda _: gate[0], primary_top=lambda: 1200)
    assert probe(snapshot()).visible
    assert not probe(snapshot(window_id=None)).visible
    assert not probe(snapshot(window_id=99)).visible


def test_native_coordinates_and_status_output_reject_invalid_geometry_or_run_writes(tmp_path):
    assert cocoa_bounds(Rect(100, 30, 500, 300), 900) == (100., 570., 500., 300.)
    with pytest.raises(ValueError): cocoa_bounds(Rect(1, 2, 0, 300), 900)
    with pytest.raises(ValueError): cocoa_bounds(Rect(1, 2, 500, 300), float("nan"))
    run = tmp_path / "consumed-run"
    with pytest.raises(ValueError): safe_state_directory(run / "hud-state", run)
    with pytest.raises(ValueError): safe_state_directory(run, run)
    assert safe_state_directory(tmp_path / "hud-state", run) == tmp_path / "hud-state"
