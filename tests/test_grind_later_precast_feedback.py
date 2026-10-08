"""Real burst producer/controller handoffs on offline images and fake native input."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from sage_wow.agent import grind_perception
from sage_wow.agent.grind_cast_feedback import (
    unresolved_cues, unresolved_nonlocal_cues, retained_error_inventory,
)
from sage_wow.agent.scene import Scene
from test_grind_product_spec import Rig
from test_active_attacker_recovery import start, install_hud, smites


FACING = {'kind': 'facing', 'text': 'Target is not in front of you'}
MANA = {'kind': 'mana', 'text': 'Not enough mana'}


async def guard_cast(r, monkeypatch, cues=(FACING,), *, stop_index=1):
    await start(r)
    hud, _ = install_hud(r)
    r.c.config.update(smite_burst_count=3, cast_wait_seconds=1.5,
        target_name_box=[210, 100, 370, 118])
    active = []

    def scene(frame, **kwargs):
        value = Scene(frame.frame_id)
        value.target_name = r.world.name
        value.target_alive = True
        value.target_health = 1.
        value.target_health_confidence = 1.
        value.health = 1.
        value.health_confidence = 1.
        value.error_cues = deepcopy(active)
        value.error = active[0]['text'] if active else None
        return value

    monkeypatch.setattr(grind_perception, 'read_hud_scene', scene)

    async def guard(binding, index):
        active[:] = deepcopy(cues) if index == stop_index else []
        r.world.error = '; '.join(cue['text'] for cue in active)
        try:
            return await grind_perception.observe_burst(r.c, binding, index)
        finally:
            r.world.error = ''
            active.clear()

    async def post(binding, index, phase):
        return await grind_perception.observe_post_cast(r.c, binding, index, phase)

    async def no_wait(seconds):
        pass

    r.executor.batch_guard = guard
    r.executor.batch_post_guard = post
    r.executor._batch_sleep = no_wait
    await r.choose('attack_mob_level_1')
    return hud, r.c.hunt.pending


@pytest.mark.parametrize('stop_index', [1, 2])
def test_real_producer_stopped_burst_enters_existing_mixed_error_question(tmp_path, monkeypatch, stop_index):
    async def run():
        r = Rig(tmp_path)
        try:
            hud, pending = await guard_cast(r, monkeypatch, stop_index=stop_index)
            trace = pending['receipt']['execution']
            assert len(trace['completed_steps']) == stop_index and trace['burst_completed'] is False
            assert not trace['post_cast_observations'][0]['observed_error_cues']
            guard = trace['guard_observations'][stop_index]
            assert guard['phase'] == 'pre_cast' and guard['step_index'] == stop_index
            assert guard['identity_stable'] is True
            assert guard['source'] == pending['source_scope']['source']
            assert guard['controller_revision'] == pending['receipt']['scope_objective_revision']
            assert guard['identity_source']['frame_id'] == trace['guard_observations'][0]['frame_id']
            assert unresolved_cues(pending) == [FACING]
            assert pending['position_error_evidence']['source'] == 'attributable_later_precast_guard'
            hud['target_health'] = .4
            await r.choose('position_error')
            h = r.c.hunt
            assert h.cast_error['kind'] == 'facing' and h.cast_obligation
            assert h.cast_error['assessment_source']['receipt']['receipt_id'] == pending['receipt']['receipt_id']
            await r.choose('reassess_cast_ready')
            assert len(smites(r)) == stop_index and not h.credited_kills and not h.own_damage_evidence
        finally:
            await r.close()
    asyncio.run(run())


def test_real_preinput_guard_error_submits_no_pulse_and_creates_no_feedback_owner(tmp_path, monkeypatch):
    async def run():
        r = Rig(tmp_path)
        try:
            _, pending = await guard_cast(r, monkeypatch, stop_index=0)
            assert pending is None and not smites(r)
            receipt = r.c.cycle.last_receipt
            assert not receipt['possible_input'] and not receipt['execution']['completed_steps']
            assert receipt['execution']['guard_observations'][0]['observed_error_cues'] == [FACING]
            assert not unresolved_nonlocal_cues(r.c.hunt) and not r.c.hunt.cast_error
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('bad', ['index0', 'preexisting', 'selection', 'target', 'identity',
    'missing_phase', 'missing_revision', 'missing_identity_source', 'missing_dimensions',
    'hash', 'source_hash', 'scope', 'epoch', 'revision', 'continuity', 'owner',
    'step_index', 'step_dispatched', 'step_frame', 'submission_order', 'capture_before_source',
    'capture_after_receipt', 'partial', 'unknown', 'receipt_source', 'primitive_unknown',
    'execution_incomplete', 'prior_guard_error', 'initial_reference_hash', 'dimensions'])
def test_new_guard_evidence_requires_actual_capture_and_complete_pulse_provenance(tmp_path, monkeypatch, bad):
    async def run():
        r = Rig(tmp_path)
        try:
            _, pending = await guard_cast(r, monkeypatch)
            trace = pending['receipt']['execution']
            guard = trace['guard_observations'][1]
            step = trace['completed_steps'][0]
            assert unresolved_cues(pending) == [FACING]
            pending.pop('position_error_evidence')
            if bad == 'index0':
                trace['guard_observations'][0]['observed_error_cues'] = [FACING]
                trace['guard_observations'] = trace['guard_observations'][:1]
            elif bad == 'preexisting':pending['measurement']['error_cues'] = [FACING]
            elif bad == 'selection':guard['selection_revision'] += 1
            elif bad == 'target':guard['observed_target_name'] = 'Different creature'
            elif bad == 'identity':guard['identity_stable'] = False
            elif bad.startswith('missing_'):
                guard.pop({'missing_phase':'phase', 'missing_revision':'controller_revision',
                    'missing_identity_source':'identity_source', 'missing_dimensions':'width'}[bad])
            elif bad == 'hash':guard['image_sha256'] = 'bad'
            elif bad == 'source_hash':pending['source_hash'] = 'bad'
            elif bad == 'scope':guard['source'] = 'another window'
            elif bad == 'epoch':guard['session_epoch'] = 'another epoch'
            elif bad == 'revision':guard['controller_revision'] += 1
            elif bad == 'continuity':guard['target_continuity'] += 1
            elif bad == 'owner':guard['target_history_key'] = 'another owner'
            elif bad == 'step_index':step['index'] = 2
            elif bad == 'step_dispatched':step['dispatched'] = False
            elif bad == 'step_frame':step['guard_frame_id'] = 'other frame'
            elif bad == 'submission_order':step['submitted_at_monotonic'] = guard['captured_at_monotonic'] + 1
            elif bad == 'capture_before_source':
                guard['captured_at'] = (datetime.fromisoformat(pending['measurement']['captured_at']) - timedelta(seconds=1)).isoformat()
            elif bad == 'capture_after_receipt':
                guard['captured_at'] = (datetime.fromisoformat(pending['receipt']['occurred_at']) + timedelta(seconds=1)).isoformat()
            elif bad == 'partial':pending['receipt']['completed'] = False
            elif bad == 'unknown':pending['receipt']['dispatch_unknown'] = True
            elif bad == 'primitive_unknown':pending['receipt']['input_steps'][0]['status'] = 'effect_unknown'
            elif bad == 'execution_incomplete':trace['completed'] = False
            elif bad == 'prior_guard_error':trace['guard_observations'][0]['continue'] = False
            elif bad == 'initial_reference_hash':guard['identity_source']['image_sha256'] = 'bad'
            elif bad == 'dimensions':guard['width'] += 1
            else:pending['receipt']['source_frame_id'] = 'other source'
            before = deepcopy(pending)
            assert retained_error_inventory(pending) == []
            assert pending == before  # Inventory/readiness cannot repair metadata or mutate debt.
            assert unresolved_cues(pending) == []
            assert len(smites(r)) == 1 and not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_retained_global_capability_after_clear_uses_existing_terminal_resolution(tmp_path, monkeypatch):
    async def run():
        r = Rig(tmp_path)
        try:
            capability = {'kind':'unavailable_spell', 'text':'Spell not learned'}
            hud, pending = await guard_cast(r, monkeypatch, [FACING, capability])
            await r.choose('position_error')
            await r.choose('reject_selected_target')
            r.world.name = ''; hud['target_health'] = None
            original = r.c.target_proposal
            async def absent(frame):
                value = await original(frame)
                return {**value, 'name':None, 'levels':[], 'invalid_text':False,
                    'visual_observation':{'selected_hud':'absent'},
                    'visual_provenance':{'continuity_frame_id':frame.frame_id}}
            r.c.target_proposal = absent
            await r.choose('target_cleared')
            assert r.c.hunt.cast_blocks_acquisition()
            await r.choose('retain_cast_error')
            assert r.c.stopped and r.c.reason == 'unsupported_capability'
            assert r.c.hunt.cast_error['cast_receipt_id'] == pending['receipt']['receipt_id']
            assert len(smites(r)) == 1 and not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_unassessed_global_guard_interrupts_before_any_error_assessment_then_clear(tmp_path, monkeypatch):
    async def run():
        r = Rig(tmp_path)
        try:
            hud, pending = await guard_cast(r, monkeypatch, [FACING, MANA])
            await r.choose('ui_blocked')
            await r.choose('world_normal_confirmed')
            h = r.c.hunt
            assert h.pending is None and h.cast_error is None and h.cast_obligation
            original = r.c.target_proposal
            async def uncertain_or_absent(frame):
                value = await original(frame)
                if r.world.name:
                    # Existing selected-HUD uncertainty prevents a living-only
                    # historical review, while leaving guarded clear available.
                    return {**value, 'eligibility':'unknown',
                        'visual_observation':{'selected_hud':'present', 'life_state':'unknown'}}
                return {**value, 'name':None, 'levels':[], 'invalid_text':False,
                    'visual_observation':{'selected_hud':'absent'},
                    'visual_provenance':{'continuity_frame_id':frame.frame_id}}
            r.c.target_proposal = uncertain_or_absent
            await r.choose('reject_selected_target')
            r.world.name = ''; hud['target_health'] = None
            await r.choose('target_cleared')
            assert h.intentional_clear_current()['disposition'] == 'closed'
            assert h.cast_error is None and h.cast_obligation and h.cast_blocks_acquisition()
            await r.choose('retain_cast_error')
            assert h.cast_error['kind'] == 'mana' and h.no_mana
            assert h.cast_error['cast_receipt_id'] == pending['receipt']['receipt_id']
            await r.choose('rest'); await r.choose('resources_improved'); await r.choose('recovered_resume')
            assert not h.cast_blocks_acquisition() and not h.no_mana and len(smites(r)) == 1
            assert not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_corrupt_matching_error_disposition_cannot_hide_retained_global_cue(tmp_path, monkeypatch):
    async def run():
        r = Rig(tmp_path)
        try:
            _, pending = await guard_cast(r, monkeypatch, [MANA])
            h = r.c.hunt
            h.archive_pending('offline feedback interruption')
            h.cast_error = {**MANA, 'cast_receipt_id':pending['receipt']['receipt_id'],
                'status':'retired_by_resource_recovery', 'assessment_source':None}
            before = deepcopy((h.cast_error, h.target_history, h.recent_combat))
            assert unresolved_nonlocal_cues(h) == [MANA]
            assert (h.cast_error, h.target_history, h.recent_combat) == before
            assert len(smites(r)) == 1 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('bad', ['owner', 'receipt', 'hash', 'scope', 'partial'])
def test_unattributed_historical_global_text_cannot_create_acquisition_veto(tmp_path, monkeypatch, bad):
    async def run():
        r = Rig(tmp_path)
        try:
            _, pending = await guard_cast(r, monkeypatch)
            h = r.c.hunt
            h.archive_pending('offline feedback interruption')
            source = h.target_history[h.combat_history_key]['unassessed_error_feedback']['source']
            source['receipt']['execution']['guard_observations'][1]['observed_error_cues'] = [MANA]
            source.pop('position_error_evidence', None)
            if bad == 'owner':source['target_history_key'] = 'unrelated owner'
            elif bad == 'receipt':source['receipt']['receipt_id'] = 'unrelated receipt'
            elif bad == 'hash':source['source_hash'] = 'corrupt'
            elif bad == 'scope':source['source_scope']['source'] = 'unrelated window'
            else:source['receipt']['completed'] = False
            h.encounter_ended = True
            before = deepcopy((h.target_history, h.recent_combat))
            assert unresolved_nonlocal_cues(h) == [] and not h.cast_blocks_acquisition()
            assert (h.target_history, h.recent_combat) == before
            assert len(smites(r)) == 1 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('first', ['facing', 'mana'])
def test_multi_guard_errors_keep_order_and_individual_disposition(tmp_path, monkeypatch, first):
    async def run():
        r = Rig(tmp_path)
        try:
            cues = [FACING, MANA] if first == 'facing' else [MANA, FACING]
            _, pending = await guard_cast(r, monkeypatch, cues)
            assert unresolved_cues(pending) == cues
            await r.choose('error_not_supported')
            assert unresolved_cues(pending) == cues[1:]
            await r.choose('position_error' if first == 'mana' else 'cast_error_observed')
            assert r.c.hunt.cast_error['kind'] == cues[1]['kind']
            assert len(smites(r)) == 1 and not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('obligation', ['local', 'unknown', 'ineffective'])
@pytest.mark.parametrize('resolution', ['dismiss', 'resources', 'focus_resources'])
def test_interrupted_global_guard_clear_retains_real_resolution_path(tmp_path, monkeypatch, obligation, resolution):
    from sage_wow.agent.grind_search import INEFFECTIVE_COMPLETED_CAST
    async def run():
        r = Rig(tmp_path)
        try:
            hud, pending = await guard_cast(r, monkeypatch, [FACING, MANA])
            await r.choose('position_error')
            r.c.require_world = True  # Existing blocking-UI confirmation route.
            await r.choose('world_normal_confirmed')
            h = r.c.hunt
            assert h.pending is None and h.cast_error['kind'] == 'facing'
            assert h.target_history[h.combat_history_key]['unassessed_error_feedback']['status'] == 'unassessed'
            await r.choose('reject_selected_target')
            r.world.name = ''; hud['target_health'] = None
            original = r.c.target_proposal
            async def absent(frame):
                target = await original(frame)
                return {**target, 'name':None, 'levels':[], 'invalid_text':False,
                    'visual_observation':{'selected_hud':'absent'},
                    'visual_provenance':{'continuity_frame_id':frame.frame_id}}
            r.c.target_proposal = absent
            await r.choose('target_cleared')
            assert h.intentional_clear_current()['disposition'] == 'closed'
            if obligation != 'local':
                # Existing direct ended uncertainty allowance is the negative
                # control: a retained global must block with no active error.
                h.cast_error = None
                from sage_wow.agent.grind_search import UNKNOWN_COMPLETED_CAST
                h.cast_obligation = INEFFECTIVE_COMPLETED_CAST if obligation == 'ineffective' else UNKNOWN_COMPLETED_CAST
            debt = deepcopy((h.target_history, h.recent_combat, h.action_failures, h.progress_facts))
            assert unresolved_nonlocal_cues(h) == [MANA] and h.cast_blocks_acquisition()
            assert (h.target_history, h.recent_combat, h.action_failures, h.progress_facts) == debt
            if resolution == 'focus_resources':
                r.c.pause_focus(); r.c.resume_focus()
                await r.choose('world_normal_confirmed')
            if resolution == 'dismiss':
                await r.choose('error_not_supported')
                assert not unresolved_nonlocal_cues(h) and not h.cast_blocks_acquisition()
            else:
                await r.choose('retain_cast_error')
                assert h.no_mana and h.phase == 'recover'
                await r.choose('rest'); await r.choose('resources_improved'); await r.choose('recovered_resume')
                assert not h.no_mana and not unresolved_nonlocal_cues(h) and not h.cast_blocks_acquisition()
            assert len(smites(r)) == 1 and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())
