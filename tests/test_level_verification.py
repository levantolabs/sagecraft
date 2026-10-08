from datetime import datetime, timedelta
from types import SimpleNamespace

from PIL import Image, ImageDraw

from sage_wow.agent.level_verification import PlayerLevelVerifier, player_level_assessment_image
from sage_wow.models import Frame


def make_decision(frame, epoch, chosen, request_id="request-1", probability=.9, model="sage-test"):
    return SimpleNamespace(
        chosen=chosen,
        probability=probability,
        model=model,
        envelope=SimpleNamespace(
            request_id=request_id,
            session_epoch=epoch,
            captured_frame_id=frame.frame_id,
        ),
    )


def test_player_level_image_preserves_full_frame_and_enlarges_only_player_unit_region(tmp_path):
    width, height = 1000, 600
    image = Image.new("RGB", (width, height), "#263746")
    draw = ImageDraw.Draw(image)
    draw.rectangle((155, 400, 180, 430), fill="#2de710")  # player badge area
    draw.rectangle((170, 446, 174, 450), fill="#f70dc7")  # bottom of level circle
    draw.rectangle((820, 400, 845, 430), fill="#ef1721")  # target badge area
    source = tmp_path / "frame.png"
    image.save(source)
    frame = Frame.create("test", width, height, image_path=str(source))

    result_path = player_level_assessment_image(frame)

    with Image.open(source) as original, Image.open(result_path) as result:
        crop_panel = result.convert("RGB").crop((10, 42, result.width, result.height))
        colors = set(crop_panel.get_flattened_data())
        assert (45, 231, 16) in colors
        assert (247, 13, 199) in colors
        assert (239, 23, 33) not in colors
        assert result.width == 480
        assert result.size != original.size
        assert "player-level-" in result_path.name


def test_level_verifier_is_periodic_and_level_evidence_is_session_scoped():
    verifier = PlayerLevelVerifier(interval_seconds=300, max_frame_age_seconds=10)
    assert verifier.due(100) is False  # no session has been activated
    assert verifier.activate_session("epoch-a") is False
    assert verifier.due(100)
    verifier.note_request_started(100)
    assert not verifier.due(399)
    assert verifier.due(400)

    frame = Frame.create("test", 100, 100, image_path="unused")
    fresh_now = datetime.fromisoformat(frame.captured_at)
    decision = make_decision(frame, "epoch-a", "player_level_2")
    observed = verifier.record(decision=decision, frame=frame, now=fresh_now)
    assert observed["level"] == 2
    assert observed["level_change_vs_same_session_observation"] == "baseline_candidate_one_fresh_frame"
    assert verifier.last_confirmed_level is None
    assert observed["frame_id"] == frame.frame_id
    assert observed["request_id"] == "request-1"
    assert observed["source"] == "Sage visual estimate of player level badge in the isolated player-frame crop"
    assert any("not a numeric XP reading" in item for item in observed["limitations"])

    next_frame = Frame.create("test", 100, 100, image_path="unused")
    next_now = datetime.fromisoformat(next_frame.captured_at)
    baseline = verifier.record(decision=make_decision(next_frame, "epoch-a", "player_level_2", "request-2"),
                               frame=next_frame, now=next_now)
    assert baseline["level_change_vs_same_session_observation"] == "baseline_confirmed_two_fresh_frames"
    assert verifier.last_confirmed_level == 2

    candidate_frame = Frame.create("test", 100, 100, image_path="unused")
    higher_candidate = verifier.record(
        decision=make_decision(candidate_frame, "epoch-a", "player_level_5", "request-3"),
        frame=candidate_frame, now=datetime.fromisoformat(candidate_frame.captured_at))
    assert higher_candidate["previous_level"] == 2
    assert higher_candidate["level_change_vs_same_session_observation"] == "higher_displayed_level_candidate_one_fresh_frame"
    assert verifier.last_confirmed_level == 2
    confirming_frame = Frame.create("test", 100, 100, image_path="unused")
    higher = verifier.record(
        decision=make_decision(confirming_frame, "epoch-a", "player_level_5", "request-4"),
        frame=confirming_frame, now=datetime.fromisoformat(confirming_frame.captured_at))
    assert higher["previous_level"] == 2
    assert higher["level_change_vs_same_session_observation"] == "higher_displayed_level_confirmed_two_fresh_frames"
    assert verifier.last_confirmed_level == 5
    unknown_frame = Frame.create("test", 100, 100, image_path="unused")
    unknown = verifier.record(decision=make_decision(unknown_frame, "epoch-a", "player_level_unknown", "request-5"),
                              frame=unknown_frame, now=datetime.fromisoformat(unknown_frame.captured_at))
    assert unknown["level"] is None
    assert verifier.last_confirmed_level == 5

    assert verifier.activate_session("epoch-b") is True
    assert verifier.last_confirmed_level is None
    assert verifier.latest_observation is None
    assert verifier.history == []
    assert verifier.due(401)


def test_level_verifier_rejects_wrong_frame_epoch_and_stale_responses():
    verifier = PlayerLevelVerifier(max_frame_age_seconds=10)
    verifier.activate_session("epoch-a")
    frame = Frame.create("test", 100, 100, image_path="unused")
    now = datetime.fromisoformat(frame.captured_at)
    wrong_frame = make_decision(frame, "epoch-a", "player_level_2")
    wrong_frame.envelope.captured_frame_id = "different-frame"
    assert verifier.record(decision=wrong_frame, frame=frame, now=now) is None
    assert verifier.record(decision=make_decision(frame, "epoch-b", "player_level_2"), frame=frame, now=now) is None
    old = datetime.fromisoformat(frame.captured_at) + timedelta(seconds=11)
    assert verifier.record(decision=make_decision(frame, "epoch-a", "player_level_2"), frame=frame, now=old) is None
    assert verifier.last_confirmed_level is None


def test_fresh_verifier_has_no_inherited_level_from_a_prior_runtime():
    verifier = PlayerLevelVerifier()
    verifier.activate_session("new-process-epoch")
    assert verifier.last_confirmed_level is None
    assert verifier.latest_observation is None
    assert verifier.history == []
