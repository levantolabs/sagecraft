"""Live-run regression: present recovery options without requiring past outcomes."""
import asyncio

import pytest

from test_grind_loot_integration import LootRig
from test_grind_travel_acceptance import TravelRig
from test_grind_travel_recovery import TravelClock, events


@pytest.mark.parametrize('old_presence', [True, None, False])
def test_missing_postcast_selection_can_inspect_corpse_without_credit(tmp_path, old_presence):
    async def run():
        r = LootRig(tmp_path)
        r.c.config['loot_enabled'] = True
        try:
            await r.start()
            await r.choose('attack_mob_level_1')
            receipt = r.c.hunt.pending['receipt']['receipt_id']
            cast_keys = list(r.physical_keys())
            r.world.name = ''
            await r.choose('cannot_assess')
            await r.choose('cannot_assess')
            r.c.hunt.selected_presence = old_presence
            await r.choose(None)
            call = r.sage.calls[-1]
            assert 'CURRENT world view' in call['prompt']
            assert 'Find the actual portrait' not in call['prompt']
            assert 'inspect_recent_corpse' in call['options']
            assert r.physical_keys() == cast_keys and len(r.casts()) == 1
            assert not r.c.hunt.credited_kills and not r.c.hunt.progress_facts
            assert r.c.hunt.recent_combat['receipt']['receipt_id'] == receipt
            await r.choose('inspect_recent_corpse')
            request = r.c.hunt.loot_request
            assert request['suspected'] and not request['death_observed'] and not request['credit_known']
            assert request['receipt_id'] == receipt
            assert r.physical_keys() == cast_keys and len(r.casts()) == 1
            assert not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('late', [False, True])
def test_recovery_retaining_travel_phase_uses_its_question_policy_and_original_deadline(tmp_path, monkeypatch, late):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel()
            r.c.request_recovery('offline unresolved travel')
            assert r.c.hunt.phase == 'travel'
            await r.choose(None)
            assert events(r, 'sage_request_started')[-1]['reasoning_mode'] == 'off'
            assert not r.physical_keys()
            if late:
                clock = TravelClock(monkeypatch, r)
                async def expire():
                    clock.offset = 13
                r.sage.hook = expire
            result = await r.choose('target_enemy')
            assert events(r, 'sage_request_started')[-1]['reasoning_mode'] == 'auto'
            if late:
                assert result.status == 'grind_timing_expired'
                assert not r.physical_keys() and not r.c.hunt.pending
            else:
                assert r.physical_keys() == [r.controls['target_enemy']['keycode']]
                assert r.c.hunt.pending['family'] == 'target'
            assert not r.casts() and not r.c.hunt.credited_kills
            assert not r.c.hunt.progress_facts
        finally:
            await r.close()
    asyncio.run(run())
