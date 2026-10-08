"""Retained dead-bar pixels exercise corpse confirmation and guarded fake F.

The two 198x23 PNGs preserve decoded live-run pixels without unrelated screen
content. Provenance records their original archived frame hashes and bounds.
All capture, OCR, Sage answers and input here use the existing offline rig.
"""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from sage_wow.agent.grind_only import same_patch
from test_grind_loot_integration import LootRig


FIXTURES = Path(__file__).parent / 'fixtures' / 'loot_dead_bar'
HEALTH_BOX = [240, 125, 438, 148]


def retained_bars(rig):
    """Paste exact archived health pixels into an otherwise synthetic world."""
    state = {'image': 'before', 'change': None}
    original = rig.world.capture
    rig.c.profile.values['calibration']['ui_layout']['regions']['target_health'] = HEALTH_BOX

    def capture():
        frame = original()
        with Image.open(frame.image_path) as raw:
            image = raw.convert('RGB')
        with Image.open(FIXTURES / (state['image'] + '.png')) as bar:
            image.paste(bar, tuple(HEALTH_BOX[:2]))
        draw = ImageDraw.Draw(image)
        if state['change'] == 'living':
            # A 1% live sliver must veto F even if broad overlay pixels match.
            draw.rectangle((240, 125, 241, 147), fill=(0, 200, 0))
        elif state['change'] == 'bar_changed':
            draw.rectangle((240, 125, 437, 147), fill=(170, 20, 20))
        image.save(frame.image_path)
        return frame

    rig.world.capture = rig.c.capture = capture
    return state


def test_retained_corpse_noise_allows_f_offer_and_fresh_guard(tmp_path):
    provenance = json.loads((FIXTURES / 'provenance.json').read_text())
    for record in provenance['records']:
        assert hashlib.sha256((FIXTURES / record['fixture']).read_bytes()).hexdigest() == record['fixture_sha256']
    before, after = (FIXTURES / name for name in ('before.png', 'after.png'))
    assert same_patch(before, after, (0, 0, 198, 23))
    assert not same_patch(before, after, (0, 0, 198, 23), badge=True)

    async def exercise():
        rig = LootRig(tmp_path)
        try:
            await rig.start()
            rig.death()
            state = retained_bars(rig)
            await rig.loot('loot_remaining')
            await rig.loot('loot_selected_corpse')
            assert 'interact_target' not in rig.sage.calls[-1]['options']
            state['image'] = 'after'

            async def different_fresh_pixels():
                state['image'] = 'before'

            rig.sage.hook = different_fresh_pixels
            result = await rig.loot('interact_target')
            assert result.status == 'dispatched', result.detail
            assert rig.physical_keys() == [3]
            assert rig.c.loot.data['attempts'] == 1
            assert rig.c.loot.pending  # Input is still not loot-success evidence.
        finally:
            await rig.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('change', ['living', 'bar_changed', 'different_name'])
def test_real_dead_bar_tolerance_keeps_fresh_selection_vetoes(tmp_path, change):
    async def exercise():
        rig = LootRig(tmp_path)
        try:
            await rig.start()
            rig.death()
            state = retained_bars(rig)
            await rig.loot('loot_remaining')
            await rig.loot('loot_selected_corpse')
            state['image'] = 'after'

            async def changed_after_choice():
                state['change'] = change
                if change == 'different_name':
                    rig.world.name = 'Different Wolf'

            rig.sage.hook = changed_after_choice
            result = await rig.loot('interact_target')
            assert 'interact_target' in rig.sage.calls[-1]['options']
            assert result.status == 'dispatch_guard_rejected'
            assert rig.physical_keys() == []
            assert rig.c.loot.data['attempts'] == 0
            assert rig.c.loot.pending
            guard = json.loads(rig.store.connection.execute(
                "SELECT payload_json FROM events WHERE event_type='dispatch_guard_checked' "
                "ORDER BY rowid DESC LIMIT 1").fetchone()[0])['evidence']['predicates']
            if change == 'living':
                assert guard['corpse_health_unchanged'] and guard['selected_corpse_unchanged']
                assert not guard['no_positive_target_health']
            elif change == 'different_name':
                assert not guard['selected_identity_matches']
            else:
                assert not guard['corpse_health_unchanged']
        finally:
            await rig.close()

    asyncio.run(exercise())
