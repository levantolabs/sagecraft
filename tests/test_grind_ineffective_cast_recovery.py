"""Semantic failure recovery is independent of imperfect resource diagnostics."""
from test_grind_product_spec import retain_legacy_no_effect
import asyncio
from copy import deepcopy

import pytest

from test_grind_product_spec import Rig
from test_active_attacker_recovery import start, install_hud, smites


async def ineffective(r, resource='regenerating'):
    await start(r)
    hud, _ = install_hud(r)
    for before, after in [(.6, .8), (.8, 1.)]:
        hud['player_mana'] = before if resource == 'regenerating' else None
        await r.choose('attack_mob_level_1')
        hud['player_mana'] = after if resource == 'regenerating' else None
        await retain_legacy_no_effect(r)
    assert r.c.hunt.no_effect_retries == 2
    assert (r.c.hunt.cast_review or {}).get('unchanged_resource_casts', 0) < 2
    return hud


@pytest.mark.parametrize('resource', ['regenerating', 'missing'])
def test_ineffective_casts_offer_correction_then_existing_retry(tmp_path, resource):
    async def run():
        r = Rig(tmp_path)
        try:
            await ineffective(r, resource)
            h = r.c.hunt
            await r.choose(None)
            options = r.sage.calls[-1]['options']
            assert {'forward', 'turn_left', 'turn_right', 'reject_selected_target'} <= options.keys()
            assert not {'attack_mob_level_1', 'cannot_assess', 'reinspect_selected_frame',
                        'approach_for_range_check', 'stand_for_cast'} & options.keys()
            assert 'Sage assessed at least two completed cast attempts as ineffective' in r.sage.calls[-1]['prompt']
            assert 'unresolved approach' in options['reject_selected_target'].description
            debt = deepcopy(h.action_failures)
            await r.choose('forward')
            await r.choose('motion_useful')
            assert h.retry_credit and not h.cast_obligation and h.action_failures == debt
            r.c.config['smite_burst_count'] = 3
            await r.choose('attack_mob_level_1')
            assert len(smites(r)) == 3 and not h.retry_credit and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_focus_roundtrip_expires_retry_without_unlocking_finisher(tmp_path, monkeypatch):
    from sage_wow.agent import grind_resources
    async def run():
        r = Rig(tmp_path)
        try:
            hud = await ineffective(r)
            await r.choose('forward');await r.choose('motion_useful')
            h = r.c.hunt
            r.c.pause_focus();assert r.c.resume_focus()
            r.executor.arm()
            await r.choose('world_normal_confirmed')
            assert h.no_effect_retries == 2 and not h.retry_credit and not h.retry_credit_source
            from sage_wow.agent.grind_search import INEFFECTIVE_COMPLETED_CAST
            assert h.cast_obligation == INEFFECTIVE_COMPLETED_CAST
            monkeypatch.setattr(grind_resources, 'hud_resources', lambda c, f: {'frame_id': f.frame_id, **hud})
            r.c.config.update(encounter_resources=True, preserve_target_heal=True)
            hud.update(player_health=.29, target_health=.2)
            await r.choose(None)
            assert 'heal_self' in r.sage.calls[-1]['options']
            assert 'finish_fight' not in r.sage.calls[-1]['options']
            assert len(smites(r)) == 2 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_numeric_ocr_churn_preserves_completed_correction_for_guarded_retry(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await ineffective(r)
            await r.choose('forward');await r.choose('motion_useful')
            h = r.c.hunt
            continuity = h.target_continuity
            receipt = h.correction_evidence['receipt_id']
            r.world.target_level = 1
            await r.choose('attack_mob_level_1')
            assert h.target_continuity == continuity and not h.retry_credit
            assert h.pending['retry_probe'] and h.pending['family'] == 'combat'
            assert h.cast_obligation is None and len(smites(r)) == 3
            assert receipt and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_observed_damage_after_retry_restores_ordinary_finisher(tmp_path, monkeypatch):
    from sage_wow.agent import grind_resources
    async def run():
        r = Rig(tmp_path)
        try:
            hud = await ineffective(r)
            await r.choose('forward');await r.choose('motion_useful')
            await r.choose('attack_mob_level_1')
            hud['target_health'] = .2
            await r.choose('damaged_alive')
            h = r.c.hunt
            assert h.no_effect_retries >= 2 and h.failures['combat'] == 0 and not h.retry_credit
            monkeypatch.setattr(grind_resources, 'hud_resources', lambda c, f: {'frame_id': f.frame_id, **hud})
            r.c.config.update(encounter_resources=True, preserve_target_heal=True)
            hud['player_health'] = .29
            await r.choose('finish_fight')
            assert len(smites(r)) == 4 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_ineffective_correction_keeps_debt_at_offer_and_dispatch(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await ineffective(r)
            h = r.c.hunt
            await r.choose('forward')
            await r.choose('motion_no_useful_effect')
            owner = h.combat_history_key
            key = h.motion_key('forward', 'cast_correction', owner)
            before = len(r.physical_keys())
            async def exhaust():h.unresolved_motion[key] = 1
            r.sage.hook = exhaust
            result = await r.choose('forward')
            assert result.status == 'precondition_failed' and len(r.physical_keys()) == before
            r.sage.hook = None
            await r.choose(None)
            assert 'forward' not in r.sage.calls[-1]['options']
            assert 'turn_left' in r.sage.calls[-1]['options']
            assert h.motion_count('forward', 'cast_correction', owner) == 2 and len(smites(r)) == 2
        finally:
            await r.close()
    asyncio.run(run())


def test_ineffective_correction_survives_ordinary_current_threat(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await ineffective(r)
            from test_active_attacker_recovery import native_fact
            r.c.combat_log_facts = [native_fact(event='SWING_DAMAGE', own=False)]
            await r.choose(None)
            options = r.sage.calls[-1]['options']
            assert {'forward', 'turn_left', 'disengage_threat'} <= options.keys()
            assert not {'cannot_assess','no_effect','reinspect_cast_problem','reinspect_selected_frame'} & options.keys()
            assert 'attack_mob_level_1' not in options and 'change_search_strategy' not in options
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['owner', 'epoch', 'continuity', 'scope', 'partial', 'hash', 'active_error'])
def test_ineffective_task_requires_current_retained_provenance(tmp_path, change):
    async def run():
        r = Rig(tmp_path)
        try:
            await ineffective(r)
            from sage_wow.agent.grind_ineffective_cast import recovery_candidate
            h = r.c.hunt
            source = h.recent_combat
            frame = r.world.capture();target = await r.c.target_proposal(frame)
            assert recovery_candidate(r.c, frame, target, None, 'inspect')
            if change == 'owner':source['target_history_key'] = 'other'
            elif change == 'epoch':source['receipt']['session_epoch'] = 'other'
            elif change == 'continuity':h.target_continuity += 1
            elif change == 'scope':source['source_scope']['source'] = 'other'
            elif change == 'partial':source['receipt']['completed'] = False
            elif change == 'hash':source['source_hash'] = 'other'
            elif change == 'active_error':h.cast_error = {'kind':'range','status':'active'}
            assert recovery_candidate(r.c, frame, target, None, 'inspect') is None
            assert len(smites(r)) == 2 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_ineffective_cast_then_skipped_corpse_returns_to_concrete_hunt(tmp_path):
    from test_grind_loot_integration import LootRig
    from test_grind_loot_budget import enable, entry, tick
    async def run():
        r = LootRig(tmp_path)
        try:
            hud = await ineffective(r)
            enable(r)
            h = r.c.hunt
            r.world.name = '';hud['target_health'] = None
            original = r.c.target_proposal
            async def absent(frame):
                return {**await original(frame), 'visual_observation': {'selected_hud': 'absent'}}
            r.c.target_proposal = absent
            await r.choose('inspect_recent_corpse')
            assert h.encounter_ended and not h.target_dead_observed and not h.credited_kills
            await r.loot(None)
            entry(r)['spent'] = 30.
            await tick(r)
            assert r.c.loot.data['outcome'] == 'skipped_unverified'
            debt = deepcopy((h.cast_obligation, h.target_history, h.action_failures, h.credited_kills))
            await r.choose('target_enemy')
            assert set(r.sage.calls[-1]['options']) == {'target_enemy', 'forward', 'turn_left', 'turn_right'}
            assert (h.cast_obligation, h.target_history, h.action_failures, h.credited_kills) == debt
            assert h.pending['family'] == 'target' and len(smites(r)) == 2
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['pending', 'owner', 'continuity'])
def test_changed_state_during_correction_answer_sends_no_input(tmp_path, change):
    async def run():
        r = Rig(tmp_path)
        try:
            await ineffective(r)
            h = r.c.hunt
            async def changed():
                if change == 'pending':h.pending = {'family': 'motion'}
                elif change == 'owner':h.combat_history_key = 'other'
                else:h.target_continuity += 1
            r.sage.hook = changed
            before = len(r.physical_keys())
            result = await r.choose('forward')
            assert result.status == 'precondition_failed' and len(r.physical_keys()) == before
            assert len(smites(r)) == 2 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_semantic_retry_rechecks_its_correction_proof(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await ineffective(r)
            await r.choose('forward');await r.choose('motion_useful')
            r.c.config['smite_burst_count'] = 3
            async def changed():r.c.hunt.correction_evidence['source_hash'] = 'changed'
            r.sage.hook = changed
            result = await r.choose('attack_mob_level_1')
            assert result.status == 'precondition_failed' and len(smites(r)) == 2
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['hash', 'proof_missing', 'generation'])
def test_invalid_semantic_credit_before_menu_returns_to_bounded_correction(tmp_path, change):
    async def run():
        r = Rig(tmp_path)
        try:
            await ineffective(r)
            await r.choose('forward');await r.choose('motion_useful')
            h = r.c.hunt
            if change == 'hash':h.correction_evidence['source_hash'] = 'changed'
            elif change == 'proof_missing':h.correction_evidence = None
            else:r.c.cycle._input_generation += 1
            await r.choose(None)
            options = r.sage.calls[-1]['options']
            assert 'attack_mob_level_1' not in options
            assert {'forward', 'turn_left', 'reject_selected_target'} <= options.keys()
            assert not h.retry_credit_source and not h.retry_credit and h.cast_obligation
            assert len(smites(r)) == 2
        finally:
            await r.close()
    asyncio.run(run())


def test_ordinary_new_retry_grant_supersedes_semantic_origin(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await ineffective(r)
            await r.choose('forward');await r.choose('motion_useful')
            h = r.c.hunt
            h.correction_evidence = None
            # Independently assessed mana shortage owns the new recovery;
            # exercise its real rest/assessment/resume grant producer.
            h.cast_error = {'kind': 'mana', 'status': 'active'}
            h.no_mana = True
            r.c.enter_resource_recovery()
            await r.choose('rest')
            await r.choose('resources_improved')
            await r.choose('recovered_resume')
            assert h.retry_credit and h.retry_credit_source is None
            r.c.config['smite_burst_count'] = 3
            await r.choose('attack_mob_level_1')
            assert h.retry_credit_source is None and len(smites(r)) == 3
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interruption', ['ui', 'heal'])
def test_interrupted_semantic_correction_keeps_heal_but_blocks_finisher(tmp_path, monkeypatch, interruption):
    from sage_wow.agent import grind_resources
    async def run():
        r = Rig(tmp_path)
        try:
            hud = await ineffective(r)
            await r.choose('forward');await r.choose('motion_useful')
            monkeypatch.setattr(grind_resources, 'hud_resources', lambda c, f: {'frame_id': f.frame_id, **hud})
            r.c.config.update(encounter_resources=True, preserve_target_heal=True)
            if interruption == 'ui':
                from test_grind_guild_invitation import attach_invitation
                # The scaled fake invitation overlaps the fake target-name
                # OCR box. Preserve the independently observed same target,
                # as with the real invitation away from the selected HUD.
                original = r.c.target_proposal
                selected = await original(r.world.capture())
                identity = {key: selected.get(key) for key in ('name', 'levels', 'self_target', 'invalid_text')}
                async def same_target(frame):
                    return {**await original(frame), **identity}
                r.c.target_proposal = same_target
                visible = attach_invitation(r)
                await r.choose('close_visible_ui')
                visible[0] = False
                await r.choose('world_normal_confirmed')
            else:
                hud['player_health'] = .29
                await r.choose('heal_self')
                hud['player_health'] = .85;r.c.wait_until = 0
                await r.c.process(r.world.capture())
            h = r.c.hunt
            assert not h.retry_credit_source and not h.retry_credit
            assert h.cast_obligation and h.correction_evidence is None
            hud.update(player_health=.29, target_health=.2)
            r.c.resource_defer_until = 0
            await r.choose(None)
            assert 'heal_self' in r.sage.calls[-1]['options']
            assert 'finish_fight' not in r.sage.calls[-1]['options']
            assert len(smites(r)) == 2 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('proof', ['unresolved', 'valid', 'changed_during_answer', 'credit_reset_during_answer'])
def test_semantic_finisher_needs_useful_current_correction(tmp_path, monkeypatch, proof):
    from sage_wow.agent import grind_resources
    async def run():
        r = Rig(tmp_path)
        try:
            hud = await ineffective(r)
            if proof != 'unresolved':
                await r.choose('forward');await r.choose('motion_useful')
            monkeypatch.setattr(grind_resources, 'hud_resources', lambda c, f: {'frame_id': f.frame_id, **hud})
            r.c.config.update(encounter_resources=True, preserve_target_heal=True)
            hud.update(player_health=.29, target_health=.2)
            if proof == 'unresolved':
                await r.choose(None)
                assert 'heal_self' in r.sage.calls[-1]['options']
                assert 'finish_fight' not in r.sage.calls[-1]['options']
            else:
                if proof in {'changed_during_answer', 'credit_reset_during_answer'}:
                    async def changed():
                        if proof == 'changed_during_answer':r.c.hunt.correction_evidence = None
                        else:r.c.hunt.retry_credit = False
                    r.sage.hook = changed
                result = await r.choose('finish_fight')
                assert result.status == ('dispatched' if proof == 'valid' else 'precondition_failed')
            assert len(smites(r)) == (3 if proof == 'valid' else 2)
        finally:
            await r.close()
    asyncio.run(run())
