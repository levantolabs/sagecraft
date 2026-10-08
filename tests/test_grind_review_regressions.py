"""Independent production trajectories for the eight review blockers."""
import asyncio
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest
from PIL import Image
from test_grind_product_spec import Rig, scenario


async def choose(r, answer):
    """Never let a fake-provider assertion become an unnoticed transport failure."""
    result = await r.choose(answer)
    assert result.status == 'dispatched', (answer, result.status, result.detail)
    assert result.decision.chosen == answer
    return result


@scenario
async def test_abandoned_debt_is_archived_while_new_sector_mob_can_fight(r):
    await choose(r, 'attack_mob_level_1')
    for text in ('Target not in line of sight', 'Cannot see target', 'Target not in line of sight'):
        r.world.error = text
        await choose(r, 'position_error')
        await choose(r, 'turn_left')
        await choose(r, 'motion_useful')
        await choose(r, 'attack_mob_level_1')
    r.world.error = 'Cannot see target'
    await choose(r, 'position_error')
    failed_encounter = r.c.hunt.encounter
    await choose(r, 'reject_selected_target')
    r.world.name = ''; r.world.error = ''
    await choose(r, 'target_cleared')
    await choose(r, 'turn_left')
    assert r.c.hunt.pending['purpose'] == 'search'
    await choose(r, 'motion_useful')
    await choose(r, 'target_enemy')
    r.world.name = 'Young Boar'
    await choose(r, 'attack_mob_level_1')
    assert r.c.hunt.encounter > failed_encounter
    assert any(item['encounter_id'] == failed_encounter and item['outcome'] == 'combat_unchanged'
               for item in r.c.hunt.outcomes)
    assert not r.c.stopped and not r.c.success


@scenario
async def test_rest_supplies_immutable_before_image_and_focus_retires_authority(r):
    r.world.player_health = '#123456'; r.world.health = 'black'
    if r.c.hunt.phase!='recover':await choose(r,'recover_now')
    await choose(r, 'rest')
    before = dict(r.c.hunt.rest_before)
    original = hashlib.sha256(Path(before['source_image']).read_bytes()).hexdigest()
    snapshots = []
    provider = r.sage.decide_image_choice
    async def inspect(path, *args, **kwargs):
        with Image.open(path) as image:
            rgb = image.convert('RGB')
            snapshots.append(set(rgb.get_flattened_data()))
        return await provider(path, *args, **kwargs)
    r.sage.decide_image_choice = inspect
    r.world.player_health = '#654321'
    await choose(r, 'resources_improved')
    assert (0x12, 0x34, 0x56) in snapshots[-1]
    assert (0x65, 0x43, 0x21) in snapshots[-1]
    assert hashlib.sha256(Path(before['source_image']).read_bytes()).hexdigest() == original
    assert r.c.hunt.rest_before is None
    if r.c.hunt.phase!='recover':await choose(r,'recover_now')
    await choose(r, 'rest')
    r.c.pause_focus()
    assert r.c.hunt.rest_before is None
    assert r.c.resume_focus()
    await choose(r, 'world_normal_confirmed')
    assert 'resources_improved' not in r.sage.calls[-1]['options']


@scenario
async def test_resource_assessment_does_not_consume_a_retreat_receipt(r):
    if r.c.hunt.phase!='recover':await choose(r,'recover_now')
    await choose(r, 'rest')
    await choose(r, 'backward')
    motion_receipt = r.c.hunt.pending['receipt']['receipt_id']
    r.world.player_health = 'yellow'
    # A new physical action retires the semantic rest comparison.
    await choose(r, 'motion_no_useful_effect')
    assert 'resources_improved' not in r.sage.calls[-1]['options']
    assert not any(item['receipt_id'] == motion_receipt and item['outcome'].startswith('resources_')
                   for item in r.c.hunt.outcomes)
    assert any(item['receipt_id'] == motion_receipt and item['outcome'] == 'motion_no_useful_effect'
               for item in r.c.hunt.outcomes)


