import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from sage_wow.sage.client import (
    CandidateSet,
    ChoiceOption,
    DecisionEnvelope,
    SageClient,
    SageAllowanceError,
    SageAuthenticationError,
    SageConfigurationError,
    SageRequestError,
    SageResponseError,
    SageTransientError,
)
from sage_wow.sage.usage import estimate_decision_units


def candidates():
    return CandidateSet("test-v1", (
        ChoiceOption("inspect", "Open an inspection panel.", {"key": "i"}),
        ChoiceOption("wait", "Wait briefly.", {"type": "wait", "seconds": 1}),
    ))


def image_file(tmp_path: Path) -> Path:
    path = tmp_path / "frame.png"
    Image.new("RGB", (64, 48), (24, 48, 72)).save(path)
    return path


def test_image_choice_sends_supported_data_uri_and_binds_result(tmp_path):
    path = image_file(tmp_path)
    candidate_set = candidates()
    envelope = DecisionEnvelope.create("frame-7", "epoch-2", candidate_set)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        payload = json.loads(request.content)
        seen["payload"] = payload
        return httpx.Response(200, json={
            "id": envelope.request_id,
            "kind": "choice",
            "result": {"chosen": "inspect", "probability": 0.81,
                       "probabilities": [{"option": "inspect", "probability": 0.81}, {"option": "wait", "probability": 0.46}]},
            "meta": {"model": "sage-test", "latency_ms": 750, "usage": {"image_count": 1}},
        })

    async def call():
        client = SageClient("test-secret", transport=httpx.MockTransport(handler))
        try:
            return await client.decide_image_choice(path, "Level 3 priest", "Choose one.", candidate_set, envelope)
        finally:
            await client.close()

    decision = asyncio.run(call())
    payload = seen["payload"]
    media = payload["content"]["media"]
    assert seen["auth"] == "Bearer test-secret"
    assert media.startswith("data:image/jpeg;base64,")
    assert len(base64.b64decode(media.split(",", 1)[1])) <= 4 * 1024 * 1024
    assert payload["question"]["id"] == envelope.request_id
    assert payload["content"]["text"] == "Level 3 priest"
    assert decision.chosen == "inspect"
    assert decision.selected_binding == {"key": "i"}
    assert decision.envelope.captured_frame_id == "frame-7"
    assert decision.probabilities[0]["probability"] + decision.probabilities[1]["probability"] > 1


def test_null_choice_is_preserved_without_selecting_a_binding(tmp_path):
    candidate_set = candidates()
    envelope = DecisionEnvelope.create("frame-null", "epoch-2", candidate_set)

    def handler(request: httpx.Request) -> httpx.Response:
        # Sage rejects an explicitly empty image.text; image-only requests omit it.
        assert "text" not in json.loads(request.content)["content"]
        return httpx.Response(200, json={"id": envelope.request_id, "kind": "choice",
                                         "result": {"chosen": None, "probability": None, "probabilities": []}, "meta": {}})

    async def call():
        client = SageClient("test-secret", transport=httpx.MockTransport(handler))
        try:
            return await client.decide_image_choice(image_file(tmp_path), "", "Choose.", candidate_set, envelope)
        finally:
            await client.close()

    result = asyncio.run(call())
    assert result.chosen is None
    assert result.selected_binding is None


def test_candidate_version_and_response_option_are_checked(tmp_path):
    candidate_set = candidates()
    envelope = DecisionEnvelope.create("frame-8", "epoch-2", candidate_set)

    async def changed_set():
        client = SageClient("test-secret", transport=httpx.MockTransport(lambda request: httpx.Response(500)))
        try:
            changed = CandidateSet("test-v2", candidate_set.options)
            with pytest.raises(SageRequestError, match="Candidate set changed"):
                await client.decide_image_choice(image_file(tmp_path), "", "Choose.", changed, envelope)
        finally:
            await client.close()

    asyncio.run(changed_set())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": envelope.request_id, "kind": "choice",
                                         "result": {"chosen": "not-in-options", "probability": 0.9}, "meta": {}})

    async def unknown_option():
        client = SageClient("test-secret", transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(SageResponseError, match="outside the submitted candidate set"):
                await client.decide_image_choice(image_file(tmp_path), "", "Choose.", candidate_set, envelope)
        finally:
            await client.close()

    asyncio.run(unknown_option())


@pytest.mark.parametrize(("status", "error_type"), [
    (400, SageRequestError), (401, SageAuthenticationError), (402, SageAllowanceError),
    (502, SageTransientError), (503, SageTransientError), (520, SageTransientError),
])
def test_api_error_categories_are_distinct_and_credentials_are_redacted(tmp_path, status, error_type):
    candidate_set = candidates()
    envelope = DecisionEnvelope.create("frame-error", "epoch-2", candidate_set)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "credential rejected: test-secret"})

    async def call():
        client = SageClient("test-secret", transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(error_type) as exc:
                await client.decide_image_choice(image_file(tmp_path), "", "Choose.", candidate_set, envelope)
            assert "test-secret" not in str(exc.value)
            assert "[REDACTED]" in str(exc.value)
        finally:
            await client.close()

    asyncio.run(call())


def test_missing_api_key_remains_a_permanent_configuration_error():
    with pytest.raises(SageConfigurationError, match="SAGE_API_KEY is missing"):
        SageClient(" ")


def test_grounding_server_520_uses_the_same_retryable_classification():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(520, json={"detail": "upstream server error"})

    async def call():
        client=SageClient("test-secret",transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(SageTransientError,match="HTTP 520"):
                await client.decide_grounded_choice("guardrail","research",[
                    {"option":"recover","description":"Recover"},
                    {"option":"inspect","description":"Inspect"}],"grounding-520")
        finally:
            await client.close()

    asyncio.run(call())


def test_usage_estimate_uses_question_or_token_units_plus_unique_images():
    assert estimate_decision_units({"billed_input_tokens": 120, "image_count": 1}) == 2
    assert estimate_decision_units({"billed_input_tokens": 8500, "image_count": 1}) == 4
    assert estimate_decision_units({"image_count": 2}, question_count=3) == 5
    assert estimate_decision_units({}) is None


def test_grounded_choice_is_text_only_and_preserves_grounding_metadata():
    import asyncio,json,httpx
    from sage_wow.sage.client import SageClient
    sent=[]
    def handle(request):
        body=json.loads(request.content);sent.append(body)
        return httpx.Response(200,json={'id':'research-1','kind':'choice','result':{'chosen':'recover'},'grounding_meta':{'sources':[{'url':'https://example.test/official'}]}})
    async def run():
        client=SageClient('fixture-key',transport=httpx.MockTransport(handle))
        try:return await client.decide_grounded_choice('Guardrail: stuck','Investigate this guardrail',[{'option':'recover','description':'Recover'},{'option':'inspect','description':'Inspect'}],'research-1')
        finally:await client.close()
    result=asyncio.run(run())
    assert sent[0]['content']=='Guardrail: stuck'
    assert sent[0]['grounding']=={'trigger':'always'}
    assert result['grounding_meta']['sources'][0]['url']=='https://example.test/official'
