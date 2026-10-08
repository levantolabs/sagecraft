"""Exhausted navigation hands back hunting choices without native input or APIs."""
import asyncio
from copy import deepcopy

import pytest

from sage_wow.agent import grind_navigation_recovery as recovery
from sage_wow.agent import grind_travel_scout as scout
from test_grind_travel_scout import setup
from test_grind_travel_recovery import events


def destinations(r):
    h = r.c.hunt
    h.catalog = {'offline_patch': deepcopy(h.plan['area']),
        'other_patch': {**deepcopy(h.plan['area']), 'id': 'other_patch',
            'label': 'Another attributed hunting patch', 'coordinate': [20., 20.]}}
    h.hunting_progression_by_player_level = {1: {
        'primary_targets': ['Young Boar'], 'area_ids': list(h.catalog)}}


async def exhaust_pages(r):
    """Drive real null decisions up to, but not including, the focused question."""
    await r.choose(None)
    for _ in range(4):
        await r.choose(None)
        attempts = events(r, 'grind_travel_retry_menu_attempt')
        if attempts and not attempts[-1]['remaining_ids']:
            return
    raise AssertionError('Expected bounded travel coverage')


@pytest.mark.parametrize('clean', [False, True])
def test_null_navigation_page_did_not_spend_tab_and_absence_returns_same_review(tmp_path, monkeypatch, clean):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        r.c.config['clean_world_observation'] = clean
        try:
            destinations(r); h = r.c.hunt
            h.action_failures['retained-combat-method'] = 2
            await exhaust_pages(r)
            menu = deepcopy(h.travel_policy['retry_menu'])
            assert 'target_enemy' in menu['presented'] and not (set(r.physical_keys()) - {58, 6})
            result = await r.action('target_enemy')
            state = recovery.record(r.c)
            assert state['requests'] == 1 and not h.blocked
            assert set(r.sage.calls[-1]['options']) == {'target_enemy', 'choose_area:other_patch', 'urgent_state'}
            assert h.pending['family'] == 'target' and scout.active(r.c)
            assert result.receipt['source_frame_id'] != result.receipt['dispatch_frame_id']
            assert r.physical_keys().count(r.controls['target_enemy']['keycode']) == 1
            assert h.travel_policy['retry_menu']['presented'] == menu['presented']
            await r.choose('no_selected_frame')
            assert h.phase == 'travel' and not scout.active(r.c)
            assert recovery.record(r.c) is state and state['requests'] == 1
            assert h.action_failures['retained-combat-method'] == 2 and h.failures['target'] == 1
            await r.choose(None)
            assert state['requests'] == 2
            assert 'target_enemy' not in r.sage.calls[-1]['options']
            assert 'offered Tab' not in r.sage.calls[-1]['prompt']
            assert state['rounds_started'] == 2 and state['round_attempt'] == 1
            await r.choose(None)  # This distinct round retains its actual-null auto retry.
            assert state['requests'] == 3 and state['terminal_null']
            calls = len(r.sage.calls)
            result = await r.choose(None)
            r.sage.answers.clear()
            assert result.status == 'grind_navigation_wait' and len(r.sage.calls) == calls
            assert not h.blocked and r.c.navigation_wait['state'] == 'waiting'
            assert not h.progress_facts
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interruption', ['none', 'focus', 'level'])
def test_two_null_reviews_keep_local_auto_mode_and_wait_without_global_block(tmp_path, monkeypatch, interruption):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); await exhaust_pages(r)
            await r.choose(None)
            state = recovery.record(r.c)
            assert state['requests'] == 1 and state['last_outcome'] == 'null'
            if interruption == 'focus':
                r.c.pause_focus(); r.c.resume_focus(); await r.choose('world_normal_confirmed')
            elif interruption == 'level':
                r.c.level.last_attempt_at = 0
                await r.choose('player_level_1')
            await r.choose(None)
            request_ids = {item['request_id'] for item in events(r, 'grind_navigation_review_started')}
            requests = [item for item in events(r, 'sage_request_started') if item['request_id'] in request_ids]
            assert [item['reasoning_mode'] for item in requests] == ['off', 'auto']
            assert state['requests'] == 2
            assert state['rounds_started'] == 1 and state['terminal_null']
            calls = len(r.sage.calls)
            result = await r.choose(None); r.sage.answers.clear()
            assert result.status == 'grind_navigation_wait' and len(r.sage.calls) == calls
            assert not r.c.hunt.blocked and not r.physical_keys()
            assert not r.c.hunt.progress_facts
        finally:
            await r.close()
    asyncio.run(run())