@pytest.mark.parametrize('dead', [False,True])
def test_scheduled_badge_crop_uses_only_own_frame_and_retains_death_exit(tmp_path,dead):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.c.level.last_attempt_at=0
            if dead:r.world.player_health='black'
            await choose(r,'player_dead' if dead else 'player_level_2')
            assert ('player_dead' in r.sage.calls[-1]['options'])==dead
            assert 'urgent_threat' not in r.sage.calls[-1]['options']
            assert not r.casts() and not r.c.success
            assert r.c.stopped==dead
        finally:await r.close()
    asyncio.run(run())


@scenario
async def test_strategy_and_resource_declarations_do_not_replenish_failed_methods(r):
    r.world.name = ''
    for _ in range(2):
        await choose(r, 'forward')
        await choose(r, 'motion_no_useful_effect')
    # Current acquisition policy requires observed unsuccessful selections
    # before offering a local strategy change; motion debt alone is not absence.
    for _ in range(2):
        await choose(r, 'target_enemy')
        await choose(r, 'no_selected_frame')
    r.c.hunt.exhausted_patches = 3
    debt=deepcopy((r.c.hunt.action_failures,r.c.hunt.unresolved_motion))
    await choose(r,'change_search_strategy')
    await choose(r,'explore_visible')
    assert 'search_here' not in r.sage.calls[-1]['options']
    assert (r.c.hunt.action_failures,r.c.hunt.unresolved_motion)==debt
    # The concrete destination declaration and survival roundtrip retain
    # failed local methods; neither claims that physical progress occurred.
    await choose(r,'urgent_state')
    await choose(r, 'rest')
    assert 'change_search_strategy' not in r.sage.calls[-1]['options']
    r.c.hunt.active_seconds += 91
    await choose(r, 'reassess_recovery')
    assert (r.c.hunt.action_failures,r.c.hunt.unresolved_motion)==debt
    await choose(r, 'backward')
    assert 'forward' not in r.sage.calls[-1]['options']
    assert not r.c.stopped and not r.c.success


@scenario
async def test_unresolved_changed_blocked_view_becomes_the_new_baseline(r):
    if r.c.hunt.phase!='recover':await choose(r,'recover_now')
    await choose(r, 'rest')
    await choose(r, 'blocked_resources')
    old_signature = r.c.hunt.blocked['evidence_signature']
    r.world.player_health = 'yellow'
    r.c.hunt.blocked['next_observation_at'] = 0
    await choose(r, 'blocked_still_unresolved')
    assert r.c.hunt.blocked['evidence_signature'] != old_signature
    calls = len(r.sage.calls)
    r.c.hunt.blocked['next_observation_at'] = 0
    r.c.wait_until = 0
    await r.c.process(r.world.capture())
    assert len(r.sage.calls) == calls and r.c.hunt.blocked is not None
    assert not r.casts()


@scenario
async def test_optional_unknown_heal_never_claims_essential_capability_failure(r):
    if r.c.hunt.phase!='recover':await choose(r,'recover_now')
    await choose(r, 'heal_self')
    r.world.name = 'Test Player'; r.world.error = 'Spell not learned'
    await choose(r, 'recovery_error_observed')
    assert 'unsupported_capability' not in r.sage.calls[-1]['options']
    if r.c.hunt.phase!='recover':await choose(r,'recover_now')
    await choose(r, 'rest')
    assert 'unsupported_capability' not in r.sage.calls[-1]['options']
    assert not r.c.stopped and not r.c.success


@pytest.mark.parametrize('badge', [False, True])
def test_plain_loading_vetoes_world_and_badge_actions(tmp_path, badge):
    async def run():
        r = Rig(tmp_path)
        try:
            await r.start()
            if badge:r.c.level.last_attempt_at=0
            r.world.error = 'Loading'
            before = list(r.backend.events)
            await choose(r, 'loading_wait')
            assert all(item.binding.get('type') == 'observe_only'
                       for item in r.sage.calls[-1]['options'].values())
            assert not any(name.startswith('player_level_') for name in r.sage.calls[-1]['options'])
            assert all(event[0] == 'release' for event in r.backend.events[len(before):])
            assert not r.c.success
        finally: await r.close()
    asyncio.run(run())


