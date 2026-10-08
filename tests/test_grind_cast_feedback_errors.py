"""Offline error-task lifetimes, mixed results and existing movement allowances."""
from test_grind_product_spec import retain_legacy_no_effect
import asyncio

import pytest

from test_grind_product_spec import Rig
from test_active_attacker_recovery import start, install_hud, smites
from test_grind_committed_combat import install_transient_error

ERRORS = [('range', 'Out of range'), ('facing', 'Target is not in front of you'),
          ('los', 'Target not in line of sight'), ('standing', 'You must be standing to do that'),
          ('mana', 'Not enough mana'), ('cooldown', 'Spell is not ready yet'),
          ('interrupted', 'Interrupted'), ('resist', 'Resist'), ('evade', 'Evade'),
          ('unavailable_spell', 'Spell not learned'), ('no_target', 'No target'),
          ('dead_target', 'Target is dead')]


@pytest.mark.parametrize('kind,text', ERRORS)
def test_linked_error_owns_feedback_until_that_cue_is_rejected(tmp_path, kind, text):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            install_hud(r)
            await r.choose('attack_mob_level_1')
            pending = r.c.hunt.pending
            install_transient_error(r, kind, text)
            await r.choose(None)
            offered = r.sage.calls[-1]['options']
            assert not {'no_effect', 'damaged_alive', 'own_damaged_alive', 'attack_mob_level_1',
                        'change_search_strategy', 'approach_for_range_check', 'stand_for_cast'} & offered.keys()
            assert {'error_not_supported', 'casting', 'lost', 'recover_now', 'ui_blocked'} <= offered.keys()
            assert r.c.hunt.pending is pending and r.c.hunt.cast_error is None
            await r.choose('error_not_supported')
            assert r.c.hunt.pending is pending and not pending['outcome_consumed']
            await retain_legacy_no_effect(r)
            assert r.c.hunt.no_effect_retries == 1 and not r.c.stopped
            assert not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_rejecting_one_cue_does_not_discard_another(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            await r.choose('attack_mob_level_1')
            install_transient_error(r, *ERRORS[0])
            pending = r.c.hunt.pending
            post = pending['receipt']['execution']['post_cast_observations'][0]
            post['observed_error_cues'].append({'kind': ERRORS[1][0], 'text': ERRORS[1][1]})
            await r.choose('error_not_supported')
            assert len(pending['rejected_error_cues']) == 1
            await r.choose(None)
            assert 'no_effect' not in r.sage.calls[-1]['options']
            assert 'facing' in r.sage.calls[-1]['options']['position_error'].description
            await r.choose('position_error')
            assert r.c.hunt.cast_error['kind'] == 'facing'
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind,text,action,purpose', [
    ('facing', ERRORS[1][1], 'turn_left', 'cast_correction'),
    ('standing', ERRORS[3][1], 'forward', 'standing')])
def test_feedback_corrections_preserve_method_debt_and_dispatch_recheck(tmp_path, kind, text, action, purpose):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            await r.choose('attack_mob_level_1')
            install_transient_error(r, kind, text)
            h = r.c.hunt
            debt = h.motion_key(action, purpose, h.combat_history_key)
            h.action_failures[debt] = 1
            option = 'stand_pulse' if kind == 'standing' else action
            async def exhaust_during_request():
                h.unresolved_motion[debt] = 1
            r.sage.hook = exhaust_during_request
            before = len(r.physical_keys())
            result = await r.choose(option)
            assert result.status == 'precondition_failed'
            assert len(r.physical_keys()) == before
            r.sage.hook = None
            await r.choose(None)
            assert option not in r.sage.calls[-1]['options']
            assert h.motion_count(action, purpose, h.combat_history_key) == 2
        finally:
            await r.close()
    asyncio.run(run())