def focused_modes(r):
    ids = {item['request_id'] for item in events(r, 'grind_navigation_review_started')}
    return [item['reasoning_mode'] for item in events(r, 'sage_request_started')
        if item['request_id'] in ids]


@pytest.mark.parametrize('first_null', [False, True])
@pytest.mark.parametrize('clean', [False, True])
def test_completed_scout_preserves_next_round_retry_with_absolute_four_call_limit(tmp_path, monkeypatch, first_null, clean):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        r.c.config['clean_world_observation'] = clean
        try:
            destinations(r); await exhaust_pages(r)
            h = r.c.hunt; original = deepcopy(h.plan['area'])
            debt = deepcopy(h.travel_policy['retry_menu'])
            h.action_failures['prior-cast-method'] = 2
            if first_null:
                await r.choose(None)
            await r.action('target_enemy')
            state = recovery.record(r.c)
            assert state['rounds_started'] == 1 and not state['terminal_null']
            scout_record = deepcopy(h.travel_policy['last_scout'])
            await r.choose('no_selected_frame')
            r.c.pause_focus(); r.c.resume_focus(); await r.action('world_normal_confirmed')
            await r.choose(None)
            assert state['rounds_started'] == 2 and state['round_attempt'] == 1
            assert state['last_outcome'] == 'null' and recovery.reasoning(state) == 'auto'
            await r.action('choose_area:other_patch')
            expected = ['off', 'auto', 'off', 'auto'] if first_null else ['off', 'off', 'auto']
            assert focused_modes(r) == expected
            assert state['requests'] == len(expected) and state['rounds_started'] == 2
            assert not recovery.available(state) and len(state['request_ids']) == len(set(state['request_ids']))
            assert h.travel_policy['last_scout']['receipt_id'] == scout_record['receipt_id']
            assert r.physical_keys().count(r.controls['target_enemy']['keycode']) == 1
            assert h.action_failures['prior-cast-method'] == 2 and h.failures['target'] == 1
            h.choose(original, r.world.capture(), 'same-ground-return', 1)
            calls = len(r.sage.calls)
            result = await r.choose(None); r.sage.answers.clear()
            assert result.status == 'grind_navigation_wait' and len(r.sage.calls) == calls
            assert h.travel_policy['retry_menu']['presented'] == debt['presented']
            assert recovery.record(r.c) is state and not h.blocked and not h.progress_facts
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('first_null', [False, True])
@pytest.mark.parametrize('failure', ['timeout', 'error', 'guard'])
def test_failed_started_attempt_spends_round_without_auto_entitlement(tmp_path, monkeypatch, first_null, failure):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); await exhaust_pages(r)
            if first_null:
                await r.choose(None)
            if failure == 'guard':
                async def change_owner(): r.c.hunt.search_revision += 1
                r.sage.hook = change_owner
                result = await r.choose('choose_area:other_patch')
                r.sage.hook = None
                assert result.status in {'precondition_failed', 'dispatch_guard_rejected'}
            else:
                result = await r.choose(TimeoutError('offline timed out') if failure == 'timeout'
                    else RuntimeError('offline provider failed'))
                assert result.status in {'travel_deadline_expired', 'decision_failed'}
            state = recovery.record(r.c)
            assert state['rounds_started'] == 1 and state['last_outcome'] != 'null'
            assert recovery.reasoning(state) == 'off' and recovery.available(state)
            await r.choose(None); await r.choose(None)
            assert focused_modes(r) == (['off', 'auto', 'off', 'auto'] if first_null else ['off', 'off', 'auto'])
            assert state['rounds_started'] == 2 and state['terminal_null']
            before = len(r.sage.calls)
            result = await r.choose(None); r.sage.answers.clear()
            assert result.status == 'grind_navigation_wait' and len(r.sage.calls) == before
            assert not r.physical_keys() and not r.c.hunt.blocked
        finally:
            await r.close()
    asyncio.run(run())


