"""Own-player correction after release cannot renew the abandoned target."""
import asyncio
from copy import deepcopy

import pytest

from sage_wow.agent.grind_cast_feedback import released_player_resolution, restore_released_acquisition, unresolved_nonlocal_cues
from test_grind_later_precast_feedback import guard_cast, FACING, MANA
from test_grind_product_spec import Rig
from test_active_attacker_recovery import smites


ERRORS = [('standing', 'You must be standing to cast'),
    ('cooldown', 'Spell is not ready yet'), ('interrupted', 'Interrupted')]


async def released(r, monkeypatch, kind, text, extra=()):
    hud, pending = await guard_cast(r, monkeypatch, [FACING, {'kind':kind, 'text':text}, *extra])
    await r.choose('position_error')
    await r.choose('reject_selected_target')
    r.world.name = ''; hud['target_health'] = None
    original = r.c.target_proposal
    async def absent(frame):
        value = await original(frame)
        if r.world.name:
            return value
        return {**value, 'name':None, 'levels':[], 'invalid_text':False,
            'visual_observation':{'selected_hud':'absent'},
            'visual_provenance':{'continuity_frame_id':frame.frame_id}}
    r.c.target_proposal = absent
    await r.choose('target_cleared')
    await r.choose('retain_cast_error')
    assert r.c.hunt.cast_error['kind'] == kind and r.c.hunt.cast_blocks_acquisition()
    return hud, pending


def debt(h):
    history = h.target_history[h.combat_history_key]
    fields = ('action_failures', 'unresolved_motion', 'failures', 'search_revision', 'search_changes',
        'travel_policy', 'forward_history', 'failed_direction_approaches', 'detour',
        'travel_approach_revision', 'cast_obligation', 'retry_credit', 'retry_credit_source',
        'correction_evidence', 'last_progress_active_at', 'progress_facts', 'rejected_signatures',
        'no_effect_retries', 'correction_rounds')
    return deepcopy(({field:getattr(h, field) for field in fields},
        {field:history.get(field) for field in ('method_revision', 'prior_method_debt', 'rejections', 'combat_failures')},
        h.cast_error['assessment_source'], h.recent_combat))


@pytest.mark.parametrize('kind,text', ERRORS)
@pytest.mark.parametrize('resolution', ['current', 'dismiss'])
def test_active_released_player_cue_has_actual_resolution_and_acquisition(tmp_path, monkeypatch, kind, text, resolution):
    async def run():
        r = Rig(tmp_path)
        try:
            await released(r, monkeypatch, kind, text)
            h = r.c.hunt
            old_debt = debt(h); old_status = h.cast_error['status']
            if resolution == 'dismiss':
                await r.choose('error_not_supported')
            elif kind == 'standing':
                await r.choose('stand_pulse')
                assert h.pending['released_error_stance']['cue_key'].startswith('standing:')
                assert r.sage.calls[-1]['options']['stand_pulse'].binding['hold_seconds'] == .08
                await r.choose('motion_useful')
            else:
                await r.choose('cast_ready')
            assert released_player_resolution(h) and not h.cast_blocks_acquisition()
            assert h.cast_error['status'] == old_status == 'active' and debt(h) == old_debt
            assert not h.retry_credit and h.correction_evidence is None
            await r.choose('target_enemy')
            assert h.pending['family'] == 'target' and len(smites(r)) == 1 and not h.credited_kills
            assert not {'attack_mob_level_1', 'stand_for_cast', 'reassess_cast_ready'} & r.sage.calls[-1]['options'].keys()
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('outcome', ['motion_no_useful_effect', 'unknown'])
def test_failed_or_unknown_standing_uses_existing_motor_debt_without_new_attempt(tmp_path, monkeypatch, outcome):
    async def run():
        r = Rig(tmp_path)
        try:
            await released(r, monkeypatch, *ERRORS[0]); h = r.c.hunt
            owner = h.combat_history_key; key = h.motion_key('forward', 'standing', owner)
            revision = h.target_history[owner]['method_revision']; search_revision = h.search_revision
            await r.choose('stand_pulse')
            if outcome == 'unknown':
                h.archive_pending('offline stance result unassessed')
            else:
                await r.choose(outcome)
            assert h.motion_count('forward', 'standing', owner) == 1
            assert (h.action_failures.get(key,0) + h.unresolved_motion.get(key,0)) == 1
            assert h.target_history[owner]['method_revision'] == revision and h.search_revision == search_revision
            assert h.cast_blocks_acquisition() and not h.retry_credit and not h.progress_facts
            await r.choose('stand_pulse'); await r.choose('motion_no_useful_effect')
            await r.choose(None)
            assert 'stand_pulse' not in r.sage.calls[-1]['options']
            assert 'error_not_supported' in r.sage.calls[-1]['options']
            assert 'unsupported_capability' not in r.sage.calls[-1]['options']
            assert h.motion_count('forward', 'standing', owner) == 2
            await r.choose('error_not_supported')
            assert not h.cast_blocks_acquisition() and h.motion_count('forward', 'standing', owner) == 2
            assert len(smites(r)) == 1 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('bad', ['empty', 'malformed', 'owner', 'receipt', 'cue', 'hash', 'scope', 'unknown', 'new_target'])
