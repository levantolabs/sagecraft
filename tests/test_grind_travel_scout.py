"""Travel scouting through the real controller; only fake capture/input/provider."""
from test_grind_product_spec import retain_legacy_no_effect
import asyncio
from copy import deepcopy
from datetime import datetime

import pytest

from sage_wow.agent import grind_travel_scout as scout
from test_grind_travel_acceptance import TravelRig
from test_grind_acquisition_opening import enable, process, smites
from test_active_attacker_recovery import native_fact


async def setup(tmp_path, monkeypatch, *, opening=False):
    r = TravelRig(tmp_path)
    if opening:
        enable(r)
    await r.travel()
    values = {'player_health': 1., 'health_confidence': 1., 'target_health': None,
        'target_health_confidence': 1., 'player_mana': 1.}
    def bars(c, frame):
        return {'frame_id': frame.frame_id, **values}
    monkeypatch.setattr('sage_wow.agent.grind_resources.hud_resources', bars)
    original = r.c.target_proposal
    async def target(frame):
        return {**await original(frame), 'hud': bars(r.c, frame)}
    r.c.target_proposal = target
    return r, values


async def acquire(r):
    # This is the real UI/provider-recovery caller, before any scout exists.
    r.c.hunt.phase = 'search'; r.c.hunt.compact_stage = 'acquire'
    return await r.choose('target_enemy')


def test_shared_first_scout_installs_target_and_absence_preserves_plan_and_debt(tmp_path, monkeypatch):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            h = r.c.hunt; plan = deepcopy(h.plan)
            h.action_failures['retained-method'] = 2
            result = await acquire(r)
            record = h.travel_policy['last_scout']
            assert record['source_frame_id'] == result.receipt['dispatch_frame_id']
            assert record['source_frame_id'] != result.receipt['source_frame_id']
            assert h.pending['source_frame_id'] == result.receipt['source_frame_id']
            assert h.pending['family'] == 'target' and h.pending['action'] == 'target_enemy'
            assert h.phase == 'search' and h.compact_stage == 'inspect' and h.suspended
            assert h.plan == plan and scout.active(r.c)
            await r.choose('no_selected_frame')
            assert h.phase == 'travel' and h.compact_stage == 'acquire' and not h.suspended
            assert not scout.active(r.c) and h.plan == plan and h.failures['target'] == 1
            assert h.action_failures['retained-method'] == 2
            h.active_seconds += 60
            h.phase = 'search'; h.compact_stage = 'acquire'
            await r.choose(None)
            assert 'target_enemy' not in r.sage.calls[-1]['options']
            assert r.physical_keys().count(r.controls['target_enemy']['keycode']) == 1
        finally:
            await r.close()
    asyncio.run(run())


def test_disabled_reinspection_unknown_archive_keeps_task_ahead_of_travel_and_planning(tmp_path, monkeypatch):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r); h = r.c.hunt; receipt = h.pending['receipt']['receipt_id']
            h.target_bands_by_player_level = {1: [1, 1]}
            await r.choose('cannot_assess')
            await r.choose('cannot_assess')
            assert not (r.c.config.get('selected_target_observer') or {}).get('enabled')
            assert 'reinspect_selected_frame' not in r.sage.calls[-1]['options']
            assert h.pending is None and scout.active(r.c)
            assert [o['outcome'] for o in h.outcomes if o['receipt_id'] == receipt] == ['unknown']
            h.phase = 'travel'; h.compact_stage = 'acquire'; h.confirmed_target_absences = 4
            h.active_seconds += 60
            await r.choose(None)
            assert h.phase == 'search' and not h.planning_requested and h.strategy_required is None
            assert 'target_enemy' not in r.sage.calls[-1]['options']
            assert 'probe_forward' not in r.sage.calls[-1]['options']
            await r.choose('no_selected_frame')
            assert h.phase == 'travel' and not scout.active(r.c)
            assert [o['outcome'] for o in h.outcomes if o['receipt_id'] == receipt] == ['unknown']
        finally:
            await r.close()
    asyncio.run(run())


