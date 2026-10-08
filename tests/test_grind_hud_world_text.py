"""World labels beneath calibrated HUD rectangles do not imply visible HUD."""
import asyncio
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from sage_wow.perception.ocr import TextObservation
from test_grind_travel_acceptance import TravelRig
from test_grind_hud_no_effect import failed_hide, ignore_chords


def world_text(r, text, box):
    capture,ocr=r.c.capture,r.c.ocr
    def painted():
        frame=capture()
        if not r.hud:
            with Image.open(frame.image_path) as im:
                ImageDraw.Draw(im).text(box[:2],text,fill='#55dd33')
                im.save(frame.image_path)
        return frame
    def rows(path):
        result=ocr(path)
        state=r.frames.get(str(Path(path)))
        if state and not state['hud']:
            x,y,w,h=box
            result=result+[TextObservation(text,.99,{'x':x,'y':y,'width':w,'height':h})]
        return result
    r.c.capture=painted;r.c.ocr=rows


@pytest.mark.parametrize('box',[(405,10,85,12),(40,5,90,12)])
def test_revealed_unrelated_world_label_allows_owned_hide_restore(tmp_path,box):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();world_text(r,'Nearby Vendor',box)
            await r.action('probe_forward')
            t=r.c.observation_transaction
            assert t['status']=='restored_verified' and not t['restoration_needed']
            assert t['hide_samples'][-1]['evidence']['region_text']==['Nearby Vendor']
            assert len(t['hidden_witnesses'])==2 and r.toggle_count==2 and r.hud
        finally:await r.close()
    asyncio.run(run())


def test_revealed_world_label_allows_delayed_owned_correction(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();key=ignore_chords(r);t=await failed_hide(r)
            r.backend.key=key;r.hud=False;world_text(r,'Unknown Creature',(405,10,85,12))
            result=await r.c.process(r.c.capture())
            assert result.status=='grind_reobserve' and t['status']=='restoration_reconciled'
            assert t['correction_receipt']['completed'] and not t['restoration_needed']
            assert r.hud and r.toggle_count==2 and not r.sage.calls and not r.casts()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('text',['Test Player','Test Wrong'])
def test_remaining_own_identity_blocks_hide_even_when_regions_change(tmp_path,text):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();world_text(r,text,(40,5,90,12))
            t=await failed_hide(r)
            assert t['hide_samples'][-1]['evidence']['remaining_player_identity']
            assert min(t['hide_samples'][-1]['evidence']['hud_difference'].values())>2
            assert not t.get('restore_receipt') and not r.sage.calls and not r.casts()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('case',['remaining_minimap','loading'])
def test_unrelated_label_does_not_override_structure_or_loading_guard(tmp_path,case):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel()
            world_text(r,'Loading' if case=='loading' else 'Nearby Vendor',(405,10,85,12))
            if case=='remaining_minimap':
                capture=r.c.capture
                def partial():
                    frame=capture()
                    if not r.hud:
                        source=r.c.observation_transaction['source_frame']['image_path']
                        with Image.open(source) as before,Image.open(frame.image_path) as after:
                            after.paste(before.crop((400,0,500,80)),(400,0));after.save(frame.image_path)
                    return frame
                r.c.capture=partial
            t=await failed_hide(r)
            assert not t.get('restore_receipt') and not r.sage.calls and not r.casts()
            evidence=t['hide_samples'][-1]['evidence']
            if case=='loading':assert evidence['blocking_ui']
            else:assert evidence['hud_difference']['minimap']==0
        finally:await r.close()
    asyncio.run(run())