@scenario
async def test_crowded_level_three_verified_strafes_preserve_outcome_and_safety_slots(r):
    r.c.config['goal_level']=5  # Exercise continuing combat above the level-three goal.
    for name, code in [('strafe_left', 20), ('strafe_right', 21)]:
        r.controls[name] = {'keycode': code, 'verified_from': 'offline operator'}
    r.c.level.last_attempt_at=0
    await choose(r, 'player_level_3')
    r.world.target_level = None
    await choose(r, 'attack_mob_level_1')
    menu = r.sage.calls[-1]['options']
    assert {'attack_mob_level_1', 'attack_mob_level_2', 'attack_mob_level_3'} <= menu.keys()
    assert 'dead_or_unrecoverable' not in menu
    r.world.error = 'Target not in line of sight'
    await choose(r, 'position_error')
    assert 'position_error' in r.sage.calls[-1]['options']
    assert 'dead_or_unrecoverable' not in r.sage.calls[-1]['options']
    await choose(r, 'strafe_left')
    await choose(r, 'motion_useful')
    assert {'motion_useful', 'cannot_assess'} <= r.sage.calls[-1]['options'].keys()
    assert 'dead_or_unrecoverable' not in r.sage.calls[-1]['options']
    if r.c.hunt.phase!='recover':await choose(r,'recover_now')
    await choose(r, 'heal_self')
    r.world.name = 'Test Player'
    await choose(r, 'resources_unchanged')
    assert {'resources_improved', 'resources_unchanged', 'rest', 'backward'} <= r.sage.calls[-1]['options'].keys()
    assert 'dead_or_unrecoverable' not in r.sage.calls[-1]['options']
    assert all(len(call['options']) <= 20 for call in r.sage.calls)
    assert r.controls['strafe_left']['keycode'] in r.physical_keys()


@scenario
async def test_correction_then_rest_pairs_the_actual_resource_baseline(r):
    r.world.player_health = '#123456'; r.world.health = 'black'
    await choose(r, 'attack_mob_level_1')
    r.world.error = 'Target not in line of sight'
    await choose(r, 'position_error')
    await choose(r, 'turn_left')
    await choose(r, 'motion_useful')
    assert r.c.hunt.correction_evidence is not None
    r.world.player_health = '#456789'
    if r.c.hunt.phase!='recover':await choose(r,'recover_now')
    await choose(r, 'rest')
    rest = dict(r.c.hunt.rest_before)
    observed = []
    provider = r.sage.decide_image_choice
    async def inspect(path, *args, **kwargs):
        with Image.open(path) as image:
            observed.append(set(image.convert('RGB').get_flattened_data()))
        return await provider(path, *args, **kwargs)
    r.sage.decide_image_choice = inspect
    r.world.player_health = '#ABCDEF'
    await choose(r, 'resources_improved')
    assert (0x45, 0x67, 0x89) in observed[-1], 'Rest outcome received the movement baseline'
    assert (0xAB, 0xCD, 0xEF) in observed[-1]
    assert hashlib.sha256(Path(rest['source_image']).read_bytes()).hexdigest() == rest['source_hash']
    assert r.c.hunt.rest_before is None and not r.c.stopped


