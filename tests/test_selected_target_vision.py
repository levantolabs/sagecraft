"""Offline contract checks; these tests cannot capture or send game input."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from sage_wow.agent import selected_target_vision


def _image(tmp_path):
    path = tmp_path / "retained-frame.png"
    Image.new("RGB", (32, 24), "#354456").save(path)
    return path


def test_absence_and_uncertainty_are_distinct():
    absent = {"selected_hud": "absent", "name": None, "level": None,
              "target_kind": "unknown", "life_state": "unknown"}
    uncertain = {**absent, "selected_hud": "unknown"}
    assert selected_target_vision.parse_observation(json.dumps(absent)) == absent
    assert selected_target_vision.parse_observation(json.dumps(uncertain)) == uncertain


def _living():
    return {"selected_hud":"present","name":"Ragged Timber Wolf","level":2,
            "target_kind":"creature","life_state":"alive"}


def test_policy_never_converts_out_of_band_presence_to_absence():
    value = _living()
    assert selected_target_vision.eligibility(value,min_level=1,max_level=1) == "ineligible"
    assert value["selected_hud"] == "present"
    assert selected_target_vision.eligibility(value,min_level=1,max_level=2) == "eligible"
    assert selected_target_vision.eligibility({**value,"level":None},min_level=1,max_level=2) == "unknown"
    assert selected_target_vision.eligibility({**value,"life_state":"dead"},min_level=1,max_level=2) == "ineligible"
    assert selected_target_vision.eligibility({**value,"target_kind":"player"},min_level=1,max_level=2) == "ineligible"


@pytest.mark.parametrize("mutate", [
    {"frame_id":"another"}, {"scope_id":"old_epoch"}, {"image_sha256":"bad"},
    {"captured_at":"2026-10-01T00:00:00"}, {"captured_at":"2026-10-01T00:00:20+00:00"},
    {"captured_at":"2026-09-30T23:59:00+00:00"}, {"backend":"unattributed"},
])
def test_reject_stale_wrong_frame_wrong_scope_and_unattributed_facts(mutate):
    now=datetime(2026,10,1,tzinfo=timezone.utc).timestamp()
    result={"observation":_living(),"frame_id":"frame","scope_id":"scope",
        "captured_at":"2026-10-01T00:00:00+00:00","image_sha256":"a"*64,
        "backend":"sage","configured_model":"levanto-sage"}
    kwargs=dict(frame_id="frame",scope_id="scope",now_epoch=now,max_age_seconds=12)
    assert selected_target_vision.accept_observation(result,**kwargs) == _living()
    assert selected_target_vision.accept_observation({**result,**mutate},**kwargs) is None


def test_sage_independent_disagreement_is_unknown(tmp_path):
    class FakeSage:
        async def decide_batch(self,groups,**kwargs):
            assert kwargs == {"reasoning":"off","latency_mode":"quality"}
            choices={"selected_hud":"absent","name":"ragged_timber_wolf","level":"2",
                     "target_kind":"creature","life_state":"alive"}
            return SimpleNamespace(answers=[SimpleNamespace(question_id=k,chosen=v,ok=True)
                for k,v in choices.items()],meta={"model":"levanto-sage-v1.2"},raw={})
    result=asyncio.run(selected_target_vision.observe(_image(tmp_path),backend="sage",sage_client=FakeSage()))
    assert result["observation"]["selected_hud"] == "unknown"
    assert result["observation"]["level"] is None


def test_corpus_is_real_attributed_and_contains_negative_and_heldout_examples():
    from pathlib import Path
    cases=json.loads((Path(__file__).parent/"fixtures/selected_target_corpus.json").read_text())["cases"]
    assert any(c["expected"]["selected_hud"] == "absent" for c in cases)
    assert any(c["expected"]["level"] == 2 for c in cases)
    assert {c["split"] for c in cases} == {"development","validation"}
    for case in cases:
        assert len(case["sha256"]) == 64
        assert case.get("source_event_id") or case.get("source")


def test_crop_contract_and_no_padding_outside_frame(tmp_path):
    source=_image(tmp_path)
    path=selected_target_vision.prepare_target_image(source,(2,3,22,13),tmp_path/"target.png")
    with Image.open(path) as image:
        assert image.size == (60,30)
        assert image.format == "PNG"
    with pytest.raises(ValueError):
        selected_target_vision.prepare_target_image(source,(-1,0,20,10),tmp_path/"bad.png")


def test_quality_mode_reaches_actual_sage_payload_and_wire_archive(tmp_path):
    from sage_wow.sage.client import SageClient
    captured=[];wire=[]
    answers={"selected_hud":"present","name":"ragged_timber_wolf","level":"2",
             "target_kind":"creature","life_state":"alive"}
    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200,json={"results":[{"answers":[
            {"ok":True,"result":{"id":key,"kind":"choice","result":{"chosen":choice}}}
            for key,choice in answers.items()]}],"meta":{"model":"levanto-sage-v1.2",
                "request_count":1,"question_count":5,"usage":{"image_count":1}}})
    async def run():
        client=SageClient("fake",transport=httpx.MockTransport(handle))
        client.image_evidence_sink=lambda media,metadata:wire.append(metadata)
        try:
            return await selected_target_vision.observe(_image(tmp_path),backend="sage",sage_client=client)
        finally:
            await client.close()
    result=asyncio.run(run())
    assert captured[0]["latency_mode"] == "quality"
    assert wire[0]["latency_mode"] == "quality"
    assert captured[0]["reasoning"] == "off"
    assert len(captured[0]["requests"][0]["questions"]) == 5
    assert result["observation"] == _living()
    assert result["actual_model"] == "levanto-sage-v1.2"