def test_marked_stance_is_consumed_without_credit_on_changed_or_invalid_provenance(tmp_path, monkeypatch, bad):
    async def run():
        r = Rig(tmp_path)
        try:
            await released(r, monkeypatch, *ERRORS[0]); h = r.c.hunt
            await r.choose('stand_pulse'); p = h.pending
            revision = h.target_history[h.combat_history_key]['method_revision']; progress = h.last_progress_active_at
            if bad == 'empty':p['released_error_stance'] = {}
            elif bad == 'malformed':p['released_error_stance'] = 'not a marker'
            elif bad == 'owner':p['released_error_stance']['owner'] = 'wrong'
            elif bad == 'receipt':p['released_error_stance']['cast_receipt_id'] = 'wrong'
            elif bad == 'cue':p['released_error_stance']['cue_key'] = 'wrong'
            elif bad == 'hash':p['source_hash'] = 'wrong'
            elif bad == 'scope':p['source_scope']['source'] = 'wrong'
            elif bad == 'unknown':p['receipt']['dispatch_unknown'] = True
            else:r.world.name = 'Different Creature'
            result = await r.c.process(r.world.capture())
            assert result.status == 'grind_reobserve' and h.pending is None
            assert not released_player_resolution(h) and not h.retry_credit and h.correction_evidence is None
            assert h.target_history[h.combat_history_key]['method_revision'] == revision
            assert h.last_progress_active_at == progress and not h.progress_facts
            assert len(smites(r)) == 1 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('bad', ['owner', 'cast_receipt', 'clear_receipt', 'cue', 'hash', 'scope', 'partial', 'input', 'timestamp'])
def test_resolution_proof_cannot_unlock_scouting_after_corruption(tmp_path, monkeypatch, bad):
    async def run():
        r = Rig(tmp_path)
        try:
            await released(r, monkeypatch, *ERRORS[1]); h = r.c.hunt
            await r.choose('cast_ready'); assert not h.cast_blocks_acquisition()
            p = h.cast_error['released_resolution']
            if bad == 'owner':p['owner'] = 'wrong'
            elif bad == 'cast_receipt':p['cast_receipt_id'] = 'wrong'
            elif bad == 'clear_receipt':p['clear_receipt_id'] = 'wrong'
            elif bad == 'cue':p['cue_key'] = 'wrong'
            elif bad == 'hash':p['sha256'] = 'wrong'
            elif bad == 'scope':p['scope']['source'] = 'wrong'
            elif bad == 'partial':p['receipt']['completed'] = False
            elif bad == 'input':p['receipt']['possible_input'] = True
            else:p['captured_at'] = '2020-01-01T00:00:00+00:00'
            before = deepcopy((h.cast_error, h.target_history))
            assert h.cast_blocks_acquisition() and not released_player_resolution(h)
            assert (h.cast_error, h.target_history) == before
        finally:
            await r.close()
    asyncio.run(run())