def test_focus_archives_authority_but_current_absence_finishes_assessment(tmp_path, monkeypatch):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch, opening=True)
        try:
            await acquire(r); h = r.c.hunt; receipt = h.pending['receipt']['receipt_id']
            assert r.c.target_opener
            r.c.pause_focus(); r.c.resume_focus()
            assert h.pending is None and r.c.target_opener is None and scout.active(r.c)
            await r.choose('world_normal_confirmed')
            assert h.phase == 'search'
            await r.choose('no_selected_frame')
            assert h.phase == 'travel' and not smites(r) and not scout.active(r.c)
            outcomes = [o for o in h.outcomes if o['receipt_id'] == receipt]
            assert len(outcomes) == 1 and outcomes[0]['outcome'] == 'unknown'
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['health', 'mana', 'target', 'world', 'recovery', 'loot', 'encounter'])
def test_final_guard_rechecks_current_scout_predicates_before_tab(tmp_path, monkeypatch, change):
    async def run():
        r, values = await setup(tmp_path, monkeypatch)
        try:
            r.c.hunt.phase = 'search'; r.c.hunt.compact_stage = 'acquire'
            async def change_after_offer():
                if change == 'health': values['player_health'] = .2
                elif change == 'mana': values['player_mana'] = 0.
                elif change == 'target': r.world.name = 'Young Wolf'
                elif change == 'world': r.c.require_world = True
                elif change == 'recovery': r.c.hunt.phase = 'recover'
                elif change == 'loot': r.c.hunt.loot_request = {'pending': True}
                else: r.c.hunt.encounter_ended = False
            r.sage.hook = change_after_offer
            result = await r.choose('target_enemy')
            assert result.status != 'dispatched'
            assert not r.physical_keys() and not scout.active(r.c)
            assert 'last_scout' not in r.c.hunt.travel_policy
        finally:
            await r.close()
    asyncio.run(run())


def test_final_guard_patch_is_consumed_instead_of_older_offer_coordinates(tmp_path, monkeypatch):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            async def move_before_guard(): r.position = [10.4, 10.]
            r.sage.hook = move_before_guard
            result = await acquire(r)
            assert result.status == 'dispatched'
            record = r.c.hunt.travel_policy['last_scout']
            assert record['position'] == [10.4, 10.]
            assert r.c.hunt.pending['measurement']['position'] == [10., 10.]
            assert record['source_frame_id'] == result.receipt['dispatch_frame_id']
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('missing', ['position', 'zone', 'live_encounter', 'same_name_live', 'selected_exit'])
def test_first_optional_scout_requires_readable_idle_source(tmp_path, monkeypatch, missing):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            h = r.c.hunt
            if missing == 'position': r.position = None
            elif missing == 'zone': r.zone = ''
            elif missing in {'live_encounter', 'same_name_live'}:
                h.encounter_ended = False
                if missing == 'same_name_live': h.last_target = 'young wolf'; h.encounter = 1
            else: h.strategy_required = {'selected_exit': {'request_id': 'retained'}, 'captured_at': r.world.capture().captured_at}
            h.phase = 'search'; h.compact_stage = 'acquire'
            await r.choose(None)
            assert 'target_enemy' not in r.sage.calls[-1]['options'] and not r.physical_keys()
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('archive_clear', [False, True])
def test_unsuitable_clear_retains_return_plan_without_automatic_strategy(tmp_path, monkeypatch, archive_clear):
    async def run():
        r, values = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r); h = r.c.hunt; plan = deepcopy(h.plan)
            r.world.name = 'Sten'; r.world.target_level = 5; values['target_health'] = 1.
            await r.choose('reject_selected_target')
            clear = h.pending['receipt']['receipt_id']
            assert h.pending['family'] == 'clear' and h.strategy_required is None
            assert h.plan == plan and not h.planning_requested and scout.active(r.c)
            if archive_clear:
                r.c.pause_focus(); r.c.resume_focus(); await r.choose('world_normal_confirmed')
            r.world.name = ''; r.world.target_level = None; values['target_health'] = None
            await r.choose('no_selected_frame' if archive_clear else 'target_cleared')
            assert h.phase == 'travel' and h.plan == plan and not h.planning_requested
            outcomes = [o['outcome'] for o in h.outcomes if o['receipt_id'] == clear]
            assert outcomes == ['unknown' if archive_clear else 'target_cleared']
        finally:
            await r.close()
    asyncio.run(run())


