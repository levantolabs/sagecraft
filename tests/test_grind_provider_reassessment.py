"""Provider recovery restores assessment, never gameplay authority or credit."""
from test_grind_product_spec import retain_legacy_no_effect
import asyncio
from copy import deepcopy

import pytest

from test_grind_product_spec import Rig
from test_grind_acquisition_opening import enable, acquire, process
from test_grind_opener_baseline import mana_pixels


@pytest.mark.parametrize('reason', ['blocked_timing', 'blocked_provider'])
@pytest.mark.parametrize('missing', [False, True])
def test_readable_reassessment_preserves_debt_then_returns_to_real_actions(tmp_path, reason, missing):
    async def run():
        r = Rig(tmp_path)
        try:
            enable(r);mana_pixels(r);await r.start();await acquire(r);await process(r)
            await retain_legacy_no_effect(r);await r.choose('attack_mob_level_1');await retain_legacy_no_effect(r)
            h = r.c.hunt
            debt = deepcopy((h.cast_obligation, h.target_history, h.action_failures, h.encounter, h.credited_kills))
            before = (r.physical_keys(), r.casts())
            r.c.enter_blocked(reason, r.c.last_signature)
            h.blocked.update(next_observation_at=0, assessment_due_at=0)
            if missing:r.world.name = '';r.world.target_level = None
            await r.choose('blocked_changed_assessment')
            call = r.sage.calls[-1]
            assert 'Can the current image be read' in call['instructions']
            assert 'scene is unchanged' in call['instructions']
            assert all(x.binding == {'type': 'observe_only'} for x in call['options'].values())
            assert (r.physical_keys(), r.casts()) == before and h.blocked is None
            assert (h.cast_obligation, h.target_history, h.action_failures, h.encounter, h.credited_kills) == debt
            if missing:
                await r.choose('no_selected_frame')
                await r.choose('target_enemy')
                assert h.pending['family'] == 'target' and r.casts() == before[1]
            else:
                await r.choose('forward')
                assert 'approach_for_range_check' not in r.sage.calls[-1]['options']
                assert h.pending['purpose'] == 'cast_correction'
                assert h.pending['family'] == 'motion' and r.casts() == before[1]
            assert not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_movement_invalidated_cast_review_stays_historical_after_unblock(tmp_path):
    async def run():
        from test_grind_travel_acceptance import TravelRig
        r = TravelRig(tmp_path)
        try:
            enable(r);mana_pixels(r);await r.start();await acquire(r);await process(r)
            await retain_legacy_no_effect(r);await r.choose('attack_mob_level_1');await retain_legacy_no_effect(r)
            h = r.c.hunt; review = deepcopy(h.cast_review)
            h.choose({'id':'offline', 'label':'Offline destination', 'coordinate':[20.,10.],
                'zone_reference':r.zone, 'arrival_radius':.6}, r.world.capture(), 'offline-plan', 1)
            await r.choose('probe_forward');r.position = [10.4,10.]
            # The retained travel receipt is resolved by the normal play path,
            # while a returned selected encounter needs fresh assessment.
            # Model a genuine returned combat task, including its plan owner.
            # A selected HUD alone would not transfer an ordinary travel plan.
            h.plan['phase'] = 'search'
            h.phase = 'fight';h.compact_stage = 'inspect'
            r.c.enter_blocked('blocked_timing', r.c.last_signature)
            h.blocked.update(next_observation_at=0, assessment_due_at=0)
            before = (r.physical_keys(), r.casts()); debt = h.cast_obligation
            await r.choose('blocked_changed_assessment')
            assert (r.physical_keys(), r.casts()) == before
            assert h.cast_review == review and h.cast_obligation == debt
            await r.choose('reject_selected_target')
            assert 'reinspect_cast_problem' not in r.sage.calls[-1]['options']
            r.world.name = '';r.world.target_level = None
            await r.choose('target_cleared');await r.choose('target_enemy')
            assert h.pending['family'] == 'target' and r.casts() == before[1]
            assert h.cast_review == review and h.cast_obligation == debt and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_provider_resume_routes_current_invitation_before_any_world_input(tmp_path):
    async def run():
        from test_grind_loot_integration import LootRig
        from test_grind_guild_invitation import attach_invitation
        r = LootRig(tmp_path)
        try:
            await r.start();attach_invitation(r)
            r.c.enter_blocked('blocked_timing', r.c.last_signature)
            r.c.hunt.blocked.update(next_observation_at=0, assessment_due_at=0)
            before = (r.physical_keys(), r.casts())
            await r.choose('blocked_changed_assessment')
            assert (r.physical_keys(), r.casts()) == before
            await r.choose('close_visible_ui')
            assert set(r.sage.calls[-1]['options']) == {'close_visible_ui', 'decline_invitation'}
            assert r.c.hunt.pending['family'] == 'ui' and not r.casts()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['epoch', 'generation', 'focus'])
def test_stale_resume_answer_cannot_restore_assessment(tmp_path, change):
    async def run():
        r = Rig(tmp_path)
        try:
            await r.start();r.c.enter_blocked('blocked_timing', r.c.last_signature)
            r.c.hunt.blocked.update(next_observation_at=0, assessment_due_at=0)
            before = (r.physical_keys(), r.casts())
            async def invalidate():
                if change == 'epoch':r.c.cycle.session_epoch = 'different-epoch'
                elif change == 'generation':r.c.cycle._input_generation += 1
                else:r.c.pause_focus()
            r.sage.hook = invalidate
            result = await r.choose('blocked_changed_assessment')
            assert result.status != 'dispatched' and r.c.hunt.blocked
            assert (r.physical_keys(), r.casts()) == before
        finally:await r.close()
    asyncio.run(run())


def test_readable_provider_resume_does_not_resolve_unknown_input(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            await r.start();h = r.c.hunt;h.input_effect_unverified = True
            r.c.enter_blocked('blocked_provider', r.c.last_signature)
            h.blocked.update(next_observation_at=0, assessment_due_at=0)
            before = (r.physical_keys(), r.casts())
            await r.choose('blocked_changed_assessment')
            assert h.input_effect_unverified and (r.physical_keys(), r.casts()) == before
            # Even a provider choosing a physical action cannot bypass the
            # independent unknown-input precondition on the next fresh frame.
            r.sage.answers.append('attack_mob_level_1')
            result = await process(r)
            assert result.status == 'precondition_failed' and result.decision.chosen == 'attack_mob_level_1'
            assert h.input_effect_unverified and (r.physical_keys(), r.casts()) == before
        finally:await r.close()
    asyncio.run(run())