def test_current_ready_does_not_hide_another_retained_global(tmp_path, monkeypatch):
    async def run():
        r = Rig(tmp_path)
        try:
            await released(r, monkeypatch, *ERRORS[1], extra=[MANA]); h = r.c.hunt
            await r.choose('cast_ready')
            assert h.cast_error['released_resolution']['disposition'] == 'current_ready'
            assert unresolved_nonlocal_cues(h) == [MANA] and h.cast_blocks_acquisition()
            await r.choose('retain_cast_error')
            assert h.cast_error['kind'] == 'mana' and h.no_mana
            await r.choose('rest'); await r.choose('resources_improved'); await r.choose('recovered_resume')
            assert not h.cast_blocks_acquisition() and len(smites(r)) == 1 and not h.credited_kills
            await r.choose('target_enemy')
            assert h.pending['family'] == 'target' and len(smites(r)) == 1
        finally:
            await r.close()
    asyncio.run(run())


def test_current_ready_serializes_next_global_standing_without_refund(tmp_path, monkeypatch):
    async def run():
        r = Rig(tmp_path)
        try:
            await released(r, monkeypatch, *ERRORS[1], extra=[{'kind':ERRORS[0][0], 'text':ERRORS[0][1]}])
            h = r.c.hunt
            await r.choose('cast_ready')
            assert released_player_resolution(h) and h.cast_blocks_acquisition()
            await r.choose('retain_cast_error'); assert h.cast_error['kind'] == 'standing'
            old_debt = debt(h)
            await r.choose('stand_pulse'); await r.choose('motion_useful')
            assert debt(h) == old_debt and not h.cast_blocks_acquisition() and not h.retry_credit
            await r.choose('target_enemy')
            assert len(smites(r)) == 1 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('newer', ['planning', 'travel', 'selected_exit'])
def test_player_state_resolution_preserves_newer_destination_owner(tmp_path, monkeypatch, newer):
    async def run():
        r = Rig(tmp_path)
        try:
            await released(r, monkeypatch, *ERRORS[1]); h = r.c.hunt
            if newer == 'planning':h.planning_requested = True
            elif newer == 'travel':h.plan = {**(h.plan or {}), 'phase':'travel', 'request_id':'newer-destination'}
            else:h.strategy_required = {'reason':'newer choice', 'selected_exit':{'area_id':'other-patch'}}
            route = deepcopy((h.phase, h.plan, h.strategy_required, h.planning_requested, h.compact_stage))
            await r.choose('cast_ready')
            assert released_player_resolution(h) and not h.cast_blocks_acquisition()
            frame = r.world.capture()
            assert not restore_released_acquisition(r.c, frame, await r.c.target_proposal(frame))
            assert (h.phase, h.plan, h.strategy_required, h.planning_requested, h.compact_stage) == route
            assert h.pending is None and not h.retry_credit and not h.progress_facts
        finally:
            await r.close()
    asyncio.run(run())


def test_final_stand_guard_samples_late_positive_target_bar(tmp_path, monkeypatch):
    from sage_wow.agent import grind_resources
    async def run():
        r = Rig(tmp_path)
        try:
            await released(r, monkeypatch, *ERRORS[0]); h = r.c.hunt
            original_proposal = r.c.target_proposal
            async def no_cached_hud(frame):
                value = await original_proposal(frame)
                value.pop('hud', None)
                return value
            r.c.target_proposal = no_cached_hud
            frames = []; original_capture = r.c.capture
            def capture():
                frame = original_capture(); frames.append(frame)
                return frame
            r.c.capture = capture
            current = {}; original_hud = grind_resources.hud_resources
            def late_hud(c, frame):
                hud = original_hud(c, frame)
                if current and frame.frame_id != current['menu_frame']:
                    hud.update(target_health=.5, target_health_confidence=1.)
                return hud
            monkeypatch.setattr(grind_resources, 'hud_resources', late_hud)
            async def inject_after_choice():
                current['menu_frame'] = frames[-1].frame_id
            r.sage.hook = inject_after_choice
            keys = len(r.physical_keys()); old_debt = debt(h)
            result = await r.choose('stand_pulse')
            assert 'stand_pulse' in r.sage.calls[-1]['options']
            assert result.status != 'dispatched' and len(r.physical_keys()) == keys
            assert debt(h) == old_debt and h.pending is None and h.cast_blocks_acquisition()
        finally:
            await r.close()
    asyncio.run(run())