def test_primary_opener_transfers_to_normal_fight_exactly_once(tmp_path, monkeypatch):
    async def run():
        r, values = await setup(tmp_path, monkeypatch, opening=True)
        try:
            tab = await acquire(r)
            r.world.name = 'Young Wolf'; values['target_health'] = 1.
            result = await process(r)
            assert result.status == 'dispatched' and result.decision is None
            assert len(smites(r)) == 1 and result.receipt['authorization_id'] == tab.receipt['receipt_id']
            assert not scout.active(r.c) and r.c.hunt.plan['phase'] == 'search'
            assert r.c.hunt.phase == 'fight' and r.c.hunt.pending['family'] == 'combat'
            await r.choose('damaged_alive')
            assert len(smites(r)) == 1
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('clean', [False, True])
def test_ordinary_travel_offers_same_guarded_scout_in_both_hud_modes(tmp_path, monkeypatch, clean):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            r.c.config['clean_world_observation'] = clean
            result = await r.choose('target_enemy')
            assert result.status == 'dispatched' and scout.active(r.c)
            assert r.c.hunt.pending['family'] == 'target' and r.hud
            assert len(r.sage.calls[-1]['options']) <= 14
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('movement,endpoint,expected', [
    ('probe_forward', [10.5, 10.], True), ('probe_forward', [10.1, 10.], False),
    ('probe_forward', [10., 10.], False), ('turn_left', [10., 10.], False)])
@pytest.mark.parametrize('clean', [False, True])
def test_only_current_resolved_post_scout_translation_renews_travel_selection(
        tmp_path, monkeypatch, movement, endpoint, expected, clean):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r); await r.choose('no_selected_frame')
            r.c.config['clean_world_observation'] = clean
            h = r.c.hunt; revision = h.search_revision
            prior = deepcopy(h.travel_policy['last_scout'])
            await r.choose(movement); r.position = endpoint
            await r.choose(None)
            assert ('target_enemy' in r.sage.calls[-1]['options']) is expected
            assert h.search_revision == revision and h.travel_policy['last_scout'] == prior
            if expected:
                origin = h.last_completed_action['search_origin']
                assert datetime.fromisoformat(origin['source_captured_at']) > datetime.fromisoformat(prior['completed_at'])
                await r.choose('target_enemy')
                assert h.travel_policy['last_scout']['position'] == endpoint
        finally:
            await r.close()
    asyncio.run(run())


def test_fresh_chain_after_focus_gap_qualifies_without_claiming_the_gap(tmp_path, monkeypatch):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r); await r.choose('no_selected_frame')
            await r.choose('probe_forward'); r.position = [10.5, 10.]
            await r.choose(None)
            r.c.pause_focus(); r.c.resume_focus(); await r.choose('world_normal_confirmed')
            await r.choose('probe_forward'); r.position = [11., 10.]
            await r.choose(None)
            move = r.c.hunt.last_completed_action
            assert move['search_origin']['position'] == [10.5, 10.]
            assert 'target_enemy' in r.sage.calls[-1]['options']
            assert move['search_origin']['position'] != r.c.hunt.travel_policy['last_scout']['position']
            assert move['session_epoch'] == r.c.cycle.session_epoch
        finally:
            await r.close()
    asyncio.run(run())


