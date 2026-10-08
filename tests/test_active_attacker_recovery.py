"""Mixed cast evidence and unfinished active fights; explicit offline fakes only.

These are gameplay-behavior regressions, not evidence of live leveling success.
The repository autouse guard disables native capture/input and nonlocal network.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import time

import pytest

from test_grind_product_spec import Rig
from test_grind_committed_combat import add_current_observer


async def start(r):
    r.c.config.update(committed_combat=True, smite_burst_count=1)
    r.controls.update({name: {'keycode': code, 'verified_from': 'offline operator'}
                       for name, code in [('strafe_left', 100), ('strafe_right', 101)]})
    await r.start()
    add_current_observer(r)


async def facing_failure(r):
    await r.choose('attack_mob_level_1')
    cast = deepcopy(r.c.hunt.pending)
    r.world.error = 'Target needs to be in front of you'
    await r.choose('position_error')
    assert r.c.hunt.cast_error['status'] == 'active'
    r.world.error = ''
    return cast


async def exhaust_turns_and_fail_clear(r):
    cast = await facing_failure(r)
    for direction in ('turn_left', 'turn_left', 'turn_right', 'turn_right'):
        await r.choose(direction)
        await r.choose('motion_no_useful_effect')
    await r.choose('reject_selected_target')
    await r.choose('clear_failed')
    assert r.c.hunt.cast_obligation and r.world.name == 'Young Wolf'
    return cast


async def facing_dead_end(r):
    """Two unassessed left pulses, two failed right pulses, two failed clears."""
    await facing_failure(r)
    for _ in range(2):
        await r.choose('turn_left')
        await r.choose(None)
        await r.choose(None)
    for _ in range(2):
        await r.choose('turn_right')
        await r.choose('motion_no_useful_effect')
    for _ in range(2):
        await r.choose('reject_selected_target')
        await r.choose('clear_failed')
    r.c.combat_log_facts = [native_fact(event='SWING_DAMAGE', own=False)]


def test_exhausted_short_turns_keep_broad_correction_and_resume_rejected_fight(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            hud, _ = install_hud(r)
            await facing_dead_end(r)
            h = r.c.hunt
            owner, encounter = h.approach['history_key'], h.encounter
            failures, unresolved = deepcopy(h.action_failures), deepcopy(h.unresolved_motion)
            await r.choose('turn_around')
            options = r.sage.calls[-1]['options']
            assert not {'turn_left', 'turn_right', 'reject_selected_target', 'attack_mob_level_1'} & options.keys()
            assert options['turn_around'].binding == {'type': 'keypress',
                'keycode': r.controls['turn_right']['keycode'], 'hold_seconds': 1.5}
            assert h.pending['action'] == 'turn_around' and h.pending['purpose'] == 'cast_correction'
            assert h.cast_obligation and len(smites(r)) == 1
            correction_receipt = h.pending['receipt']['receipt_id']
            await r.choose('motion_useful')
            r.c.config['smite_burst_count'] = 3
            await r.choose('attack_mob_level_1')
            assert r.sage.calls[-1]['options']['attack_mob_level_1'].binding['type'] == 'cast_guarded'
            assert h.target_history[owner]['rejection_reassessment']['correction_receipt_id'] == correction_receipt
            assert h.target_history[owner]['rejections'] == 2
            assert h.action_failures == failures and h.unresolved_motion == unresolved
            assert h.encounter == encounter and h.approach['history_key'] == owner
            assert len(smites(r)) == 2
            hud['target_health'] = .4
            await r.choose('damaged_alive')
            r.c.config['smite_burst_count'] = 1
            await r.choose('attack_mob_level_1')
            hud['target_health'] = 0.
            await r.choose('dead')
            assert h.target_dead_observed and len(smites(r)) == 3
            assert not h.credited_kills and not h.own_damage_evidence
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('outcome', ['failed', 'unknown'])
def test_broad_correction_is_not_replenished_by_frames_ui_or_healing(tmp_path, monkeypatch, outcome):
    from sage_wow.agent import grind_resources

    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            hud, bars = install_hud(r)
            monkeypatch.setattr(grind_resources, 'hud_resources', bars)
            r.c.config.update(encounter_resources=True, preserve_target_heal=True)
            await facing_dead_end(r)
            h = r.c.hunt
            owner = h.approach['history_key']
            await r.choose('turn_around')
            if outcome == 'failed':
                await r.choose('motion_no_useful_effect')
            else:
                await r.choose(None)
                await r.choose(None)
            assert h.motion_count('turn_around', 'cast_correction', owner) == 1
            failures, unresolved = deepcopy(h.action_failures), deepcopy(h.unresolved_motion)
            await r.choose('ui_blocked')
            await r.choose('world_normal_confirmed')
            hud['player_health'] = .29
            await r.choose('heal_self')
            hud['player_health'] = .85
            r.c.wait_until = 0
            await r.c.process(r.world.capture())
            for _ in range(3):
                r.c.combat_log_facts = [native_fact(event='SWING_DAMAGE', own=False)]
                await r.choose(None)
                assert not {'turn_around', 'turn_left', 'turn_right', 'attack_mob_level_1'} & r.sage.calls[-1]['options'].keys()
            assert h.action_failures == failures and h.unresolved_motion == unresolved
            assert h.cast_obligation and not h.retry_credit
            assert len(smites(r)) == 1 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['target', 'input_generation', 'focus'])
def test_broad_correction_uses_current_dispatch_authority(tmp_path, change):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            install_hud(r)
            await facing_dead_end(r)
            before = inputs(r)

            async def invalidate():
                if change == 'target': r.world.name = 'Different Wolf'
                elif change == 'input_generation': r.c.cycle._input_generation += 1
                else: r.c.pause_focus()

            r.sage.hook = invalidate
            result = await r.choose('turn_around')
            assert result.status != 'dispatched'
            assert inputs(r) == before
            assert len(smites(r)) == 1 and not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['new_input', 'new_selection', 'new_encounter', 'ungranted'])
def test_rejected_target_retry_needs_current_linked_useful_correction(tmp_path, change):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            install_hud(r)
            await facing_dead_end(r)
            await r.choose('turn_around')
            await r.choose('motion_useful')
            h = r.c.hunt
            if change == 'new_input': r.c.cycle._input_generation += 1
            elif change == 'new_selection': h.selection_revision += 1
            elif change == 'new_encounter': h.encounter += 1
            else: h.correction_evidence['granted'] = False
            before = inputs(r)
            await r.choose(None)
            assert 'attack_mob_level_1' not in r.sage.calls[-1]['options']
            assert inputs(r) == before and len(smites(r)) == 1
            assert h.target_history[h.approach['history_key']]['rejections'] == 2
        finally:
            await r.close()
    asyncio.run(run())


def smites(r):
    return [event for event in r.backend.events
            if event[0] == 'text' and event[1].startswith('/cast ') and 'Smite' in event[1]]


def inputs(r):
    # The observation-only executor may release held inputs defensively.
    # Releasing is allowed; these tests forbid newly dispatched keys/text.
    return [event for event in r.backend.events if event[0] in {'key', 'text'}]


def native_fact(*, event='SPELL_DAMAGE', own=True, occurred=None, read=None,
                creature='Creature-0-offline-wolf-A', **changes):
    now = time.time()
    occurred = now if occurred is None else occurred
    fact = {
        'event': event,
        'timestamp': datetime.fromtimestamp(occurred).strftime('%m/%d/%Y %H:%M:%S.%f'),
        'occurred_at': occurred,
        'read_at': now if read is None else read,
        'source_guid': 'Player-0-offline-test' if own else creature,
        'source_name': 'Test Player' if own else 'Young Wolf',
        'dest_guid': creature if own else 'Player-0-offline-test',
        'dest_name': 'Young Wolf' if own else 'Test Player',
        'own_source': own,
        'incoming_to_player': not own,
        'recent_own_target': own,
        'details': ['585', 'Smite', '2', '14'] if own else ['3'],
    }
    fact.update(changes)
    return fact


def install_hud(r, *, player_health=1., target_health=1.):
    values = {'player_health': player_health, 'health_confidence': 1.,
              'target_health': target_health, 'target_health_confidence': 1.,
              'player_mana': 1.}
    original = r.c.target_proposal

    def bars(controller, frame):
        return {'frame_id': frame.frame_id, **values}

    async def target(frame):
        return {**await original(frame), 'hud': bars(r.c, frame)}

    r.c.target_proposal = target
    return values, bars


def test_new_mixed_evidence_requires_sage_then_one_guarded_probe(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            hud, _ = install_hud(r)
            original = await exhaust_turns_and_fail_clear(r)
            h = r.c.hunt
            failures = deepcopy(h.action_failures)
            owner = h.approach['history_key']
            rejections = h.target_history[owner]['rejections']
            # Delayed native context is not fresh cast permission. The actual
            # current selected bar now supplies contradictory visual evidence.
            r.c.combat_log_facts = [native_fact()]
            hud['target_health'] = .35
            before = inputs(r)
            await r.choose(None)
            offered = r.sage.calls[-1]['options']
            assert {'reassess_cast_ready', 'cast_error_persists'} <= offered.keys()
            assert 'attack_mob_level_1' not in offered
            assert inputs(r) == before
            assert h.cast_error['status'] == 'active' and h.cast_obligation
            await r.choose('reassess_cast_ready')
            assert inputs(r) == before  # Reassessment itself has no input.
            r.c.config['smite_burst_count'] = 3
            await r.choose('attack_mob_level_1')
            assert r.sage.calls[-1]['options']['attack_mob_level_1'].binding['type'] == 'cast_guarded'
            assert len(smites(r)) == 2
            assert h.pending['receipt']['receipt_id'] != original['receipt']['receipt_id']
            assert h.action_failures == failures
            assert h.target_history[owner]['rejections'] == rejections
            assert not h.credited_kills and not h.own_damage_evidence
        finally:
            await r.close()
    asyncio.run(run())


def test_rejected_reassessment_is_not_reopened_by_frames_or_log_refresh(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            hud, _ = install_hud(r)
            await facing_failure(r)
            hud['target_health'] = .5
            await r.choose('cast_error_persists')
            h = r.c.hunt
            assert h.cast_obligation and h.cast_error['status'] == 'active'
            for value in (.5, .4, .3):
                hud['target_health'] = value
                r.c.combat_log_facts = [native_fact()]
                await r.choose(None)
                assert not {'reassess_cast_ready', 'attack_mob_level_1'} & r.sage.calls[-1]['options'].keys()
            assert len(smites(r)) == 1 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('fact_kind', ['same_guid_hit', 'wrong_guid_hit', 'stale_hit', 'context_only'])
def test_native_fact_or_prompt_context_alone_never_reopens_failed_cast(tmp_path, fact_kind):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            install_hud(r)
            await facing_failure(r)
            fact = native_fact()
            if fact_kind == 'wrong_guid_hit':
                fact['dest_guid'] = 'Creature-0-different-same-name-wolf'
            elif fact_kind == 'stale_hit':
                fact = native_fact(occurred=time.time() - 240)
            if fact_kind == 'context_only':
                r.c.combat_log_context = 'SPELL_DAMAGE own Smite hit Young Wolf for14; different GUID'
            else:
                r.c.combat_log_facts = [fact]
            await r.choose(None)
            assert not {'reassess_cast_ready', 'attack_mob_level_1'} & r.sage.calls[-1]['options'].keys()
            assert r.c.hunt.cast_error['status'] == 'active' and r.c.hunt.cast_obligation
            assert len(smites(r)) == 1 and not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_fresh_incoming_threat_interrupts_travel_after_failed_clear(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            install_hud(r)
            await exhaust_turns_and_fail_clear(r)
            h = r.c.hunt
            await r.choose('change_search_strategy')
            await r.choose('explore_visible')
            assert h.phase == 'travel'  # Selected living alone is not an attacker.
            debt = deepcopy(h.action_failures)
            before = inputs(r)
            r.c.combat_log_facts = [native_fact(event='SWING_DAMAGE', own=False)]
            await r.choose(None)
            options = r.sage.calls[-1]['options']
            assert 'disengage_threat' in options
            assert not {'explore_visible', 'search_here', 'begin_hunt', 'detour_forward'} & options.keys()
            assert h.phase != 'travel' and not h.planning_requested
            assert h.cast_obligation and h.action_failures == debt
            assert inputs(r) == before and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('fact_kind', ['none', 'old', 'different_player'])
def test_selected_living_or_irrelevant_incoming_does_not_cancel_planning(tmp_path, fact_kind):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            install_hud(r)
            await exhaust_turns_and_fail_clear(r)
            await r.choose('change_search_strategy')
            await r.choose('explore_visible')
            if fact_kind == 'old':
                r.c.combat_log_facts = [native_fact(event='SWING_DAMAGE', own=False,
                                                   occurred=time.time() - 60)]
            elif fact_kind == 'different_player':
                r.c.combat_log_facts = [native_fact(event='SWING_DAMAGE', own=False,
                    incoming_to_player=False, dest_name='Someone Else', dest_guid='Player-0-else')]
            await r.choose(None)
            assert r.c.hunt.phase == 'travel'
            assert 'disengage_threat' not in r.sage.calls[-1]['options']
            assert 'urgent_state' in r.sage.calls[-1]['options']
            assert len(smites(r)) == 1 and not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_explicit_disengagement_keeps_escape_intent_with_current_attacker(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            install_hud(r)
            await exhaust_turns_and_fail_clear(r)
            r.c.combat_log_facts = [native_fact(event='SWING_DAMAGE', own=False)]
            before = inputs(r)
            debt = deepcopy(r.c.hunt.action_failures)
            await r.choose('disengage_threat')
            assert inputs(r) == before
            await r.choose('explore_visible')
            assert r.c.hunt.phase == 'travel'
            r.c.combat_log_facts = [native_fact(event='SWING_DAMAGE', own=False)]
            await r.choose(None)
            assert r.c.hunt.phase == 'travel'
            assert 'urgent_state' in r.sage.calls[-1]['options']
            assert r.c.hunt.cast_obligation and r.c.hunt.action_failures == debt
            assert len(smites(r)) == 1 and not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_heal_preserves_failed_fight_then_reassessment_restores_single_attack(tmp_path, monkeypatch):
    from sage_wow.agent import grind_resources

    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            hud, bars = install_hud(r)
            monkeypatch.setattr(grind_resources, 'hud_resources', bars)
            r.c.config.update(encounter_resources=True, preserve_target_heal=True)
            await exhaust_turns_and_fail_clear(r)
            h = r.c.hunt
            encounter, owner = h.encounter, h.approach['history_key']
            failures = deepcopy(h.action_failures)
            await r.choose('change_search_strategy')
            await r.choose('explore_visible')
            hud['player_health'] = .29
            r.c.combat_log_facts = [native_fact(event='SWING_DAMAGE', own=False)]
            await r.choose('heal_self')
            assert r.c.heal_pending and h.pending['purpose'] == 'heal_preserve'
            assert h.encounter == encounter and h.cast_obligation
            assert '/cast [@player] Lesser Heal' in [e[1] for e in r.backend.events if e[0] == 'text']
            hud['player_health'] = .84
            r.c.wait_until = 0
            result = await r.c.process(r.world.capture())
            assert result.status == 'grind_reobserve' and r.c.heal_pending is None
            assert h.cast_obligation and h.cast_error['status'] == 'active'
            hud['target_health'] = .15
            await r.choose('reassess_cast_ready')
            assert h.phase != 'travel'
            r.c.config['smite_burst_count'] = 3
            await r.choose('attack_mob_level_1')
            assert len(smites(r)) == 2 and r.world.name == 'Young Wolf'
            assert h.encounter == encounter and h.approach['history_key'] == owner
            assert h.action_failures == failures and not h.credited_kills
            assert r.sage.calls[-1]['options']['attack_mob_level_1'].binding['type'] == 'cast_guarded'
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', [
    'tiny_drop', 'weak_current_health', 'weak_original_health', 'stale_current_hud',
    'wrong_original_hud', 'changed_source_hash', 'partial_cast', 'unknown_dispatch',
    'unreconciled_input', 'new_acquisition', 'new_encounter', 'new_epoch',
    'wrong_receipt', 'changed_target', 'dead_target', 'different_window', 'different_geometry',
    'positive_level_change', 'player_kind', 'friendly_or_self_kind',
])
def test_mixed_visual_review_rejects_unrelated_or_uncertain_source(tmp_path, change):
    from sage_wow.agent.grind_encounter_recovery import reassessment_candidate

    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            r.world.target_level = 1
            hud, _ = install_hud(r)
            await facing_failure(r)
            h = r.c.hunt
            source = h.cast_error['assessment_source']
            hud['target_health'] = .3
            frame = r.world.capture()
            target = await r.c.target_proposal(frame)
            if change == 'tiny_drop': target['hud']['target_health'] = .98
            elif change == 'weak_current_health': target['hud']['target_health_confidence'] = .79
            elif change == 'weak_original_health': source['measurement']['combat_hud']['target_health_confidence'] = .79
            elif change == 'stale_current_hud': target['hud']['frame_id'] = 'old-unrelated-frame'
            elif change == 'wrong_original_hud': source['measurement']['combat_hud']['frame_id'] = 'not-source-frame'
            elif change == 'changed_source_hash': source['source_hash'] = '0' * 64
            elif change == 'partial_cast': source['receipt']['completed'] = False
            elif change == 'unknown_dispatch': source['receipt']['dispatch_unknown'] = True
            elif change == 'unreconciled_input': h.input_effect_unverified = {'reason': 'partial native input'}
            elif change == 'new_acquisition':
                # Positive observed identity change, not numeric OCR jitter.
                h.selection_seen({**target, 'name': 'Different Wolf'})
                h.selection_seen(target)
            elif change == 'new_encounter': h.encounter += 1
            elif change == 'new_epoch': r.c.cycle.session_epoch = 'new-epoch'
            elif change == 'wrong_receipt': source['receipt']['receipt_id'] = 'unrelated-completed-receipt'
            elif change == 'changed_target': target['name'] = 'Another Wolf'
            elif change == 'dead_target': target['visual_observation']['life_state'] = 'dead'
            elif change == 'different_window': frame = replace(frame, source='another-window')
            elif change == 'different_geometry': frame = replace(frame, width=501)
            elif change == 'positive_level_change':
                h.selection_seen({**target, 'levels': []})
                target['levels'] = [2]
                target['visual_observation']['level'] = 2
                h.selection_seen(target)
            elif change in {'player_kind', 'friendly_or_self_kind'}:
                other = deepcopy(target)
                other['visual_observation']['target_kind'] = change.removesuffix('_kind')
                h.selection_seen(other)
                h.selection_seen(target)
            before = inputs(r)
            assert reassessment_candidate(r.c, frame, target) is None
            assert h.cast_error['status'] == 'active' and h.cast_obligation
            assert inputs(r) == before and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['input_generation', 'acquisition', 'epoch', 'input_unknown'])
def test_observed_ready_credit_does_not_survive_changed_input_authority(tmp_path, change):
    from sage_wow.agent.grind_encounter_recovery import probe_credit

    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            hud, _ = install_hud(r)
            await facing_failure(r)
            hud['target_health'] = .4
            await r.choose('reassess_cast_ready')
            h = r.c.hunt
            if change == 'input_generation': r.c.cycle._input_generation += 1
            elif change == 'acquisition': h.selection_revision += 1
            elif change == 'epoch': r.c.cycle.session_epoch = 'different-epoch'
            elif change == 'input_unknown': h.input_effect_unverified = {'reason': 'unknown execution'}
            frame = r.world.capture()
            target = await r.c.target_proposal(frame)
            assert probe_credit(r.c, frame, target) is None
            assert h.cast_obligation and len(smites(r)) == 1 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_reassessed_probe_still_requires_fresh_matching_dispatch_target(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            hud, _ = install_hud(r)
            await facing_failure(r)
            hud['target_health'] = .4
            await r.choose('reassess_cast_ready')
            async def changed_selection():
                r.world.name = 'Different Wolf'
            r.sage.hook = changed_selection
            result = await r.choose('attack_mob_level_1')
            assert result.status == 'dispatch_guard_rejected'
            assert len(smites(r)) == 1 and r.c.hunt.cast_obligation
            assert r.c.hunt.cast_error['reassessment']['status'] == 'ready'
            assert not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_current_own_health_loss_interrupts_travel_without_native_log(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            hud, _ = install_hud(r)
            await exhaust_turns_and_fail_clear(r)
            await r.choose('change_search_strategy')
            await r.choose('explore_visible')
            assert r.c.hunt.phase == 'travel'
            hud['player_health'] = .75
            await r.choose(None)
            assert r.c.hunt.phase != 'travel'
            assert 'disengage_threat' in r.sage.calls[-1]['options']
            assert len(smites(r)) == 1 and r.c.hunt.cast_obligation
            assert not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_numeric_ocr_jitter_does_not_discard_continuous_cast_evidence(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            r.world.target_level = 1
            hud, _ = install_hud(r)
            original = await exhaust_turns_and_fail_clear(r)
            old_revision = original['selection_revision']
            before = inputs(r)
            # Same visible selected living creature throughout. Only the
            # number proposal becomes unreadable and then readable again.
            r.world.target_level = None
            await r.choose(None)
            r.world.target_level = 1
            await r.choose(None)
            assert r.c.hunt.selection_revision > old_revision
            assert inputs(r) == before
            hud['target_health'] = .2
            await r.choose('reassess_cast_ready')
            assert inputs(r) == before
            await r.choose('attack_mob_level_1')
            assert len(smites(r)) == 2 and not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_explicit_reengagement_ends_previous_escape_intent(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            install_hud(r)
            await exhaust_turns_and_fail_clear(r)
            r.c.combat_log_facts = [native_fact(event='SWING_DAMAGE', own=False)]
            await r.choose('disengage_threat')
            await r.choose('explore_visible')
            assert r.c.hunt.disengagement and r.c.hunt.phase == 'travel'
            # Sage explicitly returns from its escape to deal with danger.
            await r.choose('urgent_state')
            assert r.c.hunt.disengagement is None
            # A subsequent ordinary travel state must not inherit that old
            # escape exemption. No travel input is manufactured by this setup.
            r.c.hunt.phase = 'travel'
            r.c.hunt.planning_requested = False
            r.c.combat_log_facts = [native_fact(event='SWING_DAMAGE', own=False)]
            await r.choose(None)
            assert r.c.hunt.phase != 'travel'
            assert 'disengage_threat' in r.sage.calls[-1]['options']
            assert r.c.hunt.cast_obligation and len(smites(r)) == 1
            assert not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('entry', ['planning', 'explicit_escape'])
def test_mixed_ready_exits_planning_or_escape_and_exposes_one_guarded_cast(tmp_path, entry):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            hud, _ = install_hud(r)
            await exhaust_turns_and_fail_clear(r)
            h = r.c.hunt
            failures = deepcopy(h.action_failures)
            owner = h.approach['history_key']
            rejections = h.target_history[owner]['rejections']
            if entry == 'explicit_escape':
                r.c.combat_log_facts = [native_fact(event='SWING_DAMAGE', own=False)]
                await r.choose('disengage_threat')
                assert h.disengagement
            else:
                await r.choose('change_search_strategy')
            assert h.phase == 'choose_area' and h.planning_requested
            hud['target_health'] = .35
            before = inputs(r)
            await r.choose('reassess_cast_ready')
            assert inputs(r) == before
            assert h.phase == 'fight' and h.compact_stage == 'inspect'
            assert not h.planning_requested and h.disengagement is None
            assert h.cast_obligation  # Assessment itself did not execute a cast.
            r.c.config['smite_burst_count'] = 3
            await r.choose('attack_mob_level_1')
            assert r.sage.calls[-1]['options']['attack_mob_level_1'].binding['type'] == 'cast_guarded'
            assert len(smites(r)) == 2
            assert h.action_failures == failures
            assert h.target_history[owner]['rejections'] == rejections
            assert not h.credited_kills and not h.own_damage_evidence
        finally:
            await r.close()
    asyncio.run(run())
