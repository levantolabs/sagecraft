import asyncio
import base64

import httpx
import pytest
from PIL import Image

from sage_wow.sage.client import (
    BatchQuestion, BatchRequestGroup, ImageBatchContent, SageClient, SageRequestError, SageResponseError,
)


def q(qid, *options):
    return BatchQuestion(qid, f"Answer question {qid} from the current evidence.",
                         tuple({"option": option, "description": option} for option in options))


def successful(question_id, chosen, *, reasoning=None):
    meta={"model":"sage-test"}
    if reasoning is not None: meta["reasoning"]=reasoning
    return {"ok":True,"result":{"id":question_id,"kind":"choice",
        "result":{"chosen":chosen,"probability":.8},"meta":meta}}


def response(groups, question_count, **meta):
    return {"results":[{"answers":answers} for answers in groups],
            "meta":{"request_count":len(groups),"question_count":question_count,**meta}}


def test_batch_groups_independent_questions_and_preserves_null_error_and_reasoning():
    captured={}
    body=response([
        [successful("target_state","alive",reasoning={"fired":False,"ran":False}),
         successful("player_health",None)],
        [{"ok":False,"error":"question budget unavailable"}],
    ],3,latency_ms=50)
    def handle(request):
        captured["payload"]=__import__("json").loads(request.content)
        return httpx.Response(200,json=body)
    async def run():
        client=SageClient("mock",transport=httpx.MockTransport(handle))
        try:
            groups=(BatchRequestGroup("same text evidence",(q("target_state","alive","dead"),
                        q("player_health","healthy","low"))),
                    BatchRequestGroup("other evidence",(q("optional_plan","act","observe"),)))
            return await client.decide_batch(groups,reasoning="on")
        finally: await client.close()
    result=asyncio.run(run())
    payload=captured["payload"]
    assert payload["reasoning"]=="on"
    assert payload["requests"][0]["content"]=="same text evidence"
    assert [item["id"] for item in payload["requests"][0]["questions"]]==["target_state","player_health"]
    assert len(result.groups)==2 and result.question_count==3
    assert result.groups[0].answers[0].chosen=="alive"
    assert result.groups[0].answers[0].meta["reasoning"]["ran"] is False
    assert result.groups[0].answers[1].ok and result.groups[0].answers[1].chosen is None
    assert not result.groups[1].answers[0].ok
    assert result.groups[1].answers[0].error=="question budget unavailable"
    assert result.meta["latency_ms"]==50
    assert result.request_ms>=0 and result.parse_ms>=0 and result.total_ms>=0


def test_batch_image_content_is_prepared_once_per_group(tmp_path):
    image=tmp_path/"frame.png";Image.new("RGB",(20,10),"red").save(image)
    captured={}
    def handle(request):
        captured["payload"]=__import__("json").loads(request.content)
        return httpx.Response(200,json=response([[successful("q1","yes")]],1))
    async def run():
        client=SageClient("mock",transport=httpx.MockTransport(handle))
        try:
            return await client.decide_batch((BatchRequestGroup(ImageBatchContent(image,"describe only current frame"),
                    (q("q1","yes","no"),)),))
        finally: await client.close()
    result=asyncio.run(run())
    content=captured["payload"]["requests"][0]["content"]
    assert content["kind"]=="image" and content["text"]=="describe only current frame"
    assert base64.b64decode(content["media"].split(",",1)[1])
    assert result.preparation_ms>=0


@pytest.mark.parametrize("bad_response",[
    {"results":[]},
    {"results":[{"answers":[]}]},
    {"results":[{"answers":[{"ok":True,"result":{"id":"other","kind":"choice","result":{"chosen":"a"}}}]}]},
    {"results":[{"answers":[successful("q1","outside")]}]},
])
def test_batch_rejects_malformed_group_answer_and_id_shapes(bad_response):
    def handle(_request): return httpx.Response(200,json=bad_response)
    async def run():
        client=SageClient("mock",transport=httpx.MockTransport(handle))
        try:
            with pytest.raises(SageResponseError):
                await client.decide_batch((BatchRequestGroup("text",(q("q1","a","b"),)),))
        finally: await client.close()
    asyncio.run(run())


def test_batch_validates_local_bound_ids_options_and_reasoning_before_transport():
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(200,json=response([[successful("q1","a")]],1))
    async def run():
        client=SageClient("mock",transport=httpx.MockTransport(handle),max_batch_questions=1)
        try:
            with pytest.raises(SageRequestError,match="configured local maximum"):
                await client.decide_batch((BatchRequestGroup("text",(q("q1","a","b"),q("q2","a","b"))),))
            with pytest.raises(ValueError,match="reasoning"):
                await client.decide_batch((BatchRequestGroup("text",(q("q1","a","b"),)),),reasoning="fast")
        finally: await client.close()
    asyncio.run(run())
    assert calls==[]