def test_last_patch_floor_allows_honest_long_revisit_without_global_novelty_claim(tmp_path, monkeypatch):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r); await r.choose('no_selected_frame')  # A
            await r.choose('probe_forward'); r.position = [10.5, 10.]
            await r.choose('target_enemy'); await r.choose('no_selected_frame')  # B
            r.c.hunt.active_seconds += 16
            await r.choose('detour_backward'); r.position = [10., 10.]
            await r.choose('target_enemy')  # A again, relative to most recent B.
            assert r.c.hunt.travel_policy['last_scout']['position'] == [10., 10.]
            assert r.physical_keys().count(r.controls['target_enemy']['keycode']) == 3
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('previous_scout', [False, True])
def test_actual_current_attacker_retains_ordinary_targeting_despite_travel_floor(tmp_path, monkeypatch, previous_scout):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            if previous_scout:
                await acquire(r); await r.choose('no_selected_frame')
            prior = deepcopy(r.c.hunt.travel_policy.get('last_scout'))
            r.c.combat_log_facts = [native_fact(own=False, event='SWING_DAMAGE')]
            result = await r.choose('target_enemy')
            assert result.status == 'dispatched' and r.c.hunt.pending['family'] == 'target'
            assert r.c.hunt.travel_policy.get('last_scout') == prior
            assert not scout.active(r.c)
        finally:
            await r.close()
    asyncio.run(run())


def test_retired_threat_cannot_dispatch_a_tab_around_consumed_scout_floor(tmp_path, monkeypatch):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r); await r.choose('no_selected_frame')
            r.c.combat_log_facts = [native_fact(own=False, event='SWING_DAMAGE')]
            before = list(r.physical_keys())
            async def retire():
                r.c.combat_log_facts = []; r.c.hunt.active_threat = None
            r.sage.hook = retire
            result = await r.choose('target_enemy')
            assert result.status != 'dispatched' and r.physical_keys() == before
        finally:
            await r.close()
    asyncio.run(run())


def test_fresh_health_loss_during_scout_guard_vetoes_optional_input(tmp_path, monkeypatch):
    async def run():
        r, values = await setup(tmp_path, monkeypatch)
        try:
            async def take_damage(): values['player_health'] = .8
            r.sage.hook = take_damage
            result = await acquire(r)
            assert result.status != 'dispatched' and not r.physical_keys()
            assert r.c.hunt.active_threat['kind'] == 'current_own_health_loss'
        finally:
            await r.close()
    asyncio.run(run())


