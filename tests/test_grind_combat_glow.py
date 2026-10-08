"""Retained live-run HUD crops: combat glow is mutable, numeral glyphs are not."""
import asyncio
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import pytest
from sage_wow.agent.grind_only import same_patch,same_target_badge
from sage_wow.agent.grind_perception import same_badge_numeral
from sage_wow.models import Frame
from sage_wow.perception.ocr import TextObservation
from test_grind_only import setup,cleanup

FIXTURES=Path(__file__).parent/'fixtures'
BADGE=[280,77,322,119];PLAYER=[0,128,32,177]
CAL={'image_width':335,'image_height':177,'badge_box':BADGE,'inset_pixels':5,
     'mode':'opaque_interior_ellipse'}


def test_retained_cast_adds_glow_but_preserves_both_numeral_silhouettes():
    before=FIXTURES/'burst-combat-glow-before.png';after=FIXTURES/'burst-combat-glow-after.png'
    assert not same_patch(before,after,[10,18,209,42])
    assert same_target_badge(before,after,BADGE,CAL)
    assert same_target_badge(before,after,BADGE,CAL,combat_glow=True)
    assert not same_patch(before,after,PLAYER,badge=True)
    assert same_badge_numeral(before,after,PLAYER,{**CAL,'badge_box':PLAYER},white=True)


@pytest.mark.parametrize('committed,change',[(False,None),(True,None),(True,'target_digit'),
                                          (True,'player_digit'),(True,'different_name')])
def test_actual_dispatch_guard_glow_continuity_is_scoped_and_changed_identity_vetoes(tmp_path,committed,change,monkeypatch):
    monkeypatch.setenv('SAGE_WOW_DISABLE_LIVE_INPUT','1');monkeypatch.setenv('SAGE_WOW_DISABLE_LIVE_CAPTURE','1')
    async def scenario():
        c,_,_,backend,store,executor=setup(tmp_path)
        try:
            layout=c.profile.values['calibration']['ui_layout'];layout.update(image_width=335,image_height=177)
            layout['regions']={'target_overlay':[0,0,335,128],'target_frame':[0,0,335,128],
                'target_health':[10,43,215,72],'player_frame':[0,128,32,177],'player_health':[0,160,12,166]}
            c.config.update(target_level_box=BADGE,player_level_box=PLAYER,target_name_box=[10,18,209,42],
                target_badge_continuity=CAL,committed_combat=committed)
            before=FIXTURES/'burst-combat-glow-before.png';after=tmp_path/'after.png'
            Image.open(FIXTURES/'burst-combat-glow-after.png').save(after)
            if change in {'target_digit','player_digit'}:
                with Image.open(after) as im:
                    draw=ImageDraw.Draw(im)
                    if change=='target_digit':
                        draw.rectangle((287,87,310,109),fill='#101010')
                        draw.text((300,99),'2',font=ImageFont.load_default(size=22),anchor='mm',fill='#ffe090')
                    else:
                        draw.rectangle((4,136,28,171),fill='#101010')
                        draw.text((16,152),'2',font=ImageFont.load_default(size=22),anchor='mm',fill='white')
                    im.save(after)
            def capture():return Frame.create('offline-window-7',335,177,image_path=str(after))
            def rows(path):
                name='Rabbit' if change=='different_name' and Path(path)==after else 'Ragged Young Wolf'
                return [TextObservation(name,.99,{'x':10,'y':18,'width':199,'height':24}),
                    TextObservation('1',.99,{'x':296,'y':89,'width':8,'height':20})]
            c.capture=capture;c.ocr=rows
            source=Frame.create('offline-window-7',335,177,image_path=str(before))
            expected=await c.target_proposal(source);guard=await c.guard(source,expected,exact_level=1)
            result=await guard(source)
            assert result.approved==(committed and change is None)
            assert not backend.events
            if result.approved:
                provenance=result.evidence['combat_glow_continuity']
                assert provenance['name_fallback_used']
                assert not provenance['badge_glyph_fallback_used']
                assert provenance['player_glyph_fallback_used']
        finally:await cleanup(executor,store)
    asyncio.run(scenario())


def test_retained_intercast_pair_uses_fresh_name_and_current_health_not_glowing_border(monkeypatch):
    from types import SimpleNamespace
    from sage_wow.agent import grind_perception
    from sage_wow.agent.scene import Scene
    before=Frame.create('offline',335,177,image_path=str(FIXTURES/'burst-combat-glow-before.png'))
    after=Frame.create('offline',335,177,image_path=str(FIXTURES/'burst-combat-glow-after.png'))
    captures=iter([before,after]);events=[]
    controller=SimpleNamespace(current=lambda:True,capture=lambda:next(captures),archive=None,
        target_observation=None,target_inspection_episode=None,ocr=lambda path:[],revision=1,
        cycle=SimpleNamespace(session_epoch='episode',input_generation=0),
        hunt=SimpleNamespace(selection_revision=1,target_continuity=1,
            approach={'history_key':'offline-retained-owner'},encounter=1),
        config={'target_level_box':BADGE,'target_name_box':[10,18,209,42],
            'target_badge_continuity':CAL,'committed_combat':True},
        profile=SimpleNamespace(values={'calibration':{}}),event=lambda kind,payload:events.append(payload))
    def hud(frame,**kwargs):return Scene(frame.frame_id,target_name='Ragged Young Wolf',target_alive=True,
        health=.996,health_confidence=1,target_health=1 if frame==before else .692,target_health_confidence=1)
    monkeypatch.setattr(grind_perception,'read_hud_scene',hud)
    async def scenario():
        first=await grind_perception.observe_burst(controller,{'expected_target_name':'Ragged Young Wolf'},0)
        controller.cycle.input_generation=5
        second=await grind_perception.observe_burst(controller,{'expected_target_name':'Ragged Young Wolf'},1)
        return first,second
    first,second=asyncio.run(scenario())
    assert first['continue'] and second['continue']
    assert second['target_health_pixel_estimate']==.692
    assert second['identity_continuity']['same_fresh_name']
    assert not second['identity_continuity']['name_patch']
