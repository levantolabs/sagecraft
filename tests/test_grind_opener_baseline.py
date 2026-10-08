"""The automatic opener participates in the same linked cast diagnostics."""
from test_grind_product_spec import retain_legacy_no_effect
import asyncio
from copy import deepcopy

import pytest
from PIL import Image, ImageDraw

from test_grind_acquisition_opening import enable, acquire, process, smites
from test_grind_product_spec import Rig


def mana_pixels(r):
    r.c.config['player_mana_box'] = [40, 43, 140, 49]
    r.world.mana = 1.
    original = r.world.capture
    def capture():
        frame = original()
        with Image.open(frame.image_path) as source:
            image = source.convert('RGB')
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 43, 139, 48), fill='black')
        draw.rectangle((40, 43, 39 + round(100*r.world.mana), 48), fill='blue')
        image.save(frame.image_path)
        return frame
    r.world.capture = r.c.capture = capture


def test_opener_and_regular_no_effect_reach_correction_with_two_casts(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            enable(r);mana_pixels(r);await r.start();await acquire(r)
            r.world.mana = .4
            original = r.c.guard
            async def guard(*args, **kwargs):
                check = await original(*args, **kwargs)
                async def fresh(current):
                    r.world.mana = 1.
                    return await check(current)
                return fresh
            r.c.guard = guard
            await process(r)
            first = deepcopy(r.c.hunt.pending)
            assert first['measurement']['frame_id'] == first['source_frame_id']
            assert first['measurement']['combat_hud']['frame_id'] == first['source_frame_id']
            assert first['measurement']['combat_hud']['player_mana'] == 1.
            await retain_legacy_no_effect(r)
            assert r.c.hunt.cast_review['unchanged_resource_casts'] == 1
            await r.choose('attack_mob_level_1')
            second = deepcopy(r.c.hunt.pending)
            await retain_legacy_no_effect(r)
            review = r.c.hunt.cast_review
            assert review['unchanged_resource_casts'] == 2
            assert review['reviewed_receipts'] == [first['receipt']['receipt_id'], second['receipt']['receipt_id']]
            await r.choose('forward')
            assert 'approach_for_range_check' not in r.sage.calls[-1]['options']
            assert r.c.hunt.pending['purpose'] == 'cast_correction'
            assert len(smites(r)) == 2 and r.c.hunt.pending['family'] == 'motion'
            assert r.c.hunt.cast_review['reviewed_receipts'] == [first['receipt']['receipt_id'], second['receipt']['receipt_id']]
            assert not r.c.hunt.credited_kills and not r.c.hunt.cast_review['damage_known']
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['pause', 'generation', 'pending', 'expiry'])
def test_fresh_baseline_preparation_cannot_outlive_opener_authority(tmp_path, monkeypatch, change):
    async def run():
        import sage_wow.agent.grind_search as search
        r = Rig(tmp_path)
        try:
            enable(r);mana_pixels(r);await r.start();await acquire(r)
            original = search.measure
            loop = asyncio.get_running_loop(); token = r.c.target_opener
            def invalidate():
                if change == 'pause':r.c.pause_focus()
                elif change == 'generation':r.c.cycle._input_generation += 1
                elif change == 'pending':r.c.hunt.pending = None
                else:token['deadline'] = 0
            def changed(*args):
                result = original(*args)
                loop.call_soon_threadsafe(invalidate)
                return result
            monkeypatch.setattr(search, 'measure', changed)
            r.sage.answers.append(None)
            await process(r)
            assert not smites(r) and r.c.target_opener is None
        finally:
            await r.close()
    asyncio.run(run())
