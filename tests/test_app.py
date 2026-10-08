import asyncio
import json
from argparse import Namespace
from pathlib import Path

from sage_wow import app
from sage_wow.config import load_profile, session_data_dir
from sage_wow.sage.client import SageDecision
from sage_wow.storage import EventStore


def test_manual_frame_decision_is_counted_in_isolated_profile_session(monkeypatch, tmp_path, capsys):
    profile_path = Path(__file__).resolve().parents[1] / "profiles/template.yaml"
    profile = load_profile(profile_path)
    monkeypatch.setenv("SAGE_API_KEY", "test-key")

    class FakeSageClient:
        def __init__(self, **_kwargs):
            pass

        async def decide_image_choice(self, _path, _text, _instructions, candidates, envelope, **_kwargs):
            return SageDecision(envelope, "world_scene", {"type": "observe_only"}, 0.8, (),
                                "fake", 5, {"ran": False}, {"image_count": 1}, 10,
                                tuple(option.option for option in candidates.options))

        async def close(self):
            pass

    monkeypatch.setattr(app, "SageClient", FakeSageClient)
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text(json.dumps({
        "version": "safe-test-v1",
        "options": [
            {"option": "world_scene", "description": "Observe only.",
             "binding": {"type": "observe_only"}},
            {"option": "human_review", "description": "Ask for a human review.",
             "binding": {"type": "observe_only"}},
        ],
    }))
    args = Namespace(
        profile=str(profile_path), candidates=str(candidates_path), image="frame.jpg", frame_id="f1",
        session_epoch="test", text="", instructions="Choose.", reasoning="off", data_dir=str(tmp_path / "data"),
    )

    assert asyncio.run(app.decide_frame_async(args)) == 0
    capsys.readouterr()
    store = EventStore(session_data_dir(Path(args.data_dir), profile) / "events.sqlite3")
    try:
        rows = store.recent()
        assert len(rows) == 1
        assert rows[0]["event_type"] == "sage_decision"
        assert rows[0]["payload"]["estimated_decision_units"] == 2
    finally:
        store.close()