def test_urgent_round_and_real_recovery_do_not_refund_next_question(tmp_path, monkeypatch):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); await exhaust_pages(r)
            await r.action('urgent_state')
            state = recovery.record(r.c)
            assert state['rounds_started'] == 1 and state['requests'] == 1
            await r.action('recovered_resume')
            await r.choose(None); await r.choose(None)
            assert focused_modes(r) == ['off', 'off', 'auto']
            assert state['rounds_started'] == 2 and state['requests'] == 3 and state['terminal_null']
            before = len(r.sage.calls)
            result = await r.choose(None); r.sage.answers.clear()
            assert result.status == 'grind_navigation_wait' and len(r.sage.calls) == before
            assert not r.c.hunt.blocked and not r.physical_keys()
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('first_null', [False, True])
def test_cancelled_admitted_round_closes_and_focus_cannot_refund_it(tmp_path, monkeypatch, first_null):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); await exhaust_pages(r)
            if first_null:
                await r.choose(None)
            entered = asyncio.Event(); release = asyncio.Event()
            async def hold_provider():
                entered.set(); await release.wait()
            r.sage.hook = hold_provider
            task = asyncio.create_task(r.choose(None))
            await asyncio.wait_for(entered.wait(), timeout=2)
            state = recovery.record(r.c); request_id = state['last_request_id']
            assert state['last_outcome'] == 'started' and not recovery.available(state)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            r.sage.hook = None; r.sage.answers.clear()
            assert state['last_outcome'] == 'navigation_review_interrupted'
            assert state['rounds_started'] == 1 and recovery.reasoning(state) == 'off'
            assert any(event['request_id'] == request_id for event in events(r, 'sage_response_discarded'))
            assert not r.c.cycle._inflight.locked()
            r.c.pause_focus(); r.c.resume_focus(); await r.action('world_normal_confirmed')
            await r.choose(None); await r.choose(None)
            assert focused_modes(r) == (['off', 'auto', 'off', 'auto'] if first_null else ['off', 'off', 'auto'])
            assert state['rounds_started'] == 2 and state['terminal_null']
            before = len(r.sage.calls)
            result = await r.choose(None); r.sage.answers.clear()
            assert result.status == 'grind_navigation_wait' and len(r.sage.calls) == before
            assert not r.physical_keys() and not r.c.hunt.blocked
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('first_null', [False, True])
def test_before_marker_deadline_rejection_spends_neither_round_nor_null_retry(tmp_path, monkeypatch, first_null):
    from dataclasses import replace
    import time
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); await exhaust_pages(r)
            if first_null:
                await r.choose(None)
            delegate = r.c.cycle.decide_and_execute
            async def expired(*args, **kwargs):
                kwargs['travel_budget'] = replace(kwargs['travel_budget'], deadline=time.time() - 1)
                return await delegate(*args, **kwargs)
            r.c.cycle.decide_and_execute = expired
            before = len(r.sage.calls)
            result = await r.choose(None); r.sage.answers.clear()
            r.c.cycle.decide_and_execute = delegate
            state = recovery.record(r.c)
            assert result.status == 'travel_deadline_expired' and len(r.sage.calls) == before
            assert state['requests'] == int(first_null) and state['rounds_started'] == int(first_null)
            assert recovery.reasoning(state) == ('auto' if first_null else 'off')
            await r.choose(None)
            assert focused_modes(r) == (['off', 'auto'] if first_null else ['off'])
            assert not r.physical_keys()
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['round', 'request_id'])
def test_focused_destination_cannot_dispatch_after_its_admission_changes(tmp_path, monkeypatch, change):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); await exhaust_pages(r)
            plan = deepcopy(r.c.hunt.plan)
            async def change_accounting():
                state = recovery.record(r.c)
                if change == 'round': state['rounds_started'] += 1
                else: state['last_request_id'] = 'different-admission'
            r.sage.hook = change_accounting
            result = await r.choose('choose_area:other_patch')
            assert result.status in {'precondition_failed', 'dispatch_guard_rejected'}
            assert r.c.hunt.plan == plan and not r.physical_keys()
            assert len(events(r, 'grind_navigation_review_started')) == 1
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('alias_delta', [0., .1])
def test_revisited_goal_restores_its_consumed_menu_and_review_budget(tmp_path, monkeypatch, alias_delta):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); await exhaust_pages(r)
            h = r.c.hunt; old_area = deepcopy(h.plan['area'])
            await r.action('choose_area:other_patch')
            state = recovery.record(r.c)
            assert state['requests'] == 1 and h.plan['area_id'] == 'other_patch'
            await r.choose(None)  # A genuinely new physical goal has its initial question.
            assert len(r.sage.calls[-1]['options']) > 6
            old_area['coordinate'][0] += alias_delta
            h.choose(old_area, r.world.capture(), 'returned-or-relabeled-goal', 1)
            await r.choose(None)
            assert state['requests'] == 2
            assert set(r.sage.calls[-1]['options']) == {'target_enemy', 'urgent_state'}
            assert h.travel_policy['retry_menu']['exhausted']
            assert not h.progress_facts and not r.physical_keys()
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['health', 'target', 'world', 'owner', 'catalog'])
def test_destination_guard_rechecks_after_provider_before_task_change(tmp_path, monkeypatch, change):
    async def run():
        r, values = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); await exhaust_pages(r)
            old_plan = deepcopy(r.c.hunt.plan)
            async def changed():
                if change == 'health': values['player_health'] = .2
                elif change == 'target': r.world.name = 'Young Boar'
                elif change == 'world': r.c.require_world = True
                elif change == 'owner': r.c.hunt.search_revision += 1
                else: r.c.hunt.catalog['other_patch']['coordinate'] = [50., 50.]
            r.sage.hook = changed
            result = await r.choose('choose_area:other_patch')
            assert result.status in {'precondition_failed', 'dispatch_guard_rejected'}
            assert r.c.hunt.plan == old_plan and recovery.record(r.c)['requests'] == 1
            assert not r.physical_keys() and not r.c.hunt.blocked
        finally:
            await r.close()
    asyncio.run(run())


