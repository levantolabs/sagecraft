"""Finite menus through the production controller, not automatic gameplay."""
import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from test_grind_travel_acceptance import TravelRig
from test_grind_travel_recovery import events
from test_active_attacker_recovery import install_hud


async def exhaust(r):
    await r.choose(None)
    full = set(r.sage.calls[-1]['options'])
    pages = []
    for _ in range(7):  # Four pages, then one off/auto round ending in two nulls.
        count = len(r.sage.calls)
        attempts = len(events(r, 'grind_travel_retry_menu_attempt'))
        reviews = len(events(r, 'grind_navigation_review_started'))
        result = await r.choose(None)
        if result.status == 'grind_navigation_wait':
            assert len(r.sage.calls) == count
            break
        assert len(r.sage.calls) == count + 1
        if len(events(r, 'grind_navigation_review_started')) > reviews:
            assert len(events(r, 'grind_travel_retry_menu_attempt')) == attempts
            assert all(name in {'target_enemy', 'urgent_state'} or name.startswith('choose_area:')
                       for name in r.sage.calls[-1]['options'])
            continue
        assert len(events(r, 'grind_travel_retry_menu_attempt')) == attempts + 1
        page = set(r.sage.calls[-1]['options'])
        assert 1 <= len(page) <= 6
        assert 'urgent_state' in page
        assert page <= full
        assert not (page - {'urgent_state'}) & set().union(*pages)
        pages.append(page - {'urgent_state'})
    assert result.status == 'grind_navigation_wait' and r.c.hunt.blocked is None
    assert len(events(r, 'grind_navigation_review_started')) <= 2
    review = r.c.hunt.travel_policy.get('recovery_review')
    if review and review['requests']:
        assert review['rounds_started'] == 1 and review['terminal_null']
    assert set().union(*pages) == full - {'urgent_state'}
    assert r.c.hunt.travel_policy['retry_menu']['exhausted']
    r.sage.answers.clear()  # The wait correctly never called the fake provider.
    return full, pages


@pytest.mark.parametrize('clean', [False, True])
def test_recovery_instructions_name_only_the_actual_page_actions(tmp_path, clean):
    async def run():
        r = TravelRig(tmp_path, clean=clean)
        try:
            await r.travel(); install_hud(r, target_health=None)
            await r.choose(None)
            assert 'Choose the forward probe if there is room for it' in r.sage.calls[-1]['instructions']
            pages = []
            for _ in range(4):
                result = await r.choose(None)
                if result.status == 'grind_navigation_wait' or events(r, 'grind_navigation_review_started'):
                    break
                call = r.sage.calls[-1]
                question = call['instructions'].split('\n\n')[0]
                ids = set(call['options'])
                assert len(ids) <= 6
                named = question.split(': ', 1)[1].removesuffix('.').split(', ')
                assert named == list(call['options'])
                assert 'Choose the forward probe' not in question
                pages.append(ids)
            assert len(pages) >= 2 and 'probe_forward' not in pages[1]
            assert not (set(r.physical_keys()) - {58, 6})
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('clean', [False, True])
@pytest.mark.parametrize('answer_kind', ['plain', 'finished', 'capped'])
def test_null_retries_cover_pages_then_bounded_hunting_review_without_progress(tmp_path, clean, answer_kind):
    async def run():
        r = TravelRig(tmp_path, clean=clean)
        try:
            await r.travel(); install_hud(r, target_health=None)
            provider = r.sage.decide_image_choice
            async def null_provider(*args, **kwargs):
                result = await provider(*args, **kwargs)
                return replace(result, reasoning={'ran': answer_kind != 'plain',
                    'finished': answer_kind == 'finished',
                    'limited': 'max_output_tokens' if answer_kind == 'capped' else None})
            r.sage.decide_image_choice = null_provider
            h = r.c.hunt
            plan = deepcopy(h.plan); failures = deepcopy(h.action_failures)
            full, pages = await exhaust(r)
            assert {'turn_left', 'turn_right', 'detour_forward_left', 'detour_backward_right', 'change_destination'} <= full
            assert len(pages) <= 4
            assert h.plan == plan and h.action_failures == failures
            assert not (set(r.physical_keys()) - {58, 6}) and not h.progress_facts and not h.credited_kills
            count = len(r.sage.calls)
            await r.c.process(r.world.capture())
            assert len(r.sage.calls) == count
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('withheld', ['forward_binding', 'all_motion'])
@pytest.mark.parametrize('clean', [False, True])
def test_filtered_initial_menu_does_not_request_an_absent_probe(tmp_path, withheld, clean):
    async def run():
        r = TravelRig(tmp_path, clean=clean)
        try:
            await r.travel(); install_hud(r, target_health=None)
            if withheld == 'forward_binding':
                r.controls['forward'].pop('verified_from')
            else:
                original = r.c.hunt.allowed
                r.c.hunt.allowed = lambda action, family: family != 'motion' and original(action, family)
            await r.choose(None)
            call = r.sage.calls[-1]
            assert 'probe_forward' not in call['options']
            assert 'Choose the forward probe' not in call['instructions']
            assert 'currently offered actions' in call['instructions']
            assert 'urgent_state' in call['options']
            if withheld == 'all_motion':
                assert set(call['options']) <= {'target_enemy', 'begin_hunt', 'change_destination', 'urgent_state'}
            assert not (set(r.physical_keys()) - {58, 6})
        finally:
            await r.close()
    asyncio.run(run())