def test_later_sage_strategy_choice_retires_return_and_preserves_unknown_effect(tmp_path, monkeypatch):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r); h = r.c.hunt; receipt = h.pending['receipt']['receipt_id']
            h.recovery_requested = {'reason': 'unresolved_inspection'}
            await r.choose('change_search_strategy')
            assert not scout.active(r.c) and h.plan['phase'] == 'search'
            assert h.phase == 'choose_area' and h.planning_requested
            assert [o['outcome'] for o in h.outcomes if o['receipt_id'] == receipt] == ['unknown']
            await r.choose('explore_visible')
            assert h.plan['phase'] == 'travel' and h.phase == 'travel'
            assert h.travel_policy['last_scout']['resolution'] == 'sage_strategy_choice'
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('restoration', ['world', 'resources', 'focus'])
def test_search_owned_plan_does_not_restore_to_travel_from_stale_alias(tmp_path, monkeypatch, restoration):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            h = r.c.hunt; h.plan['phase'] = 'search'; h.phase = 'search'; h.compact_stage = 'acquire'
            if restoration == 'resources':
                h.phase = 'recover'; h.prior_phase = 'travel'
                await r.choose('recovered_resume')
            else:
                if restoration == 'focus':
                    h.phase = 'travel'; r.c.pause_focus(); r.c.resume_focus()
                else:
                    h.phase = 'ui_recover'; h.prior_phase = 'travel'; r.c.require_world = True
                await r.choose('world_normal_confirmed')
            assert h.phase == 'search' and h.plan['phase'] == 'search'
            await r.choose('target_enemy')
            assert 'last_scout' not in h.travel_policy  # Ordinary local hunting.
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('preexisting_strategy', [False, True])
def test_both_rejection_policies_keep_sighting_and_existing_strategy_without_replanning(
        tmp_path, monkeypatch, preexisting_strategy):
    async def run():
        r, values = await setup(tmp_path, monkeypatch)
        try:
            h = r.c.hunt; h.target_bands_by_player_level = {1: [1, 1]}
            h.hunting_progression_by_player_level = {1: {'primary_targets': ['Young Boar'], 'area_ids': ['offline_patch']}}
            if preexisting_strategy:
                h.strategy_required = {'reason': 'level_progression', 'level': 1,
                    'frame_id': 'earlier-strategy', 'captured_at': r.world.capture().captured_at,
                    'search_revision': h.search_revision}
            before_strategy = deepcopy(h.strategy_required)
            original = r.c.target_proposal
            async def observed(frame):
                target = await original(frame)
                return {**target, 'visual_observation': {
                    'selected_hud': 'present' if target['name'] else 'absent', 'name': target['name'],
                    'level': target['levels'][0] if target['levels'] else None,
                    'target_kind': 'creature', 'life_state': 'alive'},
                    'visual_provenance': {'frame_id': frame.frame_id, 'actual_model': 'explicit-offline-fake'}}
            r.c.target_proposal = observed
            await acquire(r)
            r.world.name = 'Young Wolf'; r.world.target_level = 5; values['target_health'] = 1.
            h.progression_pending = True
            await r.choose('reject_selected_target')
            assert h.scouting_observations[-1]['level'] == 5
            assert h.strategy_required == before_strategy and not h.planning_requested
            assert h.progression_pending and h.pending['family'] == 'clear'
            r.world.name = ''; r.world.target_level = None; values['target_health'] = None
            await r.choose('target_cleared')
            assert h.phase == 'travel' and h.strategy_required == before_strategy
            assert h.progression_pending and not h.planning_requested
        finally:
            await r.close()
    asyncio.run(run())


