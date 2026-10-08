"""Late loot verdicts cannot outlive their bound corpse-click evidence.

Uses the existing fake runner components; the scripted positive answers are
contract inputs, not claims about real-world perception or loot success.
"""
import asyncio
from pathlib import Path

import pytest

from sage_wow.agent.cycle import ActionCandidate
import sage_wow.agent.grind_loot as loot
from test_grind_loot_integration import LootRig


@pytest.mark.parametrize('change', ['source_hash', 'input_generation', 'obligation', 'epoch'])
def test_second_positive_answer_rechecks_click_authority_after_sage_await(tmp_path, monkeypatch, change):
    monkeypatch.setattr(loot, '_corpse_choices', lambda frame, layout=None: [
        ActionCandidate('corpse_select_0', 'Offline corpse point outside HUD',
                        {'type': 'click', 'image_x': 450, 'image_y': 260})])

    async def exercise():
        rig = LootRig(tmp_path)
        rig.backend.mouse_button = lambda *args: rig.backend.events.append(('mouse', *args))
        try:
            await rig.start()
            rig.death()
            await rig.loot('corpse_select_0')
            assert rig.c.loot.data['attempts'] == 0
            await rig.loot('loot_verified')
            assert rig.c.loot.pending and rig.c.loot.data['confirmations'] == 1
            clicks = len([event for event in rig.backend.events if event[0] == 'mouse'])

            async def invalidate_after_request():
                authority = rig.c.loot.data['click_loot_authority']
                if change == 'source_hash':
                    path = Path(authority['image_path'])
                    path.write_bytes(path.read_bytes() + b'changed after request')
                elif change == 'input_generation':
                    rig.c.cycle._input_generation += 1
                elif change == 'obligation':
                    rig.c.loot.data['death_sources'].append({'identity': 'new-unverified-corpse'})
                else:
                    rig.c.cycle.invalidate('offline focus epoch retired')

            rig.sage.hook = invalidate_after_request
            await rig.loot('loot_verified')
            assert 'loot_verified' in rig.sage.calls[-1]['options']
            assert rig.c.loot.pending
            assert rig.c.loot.data['confirmations'] == 0
            assert rig.c.loot.data.get('outcome') != 'verified_looted'
            assert rig.c.loot.data['attempts'] == 0
            assert len([event for event in rig.backend.events if event[0] == 'mouse']) == clicks
        finally:
            await rig.close()

    asyncio.run(exercise())
