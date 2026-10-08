"""Live-run regression: real controller transitions with synthetic Sage, pixels and input."""
import asyncio
from copy import deepcopy
import time

import pytest

from test_grind_travel_acceptance import TravelRig
from test_grind_travel_recovery import events


@pytest.mark.parametrize('clean', [False, True])
def test_travel_abstention_does_not_enable_slow_retry_mode(tmp_path, clean):
    async def run():
        r = TravelRig(tmp_path, clean=clean)
        try:
            await r.travel()
            result = await r.choose(None)
            assert result.no_input_abstention and r.c.null_answers == 1
            assert not r.c.hunt.pending
            await r.action('detour_strafe_left')
            requests = events(r, 'sage_request_started')[-2:]
            assert [item['reasoning_mode'] for item in requests] == ['off', 'off']
            assert r.c.null_answers == 0 and r.c.hunt.pending['purpose'] == 'travel'
            assert not r.c.hunt.credited_kills and not r.pressed
        finally:
            await r.close()
    asyncio.run(run())


def test_travel_timeouts_back_off_without_level_reads_erasing_failures(tmp_path):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel()
            for count, delay in enumerate((1, 2, 4, 15), 1):
                before = time.time()
                # Model provider expiry immediately; this is a control-flow
                # test, not a claim about real provider latency.
                result = await r.choose(TimeoutError('offline timeout'))
                assert result.status == 'travel_deadline_expired'
                assert r.c.timing_failures == count
                assert r.c.wait_until >= before + delay - .1
                assert not r.c.hunt.pending and not r.physical_keys()
                if count < 4:
                    r.c.level.last_attempt_at = 0
                    await r.choose('player_level_1')
                    assert r.c.timing_failures == count
            assert r.c.hunt.blocked['reason'] == 'blocked_timing'
            calls = len(r.sage.calls)
            result = await r.c.process(r.world.capture())
            assert result.status in {'grind_wait', 'grind_blocked'}
            assert len(r.sage.calls) == calls and not r.physical_keys()
            # A fresh explicit reassessment can reopen the same destination.
            r.c.hunt.blocked.update(next_observation_at=0, assessment_due_at=0)
            await r.choose('blocked_changed_assessment')
            assert not r.c.hunt.blocked and r.c.timing_failures == 4
            await r.action('probe_forward')
            assert r.c.timing_failures == 0
            assert r.c.hunt.pending['purpose'] == 'travel'
        finally:
            await r.close()
    asyncio.run(run())


def test_level_check_becoming_due_inside_travel_cycle_keeps_timing_debt(tmp_path):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel()
            r.c.timing_failures = 3
            # Budget classification happens before routing. The level timer
            # can become due between these checks without any game change.
            checks = iter((False, False, True))
            r.c.level.due = lambda now: next(checks, True)
            await r.choose('player_level_1')
            assert r.c.timing_failures == 3 and not r.physical_keys()
        finally:
            await r.close()
    asyncio.run(run())


def test_blocked_search_reassessment_keeps_debt_until_next_timed_decision(tmp_path):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel()
            h = r.c.hunt
            h.phase = 'choose_area'
            h.planning_requested = True
            r.c.timing_failures = 4
            r.c.enter_blocked('blocked_timing', None)
            h.blocked.update(next_observation_at=0, assessment_due_at=0)
            await r.choose('blocked_changed_assessment')
            assert not h.blocked and r.c.timing_failures == 4
            assert not r.physical_keys()
            await r.choose('explore_visible')
            assert r.c.timing_failures == 0 and h.phase == 'travel'
            assert not h.progress_facts and not r.physical_keys()
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('destination', ['visual', 'catalog'])
def test_recovery_destination_selection_reaches_next_travel_cycle(tmp_path, destination):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel()
            h = r.c.hunt
            h.catalog['offline_patch'] = deepcopy(h.plan['area'])
            h.action_failures[h.motion_key('forward', 'search')] = 2
            h.failures['target'] = 2
            h.confirmed_target_absences = 2
            r.c.request_recovery('offline unresolved search')
            history = deepcopy(h.recovery_history)
            debt = deepcopy(h.action_failures)
            revision = h.search_revision
            await r.choose('change_search_strategy')
            strategy = deepcopy(h.strategy_required)
            await r.choose('explore_visible' if destination == 'visual' else 'choose_area:offline_patch')
            assert h.phase == 'travel' and h.compact_stage != 'recovery'
            assert h.recovery_history == history and h.recovery_requested
            assert h.action_failures == debt and h.failures['target'] == 2
            assert h.search_revision == revision and h.strategy_required == strategy
            assert not r.physical_keys() and not h.progress_facts
            await r.action('detour_strafe_left')
            assert 'Current task: Reach the selected hunting area.' in r.sage.calls[-1]['prompt']
            assert h.pending['purpose'] == 'travel'
            assert h.search_revision == revision and h.action_failures == debt
            assert 'begin_hunt' not in r.sage.calls[-1]['options']
            assert not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interruption', ['world', 'input_effect', 'survival'])
def test_replanned_travel_still_yields_to_current_recovery(tmp_path, interruption):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel()
            r.c.hunt.confirmed_target_absences = 2
            r.c.request_recovery('offline unresolved search')
            await r.choose('change_search_strategy')
            await r.choose('explore_visible')
            if interruption == 'world':
                r.c.require_world = True
            elif interruption == 'input_effect':
                r.c.hunt.input_effect_unverified = True
                r.c.hunt.phase = 'input_effect_unverified'
            else:
                r.c.hunt.phase = 'recover'
            await r.choose(None)
            assert 'Current task: Reach the selected hunting area.' not in r.sage.calls[-1]['prompt']
            assert 'detour_strafe_left' not in r.sage.calls[-1]['options']
            assert not r.physical_keys() and not r.c.hunt.pending
        finally:
            await r.close()
    asyncio.run(run())
