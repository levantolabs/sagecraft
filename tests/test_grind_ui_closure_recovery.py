"""Fake menu/Combat Log recovery: Escape cannot reopen a clear world."""
import asyncio
from PIL import Image, ImageDraw

from sage_wow.perception.ocr import TextObservation
from sage_wow.agent.grind_search import ui_evidence
from test_grind_product_spec import Rig


def attach_menu(r):
    visible=[True]
    original_capture=r.world.capture
    original_ocr=r.world.ocr
    def capture():
        frame=original_capture()
        with Image.open(frame.image_path) as source:image=source.copy()
        draw=ImageDraw.Draw(image)
        draw.text((10,220),'My actions',fill='white')
        if visible[0]:draw.text((180,60),'Game Menu',fill='white')
        image.save(frame.image_path)
        return frame
    def ocr(path):
        rows=original_ocr(path)
        rows.append(TextObservation('My actions',.99,{'x':10,'y':220,'width':90,'height':15}))
        if visible[0]:rows.append(TextObservation('Game Menu',.99,{'x':180,'y':60,'width':90,'height':15}))
        return rows
    r.world.capture=capture;r.world.ocr=ocr;r.c.capture=capture;r.c.ocr=ocr
    return visible


def test_clean_world_after_menu_closure_has_no_escape_candidate(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start()
            r.c.config['committed_combat']=True
            visible=attach_menu(r)
            r.c.require_world=True
            await r.choose('close_visible_ui')
            count=r.physical_keys().count(r.controls['escape']['keycode'])
            assert count==1
            visible[0]=False
            await r.choose('world_normal_confirmed')
            options=r.sage.calls[-1]['options']
            assert 'close_visible_ui' not in options and 'blocking_ui_remains' not in options
            assert r.physical_keys().count(r.controls['escape']['keycode'])==count
            assert not r.c.require_world and not r.c.hunt.input_effect_unverified
        finally:await r.close()
    asyncio.run(run())


def test_menu_disappearing_during_sage_decision_rejects_escape(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start()
            r.c.config['committed_combat']=True
            visible=attach_menu(r);r.c.require_world=True
            async def close_before_dispatch():visible[0]=False
            r.sage.hook=close_before_dispatch
            result=await r.choose('close_visible_ui')
            assert result.status=='dispatch_guard_rejected'
            assert r.controls['escape']['keycode'] not in r.physical_keys()
            assert not r.c.stopped
        finally:await r.close()
    asyncio.run(run())


def test_passive_combat_log_tab_is_not_blocking_ui(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            visible=attach_menu(r);visible[0]=False
            source=r.world.capture()
            evidence=ui_evidence(r.c.profile,source,r.world.ocr(source.image_path),{})
            assert not evidence['positive'] and not evidence['cues']
            visible[0]=True
            source=r.world.capture()
            assert ui_evidence(r.c.profile,source,r.world.ocr(source.image_path),{})['positive']
        finally:await r.close()
    asyncio.run(run())