@pytest.mark.parametrize('action', ['attack_mob_level_1', 'forward'])
def test_direct_rest_reconciles_interrupted_physical_action_once(tmp_path, action):
    async def run():
        r = Rig(tmp_path)
        try:
            await r.start()
            await choose(r, action)
            receipt = r.c.hunt.pending['receipt']['receipt_id']
            if r.c.hunt.phase!='recover':await choose(r,'recover_now')
            await choose(r, 'rest')
            assert r.c.hunt.pending is None and r.c.hunt.rest_before is not None
            outcomes = [item for item in r.c.hunt.outcomes if item['receipt_id'] == receipt]
            assert len(outcomes) == 1 and outcomes[0]['outcome'] == 'unknown'
            debt = dict(r.c.hunt.action_failures)
            await choose(r, 'resources_improved')
            assert {'resources_improved', 'resources_unchanged'} <= r.sage.calls[-1]['options'].keys()
            assert all(r.c.hunt.action_failures.get(key, 0) >= value for key, value in debt.items())
            assert len([item for item in r.c.hunt.outcomes if item['receipt_id'] == receipt]) == 1
            assert not r.c.stopped and not r.c.success
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('error', ['Interrupted', 'Spell not ready yet'])
def test_cast_readiness_acknowledgment_is_consumed_for_one_retry(tmp_path, error):
    async def run():
        r = Rig(tmp_path)
        try:
            await r.start()
            await choose(r, 'attack_mob_level_1')
            r.world.error = error
            await choose(r, 'cast_error_observed')
            await choose(r, 'cast_ready')
            await choose(r, 'attack_mob_level_1')
            assert 'cast_ready' not in r.sage.calls[-1]['options']
            assert len(r.casts()) == 2
            await choose(r, 'no_effect')
            if r.c.hunt.phase!='recover':await choose(r,'recover_now')
            await choose(r, 'rest')
            assert 'cast_ready' not in r.sage.calls[-1]['options']
            assert not any(name.startswith('attack_mob_') for name in r.sage.calls[-1]['options'])
            assert len(r.casts()) == 2 and not r.c.stopped
        finally: await r.close()
    asyncio.run(run())


@scenario
async def test_separated_loading_episodes_have_independent_active_time_budgets(r):
    r.world.error = 'Loading'
    await choose(r, 'loading_wait')
    first = r.c.loading_started
    r.c.hunt.active_seconds += 80
    await choose(r, 'loading_wait')
    assert r.c.hunt.blocked is None
    r.world.error = ''; r.world.name = ''
    await choose(r, 'target_enemy')
    assert r.c.loading_started is None
    r.c.hunt.active_seconds += 20
    r.world.error = 'Loading'
    await choose(r, 'loading_wait')
    assert r.c.loading_started >= first + 100
    assert r.c.hunt.blocked is None
    r.c.hunt.active_seconds += 88
    await choose(r, 'loading_wait')
    assert r.c.hunt.blocked is None
    r.c.hunt.active_seconds += 3
    await choose(r, 'loading_wait')
    assert r.c.hunt.blocked['reason'] == 'blocked_loading'
    assert not r.c.stopped and not r.c.success

@scenario
async def test_nonloading_badge_confirmation_retires_the_previous_loading_episode(r):
    r.c.level.last_attempt_at=0
    r.world.error = 'Loading'
    await choose(r, 'loading_wait')
    first = r.c.loading_started
    r.c.hunt.active_seconds += 80
    await choose(r, 'loading_wait')
    r.world.error = ''
    r.c.level.last_attempt_at=0
    await choose(r, 'player_level_2')
    assert r.c.level.last_confirmed_level == 2 and r.c.loading_started is None
    r.c.hunt.active_seconds += 20
    r.world.error = 'Loading'
    await choose(r, 'loading_wait')
    assert r.c.loading_started >= first + 100
    assert r.c.hunt.blocked is None
    r.world.error = ''
    r.c.level.last_attempt_at=0
    await choose(r, 'player_level_2')
    assert r.c.level.last_confirmed_level == 2 and r.c.loading_started is None


@scenario
async def test_nonloading_blocked_observation_retires_loading_before_early_return(r):
    r.world.error = 'Loading'
    await choose(r, 'loading_wait')
    r.c.hunt.active_seconds += 91
    await choose(r, 'loading_wait')
    assert r.c.hunt.blocked['reason'] == 'blocked_loading'
    r.world.error = ''
    calls = len(r.sage.calls)
    result = await r.c.process(r.world.capture())
    assert result.status == 'grind_blocked' and len(r.sage.calls) == calls
    assert r.c.loading_started is None
    r.c.hunt.blocked['next_observation_at'] = 0
    await choose(r, 'blocked_changed_assessment')
    r.world.error = 'Loading'
    await choose(r, 'loading_wait')
    assert r.c.hunt.blocked is None and r.c.loading_started >= 91