def test_selected_target_after_wait_enters_inspection_instead_of_travel(tmp_path, monkeypatch):
    async def run():
        r, values = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); await exhaust_pages(r)
            await r.choose(None); await r.choose(None)
            await r.choose(None); r.sage.answers.clear()
            assert r.c.navigation_wait
            r.world.name = 'Young Boar'; r.world.target_level = 1; values['target_health'] = 1.
            await r.choose(None)
            assert r.c.hunt.phase == 'search' and not r.c.navigation_wait
            assert 'probe_forward' not in r.sage.calls[-1]['options']
            assert 'choose_area:other_patch' not in r.sage.calls[-1]['options']
            assert recovery.record(r.c)['requests'] == 2 and not r.c.hunt.blocked
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('offered', ['scout', 'destination'])
def test_focused_question_only_invites_actions_in_its_actual_menu(tmp_path, monkeypatch, offered):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r)
            if offered == 'scout':
                r.c.hunt.catalog.pop('other_patch')
            else:
                r.position = None
            await exhaust_pages(r)
            await r.choose(None)
            call = r.sage.calls[-1]
            if offered == 'scout':
                assert set(call['options']) == {'target_enemy', 'urgent_state'}
                assert 'offered Tab action' in call['prompt']
                assert 'Choose an offered different hunting destination' not in call['prompt']
            else:
                assert set(call['options']) == {'choose_area:other_patch', 'urgent_state'}
                assert 'offered Tab' not in call['prompt']
                assert 'Choose an offered different hunting destination' in call['prompt']
            assert recovery.record(r.c)['requests'] == 1
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('clean', [False, True])
def test_only_current_resolved_post_review_translation_retires_review(tmp_path, monkeypatch, clean):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        r.c.config['clean_world_observation'] = clean
        try:
            destinations(r); await exhaust_pages(r)
            await r.action('choose_area:other_patch')
            state = recovery.record(r.c)
            r.c.hunt.action_failures['old-combat-method'] = 2
            await r.action('probe_forward')
            r.position = [10.5, 10.]
            await r.choose(None)
            assert recovery.record(r.c) is None
            assert state['requests'] == 1
            assert r.c.hunt.action_failures['old-combat-method'] == 2
            retired = events(r, 'grind_navigation_review_retired')
            assert len(retired) == 1 and not retired[0]['allowance_renewed']
            assert not retired[0]['progress_credit']
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['tiny_translation', 'turn', 'new_frame', 'anchor_hash'])
def test_insufficient_or_corrupt_evidence_cannot_retire_review(tmp_path, monkeypatch, change):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); await exhaust_pages(r)
            await r.action('choose_area:other_patch')
            state = recovery.record(r.c)
            if change == 'turn':
                await r.action('turn_left')
            elif change in {'tiny_translation', 'anchor_hash'}:
                await r.action('probe_forward')
                r.position = [10.1 if change == 'tiny_translation' else 10.5, 10.]
                if change == 'anchor_hash':
                    state['anchor']['source_hash'] = 'corrupted fixture hash'
            await r.choose(None)
            assert recovery.record(r.c) is state and state['requests'] == 1
            assert not events(r, 'grind_navigation_review_retired')
        finally:
            await r.close()
    asyncio.run(run())


