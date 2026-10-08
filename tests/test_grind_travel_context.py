"""Actual controller facts, destination binding and bounded travel rendering."""
import asyncio
from copy import deepcopy

import pytest

from test_grind_travel_acceptance import TravelRig
from test_grind_travel_recovery import events
from test_active_attacker_recovery import install_hud


def latest(r):
    return events(r, 'grind_travel_question_context')[-1]


@pytest.mark.parametrize('clean', [False, True])
@pytest.mark.parametrize('destination', [[0., 10.], [20., 10.]])
def test_current_goal_projection_does_not_rewrite_original_movement(tmp_path, clean, destination):
    async def run():
        r = TravelRig(tmp_path, clean=clean)
        try:
            await r.travel()
            await r.action('probe_forward'); r.position = [11., 10.]
            await r.action('advance_forward'); r.position = [12., 10.]
            await r.choose(None)
            h = r.c.hunt
            original = deepcopy(h.last_completed_action)
            assert original['progress'] == pytest.approx(1.)
            area = {**h.plan['area'], 'coordinate': destination, 'label': 'New label is not geometric evidence'}
            h.choose(area, r.world.capture(), 'new-choice', 1)
            await r.choose(None)
            projection = latest(r)
            expected = '-1.00' if destination[0] == 0 else '+1.00'
            assert f'{expected} ±0.14' in projection['core'][5]
            assert 'Historical action evaluated against current destination' in projection['core'][5]
            # Verified HUD transactions may transport current search scope;
            # the original endpoints, receipt generations and outcome do not change.
            transported = {'search_generation', 'search_observation_transaction_id'}
            assert {k:v for k,v in h.last_completed_action.items() if k not in transported} == {
                k:v for k,v in original.items() if k not in transported}
            assert 'Direction still current: yes' in projection['context']
            assert not h.progress_facts and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['zone', 'unknown_pair'])
def test_unusable_historical_pair_never_supplies_current_goal_progress(tmp_path, change):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); await r.action('probe_forward')
            r.position = [11., 10.]; await r.choose(None)
            h = r.c.hunt
            if change == 'zone':
                h.last_completed_action['zone_before'] = ('somewhere else',)
            else:
                h.last_completed_action['attributable_pair'] = False
            h.last_completed_action['narrative'] = 'UNSUPPORTED cached closer claim'
            await r.choose(None)
            projection = latest(r)
            assert 'Historical destination progress: unavailable' in projection['context']
            assert 'UNSUPPORTED' not in projection['context']
            assert projection['audit']['last_action']['narrative'].startswith('UNSUPPORTED')
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('clean', [False, True])
def test_retry_retains_goal_policy_currentness_and_real_continuation(tmp_path, clean):
    async def run():
        r = TravelRig(tmp_path, clean=clean)
        try:
            await r.travel()
            install_hud(r, target_health=None)
            h = r.c.hunt
            h.hunting_progression_by_player_level = {1: {'primary_targets': ['Small Crag Boar', 'Burly Rockjaw Trogg'], 'area_ids': ['offline_patch']}}
            await r.action('probe_forward'); r.position = [10.4, 10.]
            await r.action('detour_strafe_right'); r.position = [10.4, 10.4]
            await r.choose(None); await r.choose(None)
            projection = latest(r)
            assert projection['retry']
            assert 'eligible levels:' in projection['context'] and 'Small Crag Boar' in projection['context']
            assert 'Current measured forward direction: unavailable' in projection['context']
            assert 'current side: right' in projection['context']
            assert 'original rationale:' in projection['context']
            assert 'detour_forward' in projection['candidate_ids']
            assert 'tests one continuation' in projection['context']
            assert 'Begin hunting if' not in projection['context']
            assert 'begin_hunt' not in projection['candidate_ids']
            assert projection['core_chars'] <= 2200
            assert projection['context_chars'] + projection['instruction_chars'] <= 4000
        finally:
            await r.close()
    asyncio.run(run())


def test_replanned_detour_does_not_report_recorded_side_as_active(tmp_path):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); await r.action('detour_strafe_left')
            r.position = [10., 10.4]
            await r.action('change_destination')
            h = r.c.hunt
            h.choose(dict(h.plan['area']), r.world.capture(), 'reselected', 1)
            await r.choose(None)
            context = latest(r)['context']
            assert 'current side: none' in context
            assert 'recorded side: left' in context
        finally:
            await r.close()
    asyncio.run(run())


def test_protected_context_overflow_is_visible_and_has_no_gameplay(tmp_path):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel()
            r.c.hunt.travel_policy['mapping_unavailable_reason'] = 'currentness reason ' * 300
            before = list(r.physical_keys())
            result = await r.choose(None)
            assert result.status == 'grind_blocked'
            assert r.c.hunt.blocked['reason'] == 'blocked_travel_context'
            assert not r.sage.calls and r.physical_keys() == before
            assert events(r, 'grind_travel_context_overflow')
        finally:
            await r.close()
    asyncio.run(run())
