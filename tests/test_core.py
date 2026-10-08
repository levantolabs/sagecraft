from pathlib import Path

import pytest

from sage_wow.config import load_profile, session_data_dir
from sage_wow.platform.macos.geometry import Rect
from sage_wow.platform.macos.hotkey import matches_hotkey, parse_hotkey
from sage_wow.platform.macos.system import _plain_bounds
from sage_wow.perception.ocr import normalized_box_to_pixels
from sage_wow.replay import ReplaySession
from sage_wow.storage import EventStore


ROOT = Path(__file__).resolve().parents[1]


def test_template_profile_keeps_setup_unknowns_explicit():
    profile = load_profile(ROOT / "profiles/template.yaml")
    assert profile.edition == "wow_forever"
    assert profile.values["game"]["environment"] is None
    assert profile.values["character"]["class"] == "priest"
    assert profile.values["character"]["name"] is None
    assert profile.window_id is None


def test_environment_data_namespaces_are_separate_and_path_safe(tmp_path):
    template = load_profile(ROOT / "profiles/template.yaml")
    from sage_wow.config import Profile
    beta = Profile(template.path, {**template.values, "game": {**template.values["game"], "environment": "beta"}})
    live_values = {**beta.values, "game": {**beta.values["game"], "environment": "live"},
                   "character": {**beta.values["character"], "name": "../Hero"}}
    live = Profile(beta.path, live_values)
    beta_dir = session_data_dir(tmp_path, beta)
    live_dir = session_data_dir(tmp_path, live)
    assert beta_dir != live_dir
    assert "live" in str(live_dir)
    assert live_dir.is_relative_to(tmp_path)


def test_replay_fixture_loads_without_image_or_native_macos_modules():
    session = ReplaySession(ROOT / "fixtures/replay/first-observation.jsonl")
    assert [frame.frame_id for frame in session.frames()] == ["fixture-frame-001"]
    assert [event.event_type for event in session.events()] == ["session_started", "observation"]


def test_window_image_coordinate_transform_handles_retina_and_negative_origins():
    rect = Rect(-1800, 40, 1200, 800)
    assert rect.image_to_desktop(1000, 500, 2400, 1600) == (-1300, 290)


def test_macos_window_bounds_are_yaml_safe_even_with_bridged_string_keys():
    import yaml

    class BridgedString(str):
        pass

    bounds = _plain_bounds({BridgedString("X"): 1.5, BridgedString("Y"): 2.2,
                            BridgedString("Width"): 1200.0, BridgedString("Height"): 800.0})
    assert bounds == {"X": 2, "Y": 2, "Width": 1200, "Height": 800}
    assert yaml.safe_load(yaml.safe_dump(bounds)) == bounds


def test_vision_normalized_bottom_left_box_maps_to_top_left_image_pixels():
    assert normalized_box_to_pixels((0.1, 0.2, 0.3, 0.1), 1000, 500) == {
        "x": 100, "y": 350, "width": 300, "height": 50,
    }


def test_stop_hotkey_parser_uses_physical_keycodes_and_requires_modifier():
    spec = parse_hotkey("CTRL+Option+Escape")
    class QuartzFlags:
        kCGEventFlagMaskControl = 1
        kCGEventFlagMaskAlternate = 2
    assert spec.keycode == 53
    assert matches_hotkey(spec, 53, 3, QuartzFlags)
    assert not matches_hotkey(spec, 53, 1, QuartzFlags)
    assert parse_hotkey("ctrl+keycode:12").keycode == 12
    with pytest.raises(ValueError, match="modifier"):
        parse_hotkey("escape")


def test_event_store_migrates_and_round_trips_events(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    from sage_wow.models import Event

    event = Event.create("observation", {"game_edition": "wow_forever", "fact": None})
    store.append(event)
    store.append(event)  # Replaying the same stable event ID is safe and idempotent.
    assert store.recent() == [{"event_id": event.event_id, "occurred_at": event.occurred_at,
                               "event_type": "observation", "payload": {"game_edition": "wow_forever", "fact": None}}]
    store.close()