def test_coordinate_visual_same_coordinate_cannot_launder_retry_pages(tmp_path):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); install_hud(r, target_health=None)
            await r.choose(None); await r.choose(None)
            h = r.c.hunt
            state = deepcopy(h.travel_policy['retry_menu'])
            area = dict(h.plan['area'])
            h.choose({**area, 'coordinate': None}, r.world.capture(), 'visual-choice', 1)
            await r.choose(None)
            assert state['task'] == h.travel_policy['retry_menu']['task']
            h.choose(area, r.world.capture(), 'same-coordinate-choice', 1)
            await r.choose(None)
            assert not events(r, 'grind_travel_retry_menu_rearmed')
            assert set(state['presented']) <= set(h.travel_policy['retry_menu']['presented'])
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('initial_visual', [False, True])
def test_explicit_new_coordinate_task_rearms_without_progress_credit(tmp_path, initial_visual):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); install_hud(r, target_health=None)
            h = r.c.hunt
            if initial_visual:
                h.plan['area']['coordinate'] = None
            await exhaust(r)
            assert h.blocked is None  # Navigation waiting does not globally block assessment.
            h.choose({**h.plan['area'], 'coordinate': [10., 20.]}, r.world.capture(), 'explicit-new-destination', 1)
            await r.choose(None)
            assert len(r.sage.calls[-1]['options']) > 6
            assert events(r, 'grind_travel_retry_menu_rearmed')[-1]['reason'] == 'different_destination'
            assert not h.progress_facts and not r.physical_keys()
        finally:
            await r.close()
    asyncio.run(run())


def test_narrowed_menu_keeps_real_exits_and_terminates_coverage(tmp_path):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); install_hud(r, target_health=None)
            r.c.hunt.allowed = lambda action, family: False
            full, pages = await exhaust(r)
            assert full == {'begin_hunt', 'change_destination', 'urgent_state'}
            assert len(pages) == 1 and not r.physical_keys()
        finally:
            await r.close()
    asyncio.run(run())