def test_error_and_health_drop_reenter_existing_single_probe_path(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            hud, _ = install_hud(r)
            await r.choose('attack_mob_level_1')
            install_transient_error(r, *ERRORS[1])
            hud['target_health'] = .4
            await r.choose('position_error')
            h = r.c.hunt
            receipt = h.cast_error['cast_receipt_id']
            assert h.cast_obligation and h.cast_error['assessment_source']
            assert not h.credited_kills and not h.own_damage_evidence
            await r.choose('reassess_cast_ready')
            assert len(smites(r)) == 1
            r.c.config['smite_burst_count'] = 3
            await r.choose('attack_mob_level_1')
            assert len(smites(r)) == 2
            assert h.cast_error['reassessment']['cast_receipt_id'] == receipt
            assert h.cast_error['reassessment']['status'] == 'consumed'
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interruption', ['ui', 'heal', 'unassessed'])
def test_interruptions_retain_error_without_relinking_or_refunding_cast(tmp_path, monkeypatch, interruption):
    from sage_wow.agent import grind_resources
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            hud, bars = install_hud(r)
            monkeypatch.setattr(grind_resources, 'hud_resources', bars)
            r.c.config.update(encounter_resources=True, preserve_target_heal=True)
            await r.choose('attack_mob_level_1')
            install_transient_error(r, *ERRORS[1])
            h = r.c.hunt
            old_receipt = h.pending['receipt']['receipt_id']
            if interruption == 'ui':
                await r.choose('ui_blocked')
                await r.choose('world_normal_confirmed')
            elif interruption == 'heal':
                hud.update(player_health=.29, target_health=.2)
                await r.choose('heal_self')
                assert 'finish_fight' not in r.sage.calls[-1]['options']
                hud['player_health'] = .85
                r.c.wait_until = 0
                result = await r.c.process(r.world.capture())
                assert result.status == 'grind_reobserve'
            else:
                await r.choose('cannot_assess')
                await r.choose('cannot_assess')
            review = h.target_history[h.combat_history_key]['unassessed_error_feedback']
            assert review['source']['receipt']['receipt_id'] == old_receipt
            assert h.pending is None and h.cast_error is None
            before = len(r.physical_keys())
            await r.choose('retain_cast_error')
            assert all(o.binding == {'type': 'observe_only'} for o in r.sage.calls[-1]['options'].values())
            assert len(r.physical_keys()) == before and h.pending is None
            assert h.cast_error['cast_receipt_id'] == old_receipt and h.cast_obligation
            assert not h.own_damage_evidence and not h.credited_kills
            if interruption == 'heal':
                await r.choose('reassess_cast_ready')
                await r.choose('attack_mob_level_1')
                assert len(smites(r)) == 2
            else:
                await r.choose('turn_left')
                assert h.pending['family'] == 'motion'
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['epoch','generation_during_answer','continuity','owner','scope','source_hash','error_hash','unknown_input','new_cast'])
def test_interrupted_review_has_no_authority_across_changed_lifetime(tmp_path, change):
    from sage_wow.agent.grind_cast_feedback import interrupted_candidate
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            install_hud(r)
            await r.choose('attack_mob_level_1')
            install_transient_error(r, *ERRORS[1])
            h = r.c.hunt
            await r.choose('ui_blocked')
            await r.choose('world_normal_confirmed')
            frame = r.world.capture()
            target = await r.c.target_proposal(frame)
            review = interrupted_candidate(r.c, frame, target)
            assert review
            source = review['source']
            if change == 'generation_during_answer':
                async def changed():r.c.cycle._input_generation += 1
                r.sage.hook = changed
                result = await r.choose('retain_cast_error')
                assert result.status == 'stale_input_generation'
                assert h.cast_error is None and review['status'] == 'unassessed'
                return
            if change == 'epoch':source['receipt']['session_epoch'] = 'unrelated'
            elif change == 'continuity':h.target_continuity += 1
            elif change == 'owner':source['target_history_key'] = 'different'
            elif change == 'scope':source['source_scope']['source'] = 'different'
            elif change == 'source_hash':source['source_hash'] = 'changed'
            elif change == 'error_hash':source['position_error_evidence']['sha256'] = 'changed'
            elif change == 'unknown_input':h.input_effect_unverified = True
            elif change == 'new_cast':review['status'] = 'superseded_by_new_cast'
            assert interrupted_candidate(r.c, frame, target) is None
            assert h.cast_error is None and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_precast_cue_does_not_create_feedback_error_task(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            r.world.error = 'Out of range'
            await r.choose('attack_mob_level_1')
            await retain_legacy_no_effect(r)
            assert 'position_error' not in r.sage.calls[-1]['options']
            assert r.c.hunt.no_effect_retries == 1
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interrupted', [False, True])
def test_mana_error_uses_complete_resource_recovery_transition(tmp_path, interrupted):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            install_hud(r)
            await r.choose('attack_mob_level_1')
            install_transient_error(r, 'mana', 'Not enough mana')
            h = r.c.hunt
            h.active_seconds = 1000
            if interrupted:
                await r.choose('ui_blocked')
                await r.choose('world_normal_confirmed')
                await r.choose('retain_cast_error')
            else:
                await r.choose('cast_error_observed')
            assert h.phase == 'recover' and h.prior_phase == 'fight'
            assert h.recovery_started >= 1000 and h.recovery_progress_at >= 1000
            assert h.suspended_for_heal and h.no_mana
            await r.choose('rest')
            assert 'heal_self' not in r.sage.calls[-1]['options']
            await r.choose('resources_improved')
            await r.choose('recovered_resume')
            assert h.phase == 'fight' and not h.no_mana and h.recovery_started is None
            assert not h.credited_kills and len(smites(r)) == 1
        finally:
            await r.close()
    asyncio.run(run())


def test_confirmed_error_does_not_discard_remaining_cue_or_allow_probe(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            hud, _ = install_hud(r)
            await r.choose('attack_mob_level_1')
            install_transient_error(r, *ERRORS[0])
            post = r.c.hunt.pending['receipt']['execution']['post_cast_observations'][0]
            post['observed_error_cues'].append({'kind':'mana','text':'Not enough mana'})
            hud['target_health'] = .4
            await r.choose('position_error')
            h = r.c.hunt
            await r.choose(None)
            assert h.cast_error['kind'] == 'range'
            assert not {'attack_mob_level_1','reassess_cast_ready','retain_cast_error'} & r.sage.calls[-1]['options'].keys()
            await r.choose('forward')
            await r.choose('motion_useful')
            await r.choose('retain_cast_error')
            assert h.cast_error['kind'] == 'mana' and h.phase == 'recover'
            await r.choose('rest')
            await r.choose('resources_improved')
            await r.choose('recovered_resume')
            await r.choose('attack_mob_level_1')
            assert len(smites(r)) == 2 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_rejected_interrupted_error_returns_to_fresh_hunt_without_credit(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            install_hud(r)
            await r.choose('attack_mob_level_1')
            install_transient_error(r, *ERRORS[1])
            await r.choose('ui_blocked')
            await r.choose('world_normal_confirmed')
            await r.choose('error_not_supported')
            assert r.c.hunt.cast_error is None and r.c.hunt.pending is None
            await r.choose('attack_mob_level_1')
            assert len(smites(r)) == 2
            assert not r.c.hunt.own_damage_evidence and not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('recovery_action', ['backward', 'heal_self'])
def test_pending_recovery_cannot_bypass_retained_error_with_finisher(tmp_path, monkeypatch, recovery_action):
    from sage_wow.agent import grind_resources
    from sage_wow.agent.grind_cast_feedback import interrupted_candidate, retained_error_candidate
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            hud, bars = install_hud(r)
            monkeypatch.setattr(grind_resources, 'hud_resources', bars)
            await r.choose('attack_mob_level_1')
            install_transient_error(r, *ERRORS[1])
            h = r.c.hunt
            await r.choose('recover_now')
            await r.choose(recovery_action)
            pending = h.pending
            assert pending['family'] in {'motion', 'recovery'} and not r.c.heal_pending
            r.c.config.update(encounter_resources=True, preserve_target_heal=True)
            hud.update(player_health=.29, target_health=.2)
            frame = r.world.capture()
            target = await r.c.target_proposal(frame)
            assert interrupted_candidate(r.c, frame, target) is None
            review = retained_error_candidate(r.c, frame, target)
            assert review and review['status'] == 'unassessed'
            await r.choose(None)
            assert 'heal_self' in r.sage.calls[-1]['options']
            assert 'finish_fight' not in r.sage.calls[-1]['options']
            assert review['status'] == 'unassessed' and h.pending is pending
            assert len(smites(r)) == 1 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_historical_confirmation_keeps_third_cue_queued(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await start(r)
            install_hud(r)
            await r.choose('attack_mob_level_1')
            install_transient_error(r, *ERRORS[0])
            h = r.c.hunt
            post = h.pending['receipt']['execution']['post_cast_observations'][0]
            post['observed_error_cues'].extend([
                {'kind': 'facing', 'text': ERRORS[1][1]},
                {'kind': 'mana', 'text': ERRORS[4][1]}])
            await r.choose('position_error')
            await r.choose('forward')
            await r.choose('motion_useful')
            first_review = h.target_history[h.combat_history_key]['unassessed_error_feedback']
            await r.choose('retain_cast_error')
            assert first_review['status'] == 'assessed' and h.cast_error['kind'] == 'facing'
            remaining = h.target_history[h.combat_history_key]['unassessed_error_feedback']
            assert remaining is not first_review and remaining['status'] == 'unassessed'
            await r.choose('turn_left')
            await r.choose('motion_useful')
            await r.choose('retain_cast_error')
            assert h.cast_error['kind'] == 'mana' and h.no_mana and h.phase == 'recover'
            assert len(smites(r)) == 1 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())
