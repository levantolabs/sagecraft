"""Live-run regression: compact uncertainty must have a real Sage retry path.

Synthetic controller traces only; repository guards prohibit native I/O/network.
"""
import asyncio

import pytest

from test_grind_product_spec import Rig
from test_grind_travel_recovery import TravelClock, events


def test_compact_search_abstention_reasons_without_inventing_progress(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await r.start()
            r.world.name = ''
            r.c.config['move_seconds'] = .05
            for _ in range(3):
                result = await r.choose(None)
                assert result.no_input_abstention
                assert not r.physical_keys() and not r.casts()
                assert not r.c.hunt.progress_facts and not r.c.hunt.pending
            # Two unresolved acquisition answers open a different recovery
            # question. Its first request starts fast; its retry reasons.
            assert [x['reasoning_mode'] for x in events(r, 'sage_request_started')[-3:]] == ['off', 'auto', 'off']
            await r.choose('forward')
            assert events(r, 'sage_request_started')[-1]['reasoning_mode'] == 'auto'
            assert r.physical_keys() == [r.controls['forward']['keycode']]
            assert r.c.hunt.pending['purpose'] == 'search'
            assert not r.c.hunt.progress_facts
            await r.choose('motion_no_useful_effect')
            assert events(r, 'sage_request_started')[-1]['reasoning_mode'] == 'off'
            assert not r.c.hunt.pending and not r.c.hunt.progress_facts
        finally:
            await r.close()
    asyncio.run(run())


def test_level_observation_stays_fast_and_preserves_unresolved_question(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await r.start()
            await r.choose(None)
            r.c.level.last_attempt_at = 0
            await r.choose('player_level_1')
            assert events(r, 'sage_request_started')[-1]['reasoning_mode'] == 'off'
            await r.choose(None)
            assert events(r, 'sage_request_started')[-1]['reasoning_mode'] == 'auto'
            assert not r.physical_keys() and not r.casts()
            assert not r.c.hunt.progress_facts
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interruption', ['deadline', 'focus'])
def test_reasoned_answer_cannot_outlive_input_authority(tmp_path, monkeypatch, interruption):
    async def run():
        r = Rig(tmp_path)
        try:
            await r.start()
            await r.choose(None)
            clock = TravelClock(monkeypatch, r)
            async def invalidate():
                if interruption == 'deadline':
                    clock.offset = 13
                else:
                    r.c.pause_focus()
            r.sage.hook = invalidate
            result = await r.choose('attack_mob_level_1')
            assert events(r, 'sage_request_started')[-1]['reasoning_mode'] == 'auto'
            assert result.status != 'dispatched'
            assert not r.physical_keys() and not r.casts()
            assert not r.c.hunt.progress_facts and not r.c.hunt.pending
        finally:
            await r.close()
    asyncio.run(run())