def test_failed_clear_keeps_its_debt_and_unfinished_inspection_without_another_tab(tmp_path, monkeypatch):
    async def run():
        r, values = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r)
            r.world.name = 'Sten'; r.world.target_level = 5; values['target_health'] = 1.
            for _ in range(2):
                await r.choose('reject_selected_target'); await r.choose('clear_failed')
            h = r.c.hunt; assert h.failures['clear'] == 2 and scout.active(r.c)
            h.active_seconds += 60
            await r.choose(None)
            options = r.sage.calls[-1]['options']
            assert not {'target_enemy', 'reject_selected_target', 'probe_forward'} & options.keys()
            assert h.phase == 'search' and not h.planning_requested and h.failures['clear'] == 2
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('cue', ['visual_name', 'visual_level', 'target_bar', 'self', 'invalid', 'conflict'])
def test_archived_scout_with_current_positive_unknown_cues_cannot_claim_absence_or_resume(
        tmp_path, monkeypatch, cue):
    async def run():
        r, values = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r); h = r.c.hunt; receipt = h.pending['receipt']['receipt_id']
            h.archive_pending('offline_simulated_receipt_link_lost')
            original = r.c.target_proposal
            async def ambiguous(frame):
                target = await original(frame)
                visual = {'selected_hud': 'unknown', 'name': '', 'level': None,
                    'life_state': 'unknown', 'target_kind': 'unknown'}
                if cue == 'visual_name': visual['name'] = 'Young Wolf'
                elif cue == 'visual_level': visual['level'] = 1
                elif cue == 'self': target['self_target'] = True
                elif cue == 'invalid': target['invalid_text'] = True
                elif cue == 'conflict': target['observation_conflict'] = True
                return {**target, 'visual_observation': visual}
            r.c.target_proposal = ambiguous
            if cue == 'target_bar': values['target_health'] = .7
            await r.choose(None)
            assert 'no_selected_frame' not in r.sage.calls[-1]['options']
            assert scout.active(r.c) and h.phase == 'search' and h.pending is None
            assert h.selected_presence is None and not h.planning_requested
            assert [o['outcome'] for o in h.outcomes if o['receipt_id'] == receipt] == ['unknown']
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['phase', 'strategy', 'coordinates', 'zone'])
def test_guard_await_cannot_keep_old_task_or_unreadable_patch_authority(tmp_path, monkeypatch, change):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            original = r.c.target_proposal
            offered = False
            async def hook():
                nonlocal offered
                offered = True
                # Missing facts must be missing in the captured guard frame,
                # not only in a later unobserved fake-world state.
                if change == 'coordinates': r.position = None
                elif change == 'zone': r.zone = ''
            async def changed(frame):
                target = await original(frame)
                await asyncio.sleep(0)
                if offered:
                    if change == 'phase': r.c.hunt.phase = 'ui_recover'
                    elif change == 'strategy': r.c.hunt.strategy_required = {'reason': 'later_task'}
                return target
            r.c.target_proposal = changed; r.sage.hook = hook
            result = await acquire(r)
            assert result.status != 'dispatched' and not r.physical_keys()
            assert 'last_scout' not in r.c.hunt.travel_policy
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('invalid', ['old_origin', 'epoch', 'generation', 'search_revision', 'hash', 'receipt', 'revisit'])
def test_corrupted_or_pre_scout_chain_never_renews_allowance(tmp_path, monkeypatch, invalid):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r); await r.choose('no_selected_frame')
            await r.choose('probe_forward'); r.position = [10.5, 10.]
            await r.choose(None)
            h = r.c.hunt; move = h.last_completed_action
            if invalid == 'old_origin': move['search_origin']['source_captured_at'] = h.travel_policy['last_scout']['source_captured_at']
            elif invalid == 'epoch': move['session_epoch'] = 'old-session'
            elif invalid == 'generation': r.c.cycle._input_generation += 1
            elif invalid == 'search_revision': move['search_revision'] -= 1
            elif invalid == 'hash': move['after_hash'] = 'modified'
            elif invalid == 'receipt': move['receipt_id'] = 'unlinked'
            else: move['revisitation_cycle_suspected'] = True
            from sage_wow.agent.grind_search import measure
            frame = r.world.capture()
            observation = measure(r.c.profile, frame, await r.c.rows(frame), r.c.ocr)
            assert not scout.allowance(r.c, frame, observation)
            # Ordinary recovery caller cannot bypass the same proof predicate.
            h.phase = 'search'; h.compact_stage = 'acquire'
            await r.choose(None)
            assert 'target_enemy' not in r.sage.calls[-1]['options']
            assert r.physical_keys().count(r.controls['target_enemy']['keycode']) == 1
        finally:
            await r.close()
    asyncio.run(run())


def test_current_absence_finishes_archived_scout_then_actual_threat_can_target(tmp_path, monkeypatch):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r); h = r.c.hunt
            h.archive_pending('interrupted_scout')
            r.c.combat_log_facts = [native_fact(own=False, event='SWING_DAMAGE')]
            await r.choose('no_selected_frame')
            assert not scout.active(r.c) and h.phase == 'search' and h.plan['phase'] == 'travel'
            record = deepcopy(h.travel_policy['last_scout'])
            await r.choose('target_enemy')
            assert h.pending['family'] == 'target' and h.travel_policy['last_scout'] == record
        finally:
            await r.close()
    asyncio.run(run())


def test_legacy_resume_destination_restores_plan_ownership_without_renewing_floor(tmp_path, monkeypatch):
    """The accepted handler exists even though no current menu emits this name."""
    async def run():
        from sage_wow.agent.grind_search import measure
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r)
            resolved = await r.choose('no_selected_frame')
            h = r.c.hunt; h.plan['phase'] = 'search'; h.phase = 'search'
            record = deepcopy(h.travel_policy['last_scout'])
            frame = r.world.capture(); target = await r.c.target_proposal(frame)
            measurement = measure(r.c.profile, frame, await r.c.rows(frame), r.c.ocr)
            await r.c.apply_choice(frame, target, measurement, None, False, resolved,
                {'resume_destination': True}, 'resume_destination', 1)
            assert h.plan['phase'] == 'travel' and h.phase == 'travel'
            assert h.travel_policy['last_scout'] == record
            h.phase = 'search'; h.compact_stage = 'acquire'; h.active_seconds += 60
            await r.choose(None)
            assert 'target_enemy' not in r.sage.calls[-1]['options']
        finally:
            await r.close()
    asyncio.run(run())


