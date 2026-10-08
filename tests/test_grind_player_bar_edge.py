"""Text-covered player bars: retained pixels and full offline travel dispatch."""
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from sage_wow.agent.grind_observation import hud_continuity
from sage_wow.agent.grind_only import same_patch
from sage_wow.agent.grind_perception import read_current_bars
from sage_wow.agent.grind_resources import hud_resources
from sage_wow.agent.scene import _green_bar_measure, _green_bar_edge_measure
from sage_wow.agent.ui_layout import UILayoutError
from test_grind_travel_acceptance import TravelRig

FIXTURES=Path(__file__).parent/'fixtures/player_health_text'
BOX=(10,30,255,58)


def numbered(value=62,total=62,*,size=(245,28)):
    im=Image.new('RGB',size,'#151515');draw=ImageDraw.Draw(im)
    end=round(size[0]*value/total)
    if end:draw.rectangle((0,0,end-1,size[1]-1),fill='#78ce20')
    text=f'{value} / {total}';font=ImageFont.load_default(size=round(size[1]*.78))
    b=draw.textbbox((0,0),text,font=font,stroke_width=1)
    draw.text(((size[0]-(b[2]-b[0]))/2-b[0],(size[1]-(b[3]-b[1]))/2-b[1]),
              text,font=font,fill='white',stroke_fill='black',stroke_width=1)
    return im


def install(r,bar):
    regions=r.c.profile.values['calibration']['ui_layout']['regions']
    regions['player_frame']=[0,0,300,90];regions['player_health']=list(BOX)
    state={'bar':bar};capture=r.c.capture
    def rendered():
        frame=capture()
        if r.hud:
            with Image.open(frame.image_path) as im:
                im.paste(state['bar'],BOX[:2]);im.save(frame.image_path)
        return frame
    r.c.capture=r.world.capture=rendered
    return state


def test_retained_numeric_overlay_has_independent_edge_support():
    record=json.loads((FIXTURES/'provenance.json').read_text())
    assert hashlib.sha256((FIXTURES/'full.png').read_bytes()).hexdigest()==record['fixture_sha256']
    with Image.open(FIXTURES/'full.png') as im:
        assert _green_bar_measure(im)==(.939,.771)
        value,confidence,evidence=_green_bar_edge_measure(im)
    assert value==1 and confidence>=.8
    assert evidence['upper_edge']==evidence['lower_edge']==245
    assert evidence['band_agreement']==[1,1]


@pytest.mark.parametrize('value',[62,61,38,19,18])
def test_geometric_boundary_measures_varied_fill_without_reading_digits(value):
    estimate,confidence,_=_green_bar_edge_measure(numbered(value))
    assert estimate==pytest.approx(value/62,abs=.003) and confidence>=.8


@pytest.mark.parametrize('kind',['black','scattered','off_axis','diagonal','missing_top',
    'broad_occlusion','detached_island','tiny_geometry'])
def test_alternate_cannot_upgrade_unsupported_bar_geometry(kind):
    im=Image.new('RGB',(245,28),'#101010');draw=ImageDraw.Draw(im)
    if kind=='scattered':
        for x in range(0,245,7):draw.rectangle((x,0,x,27),fill='#40dd20')
    elif kind=='off_axis':draw.rectangle((20,0,200,27),fill='#40dd20')
    elif kind=='diagonal':
        for y in range(28):draw.line((0,y,80+y*4,y),fill='#40dd20')
    elif kind=='missing_top':draw.rectangle((0,8,244,27),fill='#40dd20')
    elif kind=='broad_occlusion':
        draw.rectangle((0,0,244,27),fill='#40dd20');draw.rectangle((20,6,220,21),fill='white')
    elif kind=='detached_island':
        draw.rectangle((0,0,99,27),fill='#40dd20');draw.rectangle((180,12,180,13),fill='#40dd20')
    elif kind=='tiny_geometry':im=im.resize((245,6))
    value,confidence,_=_green_bar_edge_measure(im)
    assert value is None and confidence<.8


@pytest.mark.parametrize('source',['retained','61','62'])
def test_text_bar_allows_complete_hide_restore_and_guarded_travel(tmp_path,source):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            bar=Image.open(FIXTURES/'full.png').convert('RGB') if source=='retained' else numbered(int(source))
            install(r,bar);await r.travel();await r.action('probe_forward')
            t=r.c.observation_transaction
            assert t['status']=='restored_verified' and not t['restoration_needed']
            assert r.controls['forward']['keycode'] in r.physical_keys() and r.hud
            if source=='retained':
                hud=hud_resources(r.c,r.world.capture())
                assert hud['player_health']==1 and hud['health_confidence']>=.8
                assert hud['player_health_measurement']['raw_confidence']==.771
                assert hud['player_health_measurement']['accepted']
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('when',['restore','dispatch'])
def test_changed_resources_still_prevent_motion_after_text_bar_read(tmp_path,when):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            state=install(r,Image.open(FIXTURES/'full.png').convert('RGB'));await r.travel()
            if when=='restore':r.after_hidden=lambda:state.update(bar=numbered(18))
            else:
                async def changed():state['bar']=numbered(18)
                r.sage.hook=changed
            r.sage.answers.append('probe_forward')
            result=await r.c.process(r.world.capture())
            assert result.status==('grind_reobserve' if when=='restore' else 'dispatch_guard_rejected')
            assert r.controls['forward']['keycode'] not in r.physical_keys()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('with_text',[True,False])
def test_small_real_change_crossing_survival_boundary_still_vetoes(tmp_path,with_text):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            def bar(value):
                if with_text:return numbered(value)
                im=Image.new('RGB',(245,28),'#151515')
                ImageDraw.Draw(im).rectangle((0,0,round(245*value/62)-1,27),fill='#78ce20')
                return im
            state=install(r,bar(19));before=r.world.capture()
            state['bar']=bar(18);after=r.world.capture()
            old=await r.c.target_proposal(before);new=await r.c.target_proposal(after)
            evidence=hud_continuity(r.c,before,after,old,new,await r.c.rows(after))
            # Changing numeric glyphs can independently fail the pixel guard.
            # Without that extra difference, only the survival boundary vetoes
            # the small, otherwise tolerated change in the measured fill.
            if not with_text:
                assert same_patch(before.image_path,after.image_path,BOX)
                assert evidence['predicates']['player_health']
            assert evidence['predicates']['player_health_confident']
            assert not evidence['predicates']['same_survival_band']
        finally:await r.close()
    asyncio.run(run())


def test_alternate_requires_explicit_matching_calibrated_geometry(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            install(r,Image.open(FIXTURES/'full.png').convert('RGB'));frame=r.world.capture()
            layout=r.c.profile.values['calibration']['ui_layout']
            with pytest.raises(UILayoutError):read_current_bars(replace(frame,width=501),ui_layout=layout)
            scene=read_current_bars(frame)
            assert 'alternate' not in scene.health_evidence
        finally:await r.close()
    asyncio.run(run())