def test_changed_turn_rearms_one_question_without_replay_on_redeclaration(tmp_path, monkeypatch):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); await exhaust_pages(r)
            h = r.c.hunt; old_area = deepcopy(h.plan['area'])
            await r.action('choose_area:other_patch')
            await r.action('turn_left')
            await r.choose(None)  # Resolves the turn and opens this new question.
            state = recovery.record(r.c)
            assert state['requests'] == 1
            h.choose(old_area, r.world.capture(), 'return-once-after-turn', 1)
            await r.choose(None)
            assert len(r.sage.calls[-1]['options']) > 6
            assert h.travel_policy['retry_menu']['pages'] == 0
            h.choose(old_area, r.world.capture(), 'repeat-same-turn', 1)
            await r.choose(None)
            assert h.travel_policy['retry_menu']['pages'] == 1
            assert len(r.sage.calls[-1]['options']) <= 6
            assert recovery.record(r.c) is state and state['requests'] == 1
        finally:
            await r.close()
    asyncio.run(run())


def test_unknown_anchor_requires_later_baseline_then_a_new_resolved_translation(tmp_path, monkeypatch):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); r.position = None; await exhaust_pages(r)
            await r.action('choose_area:other_patch')
            state = recovery.record(r.c)
            assert state['anchor']['position'] is None and state['requests'] == 1
            goals = deepcopy(state['goals'])
            r.position = [10., 10.]
            await r.action('probe_forward')
            baseline = deepcopy(state['anchor'])
            assert baseline['position'] == [10., 10.]
            assert state['requests'] == 1 and state['goals'] == goals
            assert not events(r, 'grind_navigation_review_retired')
            r.position = [10.5, 10.]
            await r.choose(None)
            assert recovery.record(r.c) is None
            assert len(events(r, 'grind_navigation_review_baseline')) == 1
            assert len(events(r, 'grind_navigation_review_retired')) == 1
        finally:
            await r.close()
    asyncio.run(run())


def test_later_readable_baseline_cannot_credit_a_carried_older_movement(tmp_path, monkeypatch):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r)
            await r.action('probe_forward'); r.position = [10.5, 10.]
            await r.choose(None)
            old = deepcopy(r.c.hunt.last_completed_action)
            assert old['status'] == 'observed_displacement'
            r.position = None
            # The ordinary question already abstained, so continue its actual pages.
            for _ in range(4):
                await r.choose(None)
                if not events(r, 'grind_travel_retry_menu_attempt')[-1]['remaining_ids']:
                    break
            await r.action('choose_area:other_patch')
            state = recovery.record(r.c)
            assert state['anchor']['position'] is None
            r.position = [10.5, 10.]
            await r.choose(None)
            assert state['anchor']['captured_at'] > old['source_captured_at']
            assert recovery.record(r.c) is state and state['requests'] == 1
            await r.choose(None)
            assert recovery.record(r.c) is state
            assert not events(r, 'grind_navigation_review_retired')
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('retain_live_menu', [False, True])
def test_fourth_goal_cannot_reopen_a_full_goal_record_and_old_goal_stays_spent(tmp_path, monkeypatch, retain_live_menu):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            destinations(r); h = r.c.hunt; first = deepcopy(h.plan['area'])
            second = deepcopy(h.catalog['other_patch'])
            third = {**deepcopy(first), 'id': 'third', 'coordinate': [40., 10.]}
            fourth = {**deepcopy(first), 'id': 'fourth', 'coordinate': [50., 10.]}
            h.catalog = {a['id']: a for a in (fourth, first, second, third)}
            h.hunting_progression_by_player_level[1]['area_ids'] = list(h.catalog)
            await exhaust_pages(r); initial = deepcopy(h.travel_policy['retry_menu'])
            await r.action('choose_area:other_patch'); await exhaust_pages(r)
            await r.action('choose_area:third')
            state = recovery.record(r.c)
            assert state['requests'] == 2 and len(state['goals']) == 3
            await r.action('change_destination')
            assert h.travel_policy.get('retry_menu') is None
            await r.action('choose_area:fourth')
            if retain_live_menu:
                # A retained old snapshot cannot make this capacity case weaker.
                h.travel_policy['retry_menu'] = deepcopy(initial)
            calls = len(r.sage.calls)
            result = await r.choose(None); r.sage.answers.clear()
            assert result.status == 'grind_navigation_wait' and len(r.sage.calls) == calls
            assert recovery.record(r.c) is state and len(state['goals']) == 3
            assert events(r, 'grind_travel_retry_menu_exhausted')[-1]['exhaustion_reason'] == 'goal_history_capacity'
            h.choose(first, r.world.capture(), 'return-recorded-goal', 1)
            result = await r.choose(None); r.sage.answers.clear()
            assert result.status == 'grind_navigation_wait' and len(r.sage.calls) == calls
            assert h.travel_policy['retry_menu']['presented'] == initial['presented']
            assert not h.blocked and not r.physical_keys() and not h.progress_facts
        finally:
            await r.close()
    asyncio.run(run())
