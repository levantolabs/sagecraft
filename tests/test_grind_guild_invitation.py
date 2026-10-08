"""Retained live-run invitation OCR must route pending loot through guarded UI.

Apple Vision OCR was run offline on retained screenshot SHA256
2bdedaff79c89313544756a13f96e3d1b6472548a420ee81083cde5193776b44.
Both below exact labels, bounds and confidences came from that 1496x967 image.
"""
import asyncio
from copy import deepcopy
import json

import pytest
from PIL import Image,ImageDraw

from sage_wow.agent.ui_reset import panel_cues
from sage_wow.models import Frame
from sage_wow.perception.ocr import TextObservation
from test_grind_loot_integration import LootRig


def retained_rows():
    return [TextObservation('Join Guild',1.,{'x':641,'y':311,'width':61,'height':11}),
        TextObservation('Decline Invitation',1.,{'x':755,'y':311,'width':108,'height':11})]


def test_exact_retained_guild_invitation_controls_are_blocking_ui():
    frame=Frame.create('offline-retained-geometry',1496,967)
    assert panel_cues(frame,retained_rows())==['decline invitation','join guild']


@pytest.mark.parametrize('case',['passive_chat','chat_location','edge_location','low_confidence','lone_control','different_rows','reversed','substring'])
def test_guild_words_without_confident_centered_control_pair_are_not_modal(case):
    rows=retained_rows()
    if case=='passive_chat':rows=[TextObservation('Someone says: Join Guild or Decline Invitation',1.,{'x':10,'y':760,'width':350,'height':15})]
    elif case=='lone_control':rows=rows[:1]
    else:
        changed=[]
        for index,row in enumerate(rows):
            bounds=dict(row.bounds);text=row.text;confidence=row.confidence
            if case=='chat_location':bounds.update(x=10+index*140,y=740)
            elif case=='edge_location':bounds['x']=10+index*140
            elif case=='low_confidence':confidence=.74
            elif case=='different_rows':bounds['y']+=index*50
            elif case=='reversed':bounds['x']=755 if index==0 else 641
            elif case=='substring':text='Please '+text
            changed.append(TextObservation(text,confidence,bounds))
        rows=changed
    assert panel_cues(Frame.create('offline',1496,967),rows)==[]


def attach_invitation(r):
    visible=[True];capture=r.world.capture;ocr=r.world.ocr
    rows=[TextObservation(row.text,row.confidence,{key:round(value*(500/1496 if key in ('x','width') else 300/967))
        for key,value in row.bounds.items()}) for row in retained_rows()]
    def fake_capture():
        frame=capture()
        if visible[0]:
            with Image.open(frame.image_path) as image:scene=image.convert('RGB')
            draw=ImageDraw.Draw(scene);draw.rectangle((185,50,310,110),fill='#332211')
            for row in rows:draw.text((row.bounds['x'],row.bounds['y']),row.text,fill='white')
            scene.save(frame.image_path)
        return frame
    def fake_ocr(path):return ocr(path)+(rows if visible[0] else [])
    r.world.capture=r.c.capture=fake_capture;r.world.ocr=r.c.ocr=fake_ocr
    return visible


@pytest.mark.parametrize('disappears_before_dispatch',[False,True])
def test_pending_loot_yields_to_guarded_escape_then_resumes_without_joining(tmp_path,disappears_before_dispatch):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();r.c.config['loot_enabled']=True;r.death()
            await r.loot(None)
            sources=deepcopy(r.c.loot.data['death_sources'])
            visible=attach_invitation(r)
            if disappears_before_dispatch:
                async def dismissed_elsewhere():visible[0]=False
                r.sage.hook=dismissed_elsewhere
            result=await r.choose('close_visible_ui')
            options=r.sage.calls[-1]['options']
            assert 'close_visible_ui' in options
            assert not any(name.startswith(('loot_','corpse_','attack_','join','accept')) for name in options)
            guards=[json.loads(row[0]) for row in r.store.connection.execute(
                "SELECT payload_json FROM events WHERE event_type='dispatch_guard_checked'")]
            guard=next(item for item in guards if item['chosen']=='close_visible_ui')
            assert guard['approved']==(not disappears_before_dispatch)
            assert r.physical_keys().count(r.controls['escape']['keycode'])==(0 if disappears_before_dispatch else 1)
            r.sage.hook=None;visible[0]=False
            if not disappears_before_dispatch:
                await r.choose('world_normal_confirmed')
                assert 'close_visible_ui' not in r.sage.calls[-1]['options']
                assert not r.c.hunt.input_effect_unverified
            else:assert result.status=='dispatch_guard_rejected'
            await r.choose('loot_remaining')
            assert 'loot_remaining' in r.sage.calls[-1]['options']
            assert r.c.loot.pending and r.c.loot.data['death_sources']==sources
            assert r.c.loot.data['attempts']==0 and r.c.loot.data['confirmations']==0
            assert r.c.loot.data.get('outcome')!='verified_looted'
            assert not r.casts() and not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())
