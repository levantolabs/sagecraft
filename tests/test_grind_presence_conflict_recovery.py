"""Offline replay of a disputed absence answer after sensing is exhausted."""
import asyncio

import httpx
import pytest

from test_grind_exact_band_observation import start_band
from test_grind_target_inspection_contract import ContractRig


@pytest.mark.parametrize('failure', ['absent_conflict', 'timeout', 'scope'])
def test_repeating_absence_policy_must_leave_exhausted_inspection(tmp_path, failure):
    async def run():
        r = ContractRig(tmp_path/failure)
        try:
            r.p.values['grind_only']['committed_combat'] = True
            await start_band(r)
            r.c.hunt.compact_stage = 'reinspect'
            if failure == 'absent_conflict':
                r.answers.update(selected_hud='absent', name='other_or_unknown',
                                 level='unknown', target_kind='unknown', life_state='unknown')
            elif failure == 'timeout':
                r.failure = httpx.ReadTimeout('offline observer unavailable')
            else:
                r.flaw = 'scope'
            assert (await r.observe_target()).status == 'grind_reobserve'
            assert (await r.observe_target()).status == 'grind_reobserve'
            episode = r.c.target_inspection_episode
            revision = r.c.hunt.search_revision

            # Reproduce the live policy's preferred disputed answer. A bounded
            # sensing failure must change its menu, not hope this policy changes.
            async def policy():
                options = r.sage.calls[-1]['options']
                r.sage.answers.append(next((name for name in
                    ('no_selected_frame', 'change_search_strategy', 'explore_visible')
                    if name in options), None))
            r.sage.hook = policy
            for _ in range(6):
                await r.choose()
                if r.c.hunt.phase == 'travel':
                    break
            assert r.c.hunt.phase == 'travel'
            assert r.c.target_inspection_episode is episode and episode['exhausted']
            assert len(r.wire) == 2 and not r.physical()
            assert r.c.hunt.search_revision == revision
            assert not r.c.hunt.attempt_absence and not r.c.hunt.credited_kills
            assert not any('attack_mob_level_2' in call['options'] for call in r.sage.calls[-2:])
            assert any(row['action'] == 'no_selected_frame'
                       for event in r.events('grind_compact_question')
                       for row in event['withheld'])
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('clear_first', [False, True])
def test_exhausted_inspection_still_accepts_a_later_actual_absence(tmp_path, clear_first):
    async def run():
        r = ContractRig(tmp_path/'absence')
        try:
            r.p.values['grind_only']['committed_combat'] = True
            await start_band(r)
            r.answers.update(selected_hud='absent', name='other_or_unknown',
                             level='unknown', target_kind='unknown', life_state='unknown')
            await r.observe_target()
            await r.observe_target()
            if clear_first:
                from test_grind_selected_task_exit import exhaust
                # This selection has never owned combat. Dispose it through the
                # existing accepted strategy and exhausted destination route.
                await r.choose('change_search_strategy')
                await r.choose('explore_visible')
                await exhaust(r)
                await r.choose('reject_selected_target')
                assert r.c.hunt.pending['family'] == 'clear'
            else:
                r.c.hunt.compact_stage = 'inspect'
            r.world.name = ''
            r.world.target_level = None
            result = await r.choose('target_cleared' if clear_first else 'no_selected_frame')
            assert result.status == 'dispatched'
            assert r.c.hunt.selected_presence is False
            assert r.c.hunt.compact_stage == 'acquire'
            assert not r.c.hunt.credited_kills
            assert len(r.wire) == 2
            if clear_first:
                assert r.c.hunt.phase == 'travel'
                assert r.c.hunt.strategy_required['selected_exit']['disposition']=='closed'
            else:
                physical_before = len(r.physical())
                await r.choose('target_enemy')
                assert len(r.physical()) > physical_before
        finally:
            await r.close()
    asyncio.run(run())
