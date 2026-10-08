import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from sage_wow.agent.evidence import EvidenceArchive, EvidenceRetentionError
from sage_wow.models import Frame
from sage_wow.sage.client import SageClient, DecisionEnvelope, BatchRequestGroup, ImageBatchContent, BatchQuestion
from sage_wow.storage import EventStore
from test_sage_client import candidates, image_file


def test_shared_composite_overwrite_and_source_prune_preserve_exact_images(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3');archive=EvidenceArchive(tmp_path/'archive',store)
    source=image_file(tmp_path);first=source.read_bytes()
    frame=Frame.create('offline',64,48,image_path=str(source))
    retained_frame = archive.frame(frame,'E',0)
    assert archive.frame(frame,'E',0) == retained_frame
    snapshot=archive.request_image(source,request_id='r1',frame_id=frame.frame_id,epoch='E')
    Image.new('RGB',(64,48),'red').save(source);second=source.read_bytes()
    next_snapshot=archive.request_image(source,request_id='r2',frame_id='f2',epoch='E')
    source.unlink()
    assert retained_frame.read_bytes() == first  # Long service/recovery steps retain their source.
    assert snapshot != next_snapshot and snapshot.read_bytes()==first and next_snapshot.read_bytes()==second
    records=[e['payload'] for e in store.recent(10) if e['event_type']=='trial_request_image_retained']
    assert records[0]['request_id']=='r1' and records[1]['request_id']=='r2'
    store.close()


@pytest.mark.parametrize('batch',[False,True])
def test_archived_wire_image_equals_actual_mock_http_payload(tmp_path,batch):
    store=EventStore(tmp_path/'events.sqlite3');archive=EvidenceArchive(tmp_path/'archive',store)
    path=image_file(tmp_path);options=candidates();envelope=DecisionEnvelope.create('F','E',options)
    seen=[]
    def handler(req):
        payload=json.loads(req.content)
        media=payload['requests'][0]['content']['media'] if batch else payload['content']['media']
        seen.append(base64.b64decode(media.split(',')[1]))
        # Deliberately terminate with an error after the mock sees the exact outbound image.
        return httpx.Response(400,json={'error':'offline rejection'})
    async def run():
        client=SageClient('offline-fixture',transport=httpx.MockTransport(handler))
        client.image_evidence_sink=archive.wire_image
        try:
            from sage_wow.sage.client import SageRequestError
            with pytest.raises(SageRequestError):
                if batch:
                    await client.decide_batch([BatchRequestGroup(ImageBatchContent(path,'context'),(
                        BatchQuestion('question','choose',tuple(envelope.candidate_options)),))])
                else:
                    await client.decide_image_choice(path,'context','choose',options,envelope)
        finally:await client.close()
    asyncio.run(run())
    record=[e['payload'] for e in store.recent(10) if e['event_type']=='trial_wire_image_retained'][0]
    assert Path(record['path']).read_bytes()==seen[0]
    assert record.get('question_ids')==['question'] if batch else record['request_id']==envelope.request_id
    store.close()


def test_evidence_capacity_failure_latches_and_sends_no_request(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3');archive=EvidenceArchive(tmp_path/'archive',store,max_bytes=1)
    seen=[]
    async def run():
        client=SageClient('offline-fixture',transport=httpx.MockTransport(lambda r:seen.append(r)))
        client.image_evidence_sink=archive.wire_image
        opts=candidates();envelope=DecisionEnvelope.create('F','E',opts)
        try:
            with pytest.raises(EvidenceRetentionError):
                await client.decide_image_choice(image_file(tmp_path),'context','choose',opts,envelope)
        finally:await client.close()
    asyncio.run(run())
    assert archive.failed and not seen
    with pytest.raises(EvidenceRetentionError):archive.retain(b'','png')
    store.close()


def test_reused_frame_identity_with_different_pixels_stops_evidence(tmp_path):
    store=EventStore(tmp_path/'events.sqlite3');archive=EvidenceArchive(tmp_path/'archive',store)
    path=image_file(tmp_path);frame=Frame.create('offline',64,48,image_path=str(path))
    archive.frame(frame,'E',0)
    Image.new('RGB',(64,48),'red').save(path)
    with pytest.raises(EvidenceRetentionError):archive.frame(frame,'E',0)
    assert archive.failed
    store.close()


