"""Actual offline question paths distinguish local opportunity from empty search."""
import asyncio
from copy import deepcopy

import pytest

from sage_wow.agent import grind_navigation_recovery as recovery
from sage_wow.agent.grind_progression import primary_region, region_context, selected_policy
from test_grind_opportunistic_targets import rig, snapshot
from test_grind_empty_search import empty_start
from test_grind_product_spec import Rig
from test_grind_navigation_recovery import destinations, exhaust_pages
from test_grind_travel_scout import setup
from test_grind_travel_recovery import events


@pytest.mark.parametrize('location', ['troggs', 'outside', 'unknown', 'depleted'])
def test_region_context_uses_actual_shared_geometry_without_selection_or_new_policy(tmp_path, location):
    async def run():
        r = await rig(tmp_path, 4)
        try:
            h = r.c.hunt
            area = h.catalog['coldridge_southeast_troggs']
            r.position = area['coordinate'][:]
            if location == 'outside': r.position = [31., 73.3]
            if location == 'unknown': r.position = None
            if location == 'depleted':
                h.area_results[area['id']] = {'status': 'depleted_or_unsupported_hypothesis'}
            frame, target, measurement = await snapshot(r)
            before = deepcopy((h.plan, h.area_results, h.learned, h.hunting_progression_by_player_level))
            fact = primary_region(r.c, frame, measurement)
            selected = selected_policy(r.c, target, frame, measurement)
            expected = {'troggs': 'inside_primary_region', 'outside': 'outside_primary_region',
                'unknown': 'ground_unknown', 'depleted': 'outside_primary_region'}[location]
            assert fact['kind'] == expected
            assert selected == ({**fact, 'kind': 'opportunistic_here'} if location == 'troggs' else fact)
            message = region_context(r.c, frame, measurement)
            if location == 'troggs': assert area['label'] in message and 'Inspect nearby candidates' in message
            if location == 'unknown': assert 'unverified' in message
            assert (h.plan, h.area_results, h.learned, h.hunting_progression_by_player_level) == before
        finally: await r.close()
    asyncio.run(run())


def test_idle_acquisition_on_trogg_ground_offers_local_actions_with_current_region(tmp_path):
    async def run():
        r = await rig(tmp_path, 4)
        try:
            h = r.c.hunt; area = h.catalog['coldridge_southeast_troggs']
            r.position = area['coordinate'][:]; r.world.name = ''; r.world.target_level = None
            h.choose(area, r.world.capture(), 'deliberate-current-region', 4)
            h.plan['phase'] = 'search'; h.phase = 'search'; h.planning_requested = False
            h.progression_pending = False
            await r.choose(None)
            call = r.sage.calls[-1]
            assert {'target_enemy', 'turn_left', 'turn_right', 'forward'} <= call['options'].keys()
            assert not {'change_search_strategy', 'inspect_recent_corpse', 'cannot_assess'} & call['options'].keys()
            assert area['label'] in call['prompt'] and 'inside attributed primary hunting region' in call['prompt']
            assert h.confirmed_target_absences == 0 and h.plan['area_id'] == area['id']
            assert not r.casts() and not r.physical_keys()
        finally: await r.close()
    asyncio.run(run())


def test_zero_target_misses_and_genuine_empty_search_get_different_planning_questions(tmp_path):
    async def run():
        for empty in (False, True):
            directory = tmp_path / str(empty); directory.mkdir()
            r = Rig(directory)
            try:
                if empty: h = await empty_start(r)
                else:
                    await r.start(); r.world.name = ''; h = r.c.hunt
                    # User-selected strategy after movement failure; no target absence outcome.
                    h.phase = 'choose_area'; h.planning_requested = True
                    h.strategy_required = {'reason': 'deliberate_strategy_change', 'search_revision': h.search_revision}
                    h.failures['motion'] = 2
                await r.choose('explore_visible')
                call = r.sage.calls[-1]
                assert ('Repeated Tab attempts' in call['instructions']) is empty
                assert 'Local bounded search is exhausted' not in call['instructions']
                assert 'Returning to unchanged local inspection cannot resolve' not in call['prompt']
                assert h.confirmed_target_absences == (3 if empty else 0)
                assert h.phase == 'travel' and not r.casts()
            finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('unfinished', [False, True])
def test_real_navigation_wait_records_no_admission_vs_spent_rounds_without_refund(tmp_path, monkeypatch, unfinished):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); await exhaust_pages(r)
            h = r.c.hunt
            if unfinished:
                h.encounter_ended = False
                h.cast_obligation = 'Linked ineffective cast: assess a different correction or abandon'
            else:
                await r.choose(None); await r.choose(None)
            before = deepcopy(h.travel_policy)
            calls = len(r.sage.calls); keys = r.physical_keys()
            result = await r.choose(None); r.sage.answers.clear()
            assert result.status == 'grind_navigation_wait'
            diagnostic = r.c.navigation_wait['readiness']
            state = recovery.record(r.c)
            assert diagnostic['admitted_requests'] == state['requests'] == (0 if unfinished else 2)
            assert diagnostic['reason'] == ('task_prerequisite' if unfinished else 'review_capacity_spent')
            if unfinished: assert {'unfinished_encounter', 'retained_cast_constraint'} <= set(diagnostic['prerequisites'])
            assert len(r.sage.calls) == calls and r.physical_keys() == keys
            if not unfinished: assert h.travel_policy == before
            checkpoint = r.store.load_checkpoint('grind_only')
            assert checkpoint['navigation_wait']['readiness'] == diagnostic
            assert checkpoint['hunt']['travel_policy']['recovery_review'] == state
            count = len(events(r, 'grind_navigation_wait_readiness'))
            await r.choose(None); r.sage.answers.clear()
            assert len(events(r, 'grind_navigation_wait_readiness')) == count
            assert state['requests'] == (0 if unfinished else 2)
        finally: await r.close()
    asyncio.run(run())
