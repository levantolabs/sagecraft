"""A translucent target nameplate can animate without changing selection."""
import asyncio
from dataclasses import replace

from PIL import Image,ImageDraw
import pytest
from sage_wow.perception.ocr import TextObservation

from test_grind_product_spec import Rig


@pytest.mark.parametrize('change', ['background','different_name','different_level','missing_name',
    'low_confidence','badge','player_badge','unconfigured','input_generation','source_mutation',
    'blocking_ui','focus'])
def test_clear_dispatch_uses_current_name_badge_but_preserves_vetoes(tmp_path,change):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.world.target_level=1
            c=r.c;badge=c.config['target_level_box'];name_box=[205,95,370,120]
            c.config.update(committed_combat=True,target_name_box=name_box,
                target_badge_continuity={'image_width':500,'image_height':300,
                    'badge_box':badge,'inset_pixels':3,'mode':'opaque_interior_ellipse'})
            if change=='unconfigured':c.config.pop('target_badge_continuity')
            original_capture=r.world.capture;original_ocr=r.world.ocr;state={'fresh':False,'source':None}
            def capture():
                frame=original_capture()
                if state['fresh']:
                    with Image.open(frame.image_path) as im:
                        draw=ImageDraw.Draw(im)
                        # Moving background, outside the actual name glyphs.
                        draw.rectangle((354,96,369,119),fill='white')
                        if change=='badge':draw.rectangle(tuple(badge),fill='white')
                        if change=='player_badge':draw.rectangle(tuple(c.config['player_level_box']),fill='white')
                        im.save(frame.image_path)
                else:state['source']=frame.image_path
                return frame
            def rows(path):
                observed=original_ocr(path)
                if state['fresh'] and change=='low_confidence':
                    observed=[replace(row,confidence=.8) if row.text=='Young Wolf' else row for row in observed]
                if state['fresh'] and change=='blocking_ui':
                    observed.append(TextObservation('Game Menu',.99,{'x':180,'y':60,'width':90,'height':15}))
                return observed
            r.world.capture=c.capture=capture;c.ocr=rows
            async def next_frame():
                state['fresh']=True
                if change=='different_name':r.world.name='Different Wolf'
                elif change=='different_level':r.world.target_level=2
                elif change=='missing_name':r.world.name=''
                elif change=='input_generation':c.cycle.input_generation+=1
                elif change=='focus':
                    gate=r.executor.inspect_gate()
                    r.executor.inspect_gate=lambda:replace(gate,foreground=False)
                elif change=='source_mutation':
                    with Image.open(state['source']) as im:
                        ImageDraw.Draw(im).point((0,0),fill='white');im.save(state['source'])
            r.sage.hook=next_frame
            result=await r.choose('reject_selected_target')
            approved=change=='background'
            assert (result.status=='dispatched')==approved
            assert r.physical_keys()==([r.controls['escape']['keycode']] if approved else [])
            assert not r.casts() and not c.hunt.credited_kills and not c.hunt.progress_facts
            if approved:
                assert c.hunt.pending['family']=='clear'
                checks=[e['payload'] for e in r.store.recent(40) if e['event_type']=='dispatch_guard_checked']
                assert checks[-1]['evidence']['clear_name_continuity']['name_fallback_used']
                r.sage.hook=None;r.world.name=''
                await r.choose('target_cleared')
                assert c.hunt.pending is None and c.hunt.selected_presence is False
        finally:await r.close()
    asyncio.run(run())