def test_same_name_ended_history_is_retained_and_only_existing_acquisition_renews_attempt(tmp_path, monkeypatch):
    async def run():
        r, values = await setup(tmp_path, monkeypatch, opening=True)
        try:
            await acquire(r); r.world.name = 'Young Wolf'; values['target_health'] = 1.
            await process(r); await retain_legacy_no_effect(r)
            h = r.c.hunt; owner = h.combat_history_key
            old_history = deepcopy(h.target_history[owner])
            h.encounter_ended = True; h.plan['phase'] = 'travel'
            r.world.name = ''; values['target_health'] = None
            # Actual local arrival/hunt transfer already consumed the first
            # scout at A. Travel B supplies the next independently measured patch.
            h.phase = 'travel'; await r.choose('probe_forward'); r.position = [10.5, 10.]
            await r.choose('target_enemy')
            assert h.target_history[owner]['cast_attempted'] == old_history['cast_attempted']
            assert h.target_history[owner].get('retired_by_acquisition') is None
            r.world.name = 'Young Wolf'; values['target_health'] = 1.
            await process(r)
            assert h.target_history[owner].get('retired_by_acquisition')
            assert len(smites(r)) == 2 and h.plan['phase'] == 'search'
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('turn', [False, True])
def test_verified_clean_observations_transport_search_facts_without_restoring_heading(tmp_path, monkeypatch, turn):
    async def run():
        from sage_wow.agent.grind_search import measure
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r); await r.choose('no_selected_frame')
            await r.choose('probe_forward'); r.position = [10.5, 10.]
            # Resolve this movement while Sage chooses an optional turn; this
            # avoids a null page being mistaken for physical movement evidence.
            await r.choose('turn_left' if turn else None)
            r.c.config['clean_world_observation'] = True
            for _ in range(2):
                await r.choose(None)
                move = r.c.hunt.last_completed_action
                frame = r.world.capture()
                observation = measure(r.c.profile, frame, await r.c.rows(frame), r.c.ocr)
                assert scout.allowance(r.c, frame, observation)
                assert move['search_generation'] == r.c.cycle.input_generation
                assert move['generation_after'] < move['search_generation']
                if turn: assert r.c.hunt.mapping_generation is None
            assert r.physical_keys().count(r.controls['target_enemy']['keycode']) == 1
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('break_chain', ['external_generation', 'unknown_hud_input'])
def test_unmatched_or_failed_clean_transaction_does_not_transport_search_evidence(tmp_path, monkeypatch, break_chain):
    async def run():
        r, _ = await setup(tmp_path, monkeypatch)
        try:
            await acquire(r); await r.choose('no_selected_frame')
            await r.choose('probe_forward'); r.position = [10.5, 10.]
            await r.choose(None)
            move = r.c.hunt.last_completed_action
            before = move.get('search_generation', move['generation_after'])
            r.c.config['clean_world_observation'] = True
            if break_chain == 'external_generation': r.c.cycle._input_generation += 1
            else: r.fail_toggle = r.toggle_count + 1
            result = await r.choose(None)
            assert move.get('search_generation', move['generation_after']) == before
            assert move.get('search_generation', move['generation_after']) != r.c.cycle.input_generation
            if break_chain == 'unknown_hud_input': assert result.status != 'dispatched'
            else: assert 'target_enemy' not in r.sage.calls[-1]['options']
        finally:
            await r.close()
    asyncio.run(run())