def test_repeated_actual_precondition_rejection_uses_finite_pages(tmp_path):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); install_hud(r, target_health=None)
            allowed = r.c.hunt.allowed
            current = {'allow': True}
            r.c.hunt.allowed = lambda action, family: current['allow'] and allowed(action, family)
            async def veto():
                current['allow'] = False
            r.sage.hook = veto
            for _ in range(8):
                current['allow'] = True
                # Choose a currently offered motor after the production menu is built.
                async def choose_and_veto():
                    r.sage.answers[-1] = next((name for name, item in r.sage.calls[-1]['options'].items()
                        if item.binding['type'] != 'observe_only'), None)
                    await veto()
                r.sage.hook = choose_and_veto
                result = await r.choose(None)
                if result.status == 'grind_navigation_wait':
                    break
                assert result.status in {'precondition_failed', 'needs_more_evidence'}
            assert result.status == 'grind_navigation_wait' and r.c.hunt.blocked is None
            assert len(events(r, 'grind_navigation_review_started')) <= 2
            assert not r.physical_keys() and r.c.hunt.pending is None
            assert not r.c.hunt.travel_failures
        finally:
            await r.close()
    asyncio.run(run())


def test_guard_rejection_consumes_only_presentation_no_input_or_allowance(tmp_path):
    from sage_wow.agent.cycle import DispatchValidation
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); install_hud(r, target_health=None)
            async def reject(_):
                return DispatchValidation(False, detail='offline fresh guard veto')
            r.c.travel_guard = lambda *args, **kwargs: reject
            async def hook():
                pass
            r.sage.hook = hook
            result = await r.choose('probe_forward')
            assert result.status == 'dispatch_guard_rejected'
            assert r.c.hunt.travel_policy['retry_menu']['pages'] == 0
            result = await r.choose('probe_forward')
            assert result.status == 'dispatch_guard_rejected'
            assert r.c.hunt.travel_policy['retry_menu']['pages'] == 1
            await r.choose(None)
            assert 'probe_forward' not in r.sage.calls[-1]['options']
            assert not r.physical_keys() and r.c.hunt.pending is None
            assert not r.c.hunt.travel_failures and not r.c.hunt.progress_facts
        finally:
            await r.close()
    asyncio.run(run())


def test_fresh_world_assessment_cannot_restart_same_exhausted_menu(tmp_path):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); install_hud(r, target_health=None)
            await exhaust(r)
            state = deepcopy(r.c.hunt.travel_policy['retry_menu'])
            review = deepcopy(r.c.hunt.travel_policy['recovery_review'])
            r.c.pause_focus(); r.c.resume_focus()
            await r.action('world_normal_confirmed')
            assert not r.c.hunt.blocked
            count = len(r.sage.calls)
            result = await r.choose(None)
            assert result.status == 'grind_navigation_wait' and len(r.sage.calls) == count
            assert r.c.hunt.travel_policy['retry_menu'] == state
            assert r.c.hunt.travel_policy['recovery_review'] == review
            assert not r.physical_keys()
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['frame', 'relabel', 'focus', 'visual_redeclare'])
def test_declarations_and_scope_changes_do_not_reopen_covered_options(tmp_path, change):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); install_hud(r, target_health=None)
            h = r.c.hunt
            if change == 'visual_redeclare':
                h.plan['area']['coordinate'] = None
            await r.choose(None); await r.choose(None)
            seen = set(h.travel_policy['retry_menu']['presented'])
            if change in {'relabel', 'visual_redeclare'}:
                area = {**h.plan['area'], 'label': 'Changed label', 'id': 'new-label'}
                h.choose(area, r.world.capture(), 'new-request', 1)
            elif change == 'focus':
                r.c.pause_focus(); r.c.resume_focus()
                await r.action('world_normal_confirmed')
            await r.choose(None)
            options = set(r.sage.calls[-1]['options'])
            assert not seen & (options - {'urgent_state'})
            assert seen <= set(h.travel_policy['retry_menu']['presented'])
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('action', ['turn_left', 'detour_strafe_right'])
def test_linked_changed_question_rearms_presentation_without_renewing_debt(tmp_path, action):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); install_hud(r, target_health=None)
            h = r.c.hunt
            h.action_failures[h.motion_key('backward', 'search')] = 2
            debt = deepcopy(h.action_failures)
            await r.choose(None)
            if action == 'turn_left':
                # Its later page is reached by abstaining on the first small page.
                await r.choose(None)
            await r.action(action)
            assert h.travel_policy.get('retry_menu')
            if action.startswith('detour_'):
                r.position = [10., 10.5]
            await r.choose(None)
            assert len(r.sage.calls[-1]['options']) > 6
            assert h.action_failures == debt and not h.progress_facts
            assert events(r, 'grind_travel_retry_menu_rearmed')[-1]['progress_credit'] is False
            rearmed = len(events(r, 'grind_travel_retry_menu_rearmed'))
            await r.choose(None)
            assert len(r.sage.calls[-1]['options']) <= 6
            assert len(events(r, 'grind_travel_retry_menu_rearmed')) == rearmed
        finally:
            await r.close()
    asyncio.run(run())


def test_unresolved_translation_does_not_rearm_menu(tmp_path):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); install_hud(r, target_health=None)
            await r.choose(None); await r.action('detour_strafe_right')
            await r.choose(None)
            assert r.c.hunt.last_completed_action['status'] == 'unresolved_precision'
            assert len(r.sage.calls[-1]['options']) <= 6
            assert not events(r, 'grind_travel_retry_menu_rearmed')
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('clean', [False, True])
def test_unresolved_chosen_moves_preserve_unchosen_turns_and_destination_exit(tmp_path, clean):
    async def run():
        r = TravelRig(tmp_path, clean=clean)
        try:
            await r.travel(); install_hud(r, target_health=None)
            h = r.c.hunt
            # A primary-destination relocation outside its radius cannot begin
            # local hunting; keep that real policy, as in the live failure.
            h.hunting_progression_by_player_level = {1: {
                'primary_targets': ['Young Boar'], 'area_ids': ['offline_patch']}}
            await r.choose(None)  # Original full question.
            await r.choose(None)  # First concrete page is declined.
            first_page = set(r.sage.calls[-1]['options']) - {'urgent_state'}
            assert h.travel_policy['retry_menu']['presented'] == list(
                name for name in r.sage.calls[-1]['options'] if name != 'urgent_state')

            await r.action('detour_backward_left')
            state = h.travel_policy['retry_menu']
            assert state['pages'] == 2
            assert set(state['presented']) == first_page | {'detour_backward_left'}
            assert {'turn_left', 'turn_right'} <= set(r.sage.calls[-1]['options'])
            r.position = [10.1, 10.1]  # At the uncertainty bound: no rearm/progress.
            await r.action('detour_backward_right')
            assert h.last_completed_action['status'] == 'unresolved_precision'
            assert 'detour_backward_left' not in r.sage.calls[-1]['options']
            assert {'turn_left', 'turn_right'} <= set(r.sage.calls[-1]['options'])
            assert set(state['presented']) == first_page | {'detour_backward_left', 'detour_backward_right'}
            assert state['pages'] == 3 and not state['exhausted']

            r.position = [10.1, 10.0]
            await r.action('change_destination')
            assert h.last_completed_action['status'] == 'unresolved_precision'
            assert {'turn_left', 'turn_right', 'change_destination'} <= set(r.sage.calls[-1]['options'])
            assert not {'detour_backward_left', 'detour_backward_right'} & set(r.sage.calls[-1]['options'])
            assert h.phase == 'choose_area' and h.planning_requested
            assert state['pages'] == 4 and state['exhausted']
            attempts = events(r, 'grind_travel_retry_menu_attempt')
            assert [item['coverage_kind'] for item in attempts] == [
                'no_input_page', 'selected_option', 'selected_option', 'selected_option']
            assert [item['consumed_ids'] for item in attempts[1:]] == [
                ['detour_backward_left'], ['detour_backward_right'], ['change_destination']]
            assert not events(r, 'grind_travel_retry_menu_rearmed')
            assert not h.progress_facts and not h.credited_kills
            assert h.travel_failures  # Real inconclusive action debt remains charged.
        finally:
            await r.close()
    asyncio.run(run())


def test_successful_retry_choices_keep_four_question_cap_with_honest_remaining_options(tmp_path):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); install_hud(r, target_health=None)
            await r.choose(None)
            h = r.c.hunt
            actions = ['probe_forward', 'detour_backward', 'detour_strafe_left', 'detour_strafe_right']
            for number, action in enumerate(actions, 1):
                await r.action(action)
                assert h.travel_policy['retry_menu']['pages'] == number
                assert set(h.travel_policy['retry_menu']['presented']) == set(actions[:number])
                assert not set(actions[:number - 1]) & set(r.sage.calls[-1]['options'])
            calls = len(r.sage.calls)
            result = await r.choose(None)
            assert result.no_input_abstention and len(r.sage.calls) == calls + 1
            assert len(events(r, 'grind_navigation_review_started')) == 1
            assert not set(actions) & set(r.sage.calls[-1]['options'])
            await r.choose(None)
            result = await r.choose(None)
            assert result.status == 'grind_navigation_wait' and len(r.sage.calls) == calls + 2
            assert h.blocked is None
            r.sage.answers.clear()
            state = h.travel_policy['retry_menu']
            assert state['pages'] == 4 and state['exhausted']
            exhausted = events(r, 'grind_travel_retry_menu_exhausted')[-1]
            assert exhausted['exhaustion_reason'] == 'page_limit'
            assert {'turn_left', 'turn_right', 'change_destination'} <= set(exhausted['remaining_ids'])
            assert not set(exhausted['remaining_ids']) & set(state['presented'])
            assert not events(r, 'grind_travel_retry_menu_rearmed')
            assert not h.progress_facts and h.last_completed_action['status'] == 'unresolved_precision'
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interruption', ['focus', 'world', 'relabel', 'visual_redeclare'])
def test_selected_option_debt_survives_interruption_without_discarding_alternatives(tmp_path, interruption):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); install_hud(r, target_health=None)
            await r.choose(None)
            await r.action('detour_strafe_right')
            h = r.c.hunt
            before = deepcopy(h.travel_policy['retry_menu'])
            assert before['presented'] == ['detour_strafe_right'] and before['pages'] == 1
            if interruption == 'focus':
                r.c.pause_focus(); r.c.resume_focus()
                await r.action('world_normal_confirmed')
            elif interruption == 'world':
                h.prior_phase = 'travel'; h.phase = 'ui_recover'; r.c.require_world = True
                await r.action('world_normal_confirmed')
            else:
                area = {**h.plan['area'], 'id': 'renamed', 'label': 'Same physical task'}
                if interruption == 'visual_redeclare':
                    area['coordinate'] = None
                h.choose(area, r.world.capture(), 'fresh-declaration', 1)
            assert h.travel_policy['retry_menu'] == before
            await r.choose(None)
            options = set(r.sage.calls[-1]['options'])
            assert 'detour_strafe_right' not in options
            assert {'detour_strafe_left', 'detour_backward'} <= options
            assert h.travel_policy['retry_menu']['pages'] == 2
            assert not events(r, 'grind_travel_retry_menu_rearmed')
            assert not h.progress_facts
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('clean', [False, True])
def test_urgent_observation_roundtrip_spends_page_without_consuming_or_restoring_options(tmp_path, clean):
    async def run():
        r = TravelRig(tmp_path, clean=clean)
        try:
            await r.travel(); install_hud(r, target_health=None)
            await r.choose(None)
            h = r.c.hunt
            await r.action('urgent_state')
            before = deepcopy(h.travel_policy['retry_menu'])
            assert before['pages'] == 1 and before['presented'] == []
            attempt = events(r, 'grind_travel_retry_menu_attempt')[-1]
            assert attempt['coverage_kind'] == 'selected_option' and attempt['consumed_ids'] == []
            await r.action('recovered_resume')
            assert h.travel_policy['retry_menu'] == before
            assert h.travel_urgent_debt['completed_roundtrips'] == 1
            for page_number in (2, 3, 4):
                await r.choose(None)
                assert h.travel_policy['retry_menu']['pages'] == page_number
                assert len(r.sage.calls[-1]['options']) <= 6
            calls = len(r.sage.calls)
            result = await r.choose(None)
            assert result.no_input_abstention and len(r.sage.calls) == calls + 1
            assert len(events(r, 'grind_navigation_review_started')) == 1
            await r.choose(None)
            result = await r.choose(None)
            assert result.status == 'grind_navigation_wait' and len(r.sage.calls) == calls + 2
            assert h.blocked is None
            r.sage.answers.clear()
            assert h.travel_policy['retry_menu']['pages'] == 4
            assert not events(r, 'grind_travel_retry_menu_rearmed')
            assert not h.progress_facts and not (set(r.physical_keys()) - {58, 6})
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('invalid_receipt', ['noncurrent', 'incomplete', 'unknown', 'error'])
def test_dispatched_status_without_accepted_current_receipt_cannot_preserve_other_choices(tmp_path, invalid_receipt):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); install_hud(r, target_health=None)
            await r.choose(None)
            decide = r.c.decide
            async def corrupt_result(*args, **kwargs):
                result = await decide(*args, **kwargs)
                receipt = deepcopy(result.receipt)
                assert receipt['possible_input'] and receipt['completed']
                if invalid_receipt == 'noncurrent':
                    receipt['receipt_id'] = 'not-the-current-completed-receipt'
                else:
                    if invalid_receipt == 'incomplete': receipt['completed'] = False
                    elif invalid_receipt == 'unknown': receipt['dispatch_unknown'] = True
                    else: receipt['error'] = 'offline execution failure'
                    r.c.cycle._last_receipt = deepcopy(receipt)
                return replace(result, receipt=receipt)
            r.c.decide = corrupt_result
            await r.action('probe_forward')
            assert r.c.stopped and r.c.reason == 'partial_or_unknown_grind_input'
            h = r.c.hunt
            assert h.pending is None and h.travel_policy['retry_menu']['pages'] == 1
            options = set(r.sage.calls[-1]['options']) - {'urgent_state'}
            assert set(h.travel_policy['retry_menu']['presented']) == options
            attempt = events(r, 'grind_travel_retry_menu_attempt')[-1]
            assert attempt['coverage_kind'] == 'unaccepted_dispatch_page'
            assert not h.progress_facts
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('unknown', ['health', 'confidence', 'stale_hud', 'low_health'])
def test_generic_recovery_cannot_bypass_pages_when_current_readiness_is_missing(tmp_path, unknown):
    async def run():
        r = TravelRig(tmp_path)
        try:
            await r.travel(); hud, _ = install_hud(r, target_health=None)
            await r.choose(None); await r.choose(None)
            h = r.c.hunt
            before = deepcopy(h.travel_policy['retry_menu'])
            h.compact_stage = 'recovery'
            if unknown == 'health': hud['player_health'] = None
            elif unknown == 'confidence': hud['health_confidence'] = .1
            elif unknown == 'stale_hud': hud['frame_id'] = 'old-frame'
            else:
                hud['player_health'] = max(r.c.config['critical_health_threshold'], r.c.config['heal_health_fraction']) / 2
                # Stable low resources, not freshly measured incoming damage.
                # Authenticated health loss rightly has higher-priority actions.
                h.threat_health_observation['health'] = hud['player_health']
            await r.choose(None)
            options = set(r.sage.calls[-1]['options'])
            assert not options & {'target_enemy', 'forward', 'backward', 'turn_left', 'turn_right', 'strafe_left', 'strafe_right'}
            assert h.travel_policy['retry_menu'] == before
            assert not r.physical_keys()
            hud.update(player_health=1., health_confidence=1.)
            hud.pop('frame_id', None)
            await r.choose(None)
            options = set(r.sage.calls[-1]['options'])
            assert len(options) <= 6 and 'urgent_state' in options
            assert not (options-{'urgent_state'}) & set(before['presented'])
            assert not r.physical_keys() and not h.progress_facts
        finally:
            await r.close()
    asyncio.run(run())
