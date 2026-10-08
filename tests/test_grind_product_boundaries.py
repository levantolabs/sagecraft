"""Focused independent edge trajectories, all through the local fake controller."""
import asyncio

import pytest
from test_grind_product_spec import Rig, scenario


@scenario
async def test_linked_no_mana_error_disables_heal_but_keeps_passive_recovery(r):
    await r.choose('attack_mob_level_1')
    r.world.error = 'Not enough mana'
    await r.choose('cast_error_observed')
    await r.choose('rest')
    assert 'heal_self' not in r.sage.calls[-1]['options']
    assert not any(name.startswith('attack_mob_') for name in r.sage.calls[-1]['options'])
    await r.choose('resources_unchanged')
    await r.choose('rest')
    assert not r.c.stopped and len(r.casts()) == 1


@scenario
async def test_two_ineffective_heals_suppress_heal_and_offer_changed_recovery(r):
    await r.choose('recover_now')
    await r.choose('heal_self')
    r.world.name = 'Test Player'
    await r.choose('resources_unchanged')
    await r.choose('heal_self')
    await r.choose('resources_unchanged')
    await r.choose('rest')
    assert 'heal_self' not in r.sage.calls[-1]['options']
    assert {'rest', 'backward', 'blocked_resources'} <= r.sage.calls[-1]['options'].keys()
    assert r.physical_keys().count(r.controls['lesser_heal']['keycode']) == 2
    assert not r.c.stopped and not r.c.success


@scenario
async def test_must_stand_uses_one_verified_pulse_then_one_cast_probe(r):
    await r.choose('attack_mob_level_1')
    r.world.error = 'You must be standing'
    await r.choose('cast_error_observed')
    await r.choose('stand_pulse')
    assert r.c.hunt.pending['purpose'] == 'standing'
    await r.choose('motion_useful')
    await r.choose('attack_mob_level_1')
    assert r.physical_keys().count(r.controls['forward']['keycode']) == 1
    assert len(r.casts()) == 2
    assert 'stand_pulse' not in r.sage.calls[-1]['options']
    assert not r.c.stopped


@pytest.mark.parametrize('spell', ['smite', 'heal'])
def test_required_smite_unavailable_exits_but_optional_heal_allows_rest(tmp_path, spell):
    async def run():
        r = Rig(tmp_path)
        try:
            await r.start()
            if spell=='heal':await r.choose('recover_now')
            await r.choose('attack_mob_level_1' if spell == 'smite' else 'heal_self')
            r.world.error = 'Spell not learned'
            if spell == 'heal': r.world.name = 'Test Player'
            await r.choose('cast_error_observed' if spell == 'smite' else 'recovery_error_observed')
            if spell == 'smite':
                assert r.c.stopped and not r.c.success and r.c.reason == 'unsupported_capability'
            else:
                await r.choose('rest')
                assert 'heal_self' not in r.sage.calls[-1]['options']
                assert not r.c.stopped and not r.c.success
        finally: await r.close()
    asyncio.run(run())


@scenario
async def test_blocked_resources_ignore_animation_but_allow_changed_resource_assessment(r):
    await r.choose('recover_now')
    await r.choose('rest')
    await r.choose('blocked_resources')
    assert r.c.hunt.blocked['reason'] == 'blocked_resources'
    before = len(r.sage.calls)
    # Changed frame ids and unrelated selected-health animation carry no resource authority.
    for color in ('yellow', 'orange', 'red'):
        r.world.health = color
        r.c.hunt.blocked['next_observation_at'] = 0
        await r.c.process(r.world.capture())
    assert len(r.sage.calls) == before
    assert r.c.hunt.blocked['interval'] == 60
    r.world.player_health = 'yellow'
    r.c.hunt.blocked['next_observation_at'] = 0
    await r.choose('blocked_changed_assessment')
    assert r.c.hunt.blocked is None
    assert not r.c.stopped and not r.casts()


@scenario
async def test_null_own_badge_preserves_level_and_waits_for_schedule(r):
    r.c.level.last_attempt_at=0
    await r.choose(None)
    assert not r.c.stopped and not r.c.success and not r.c.hunt.blocked
    assert r.c.level.last_confirmed_level==1 and not r.c.null_answers
    requests=len(r.sage.calls)
    await r.choose('attack_mob_level_1')
    assert len(r.sage.calls)==requests+1 and 'player_level_1' not in r.sage.calls[-1]['options']


@scenario
async def test_scheduled_levels_three_and_four_update_exact_attack_band(r):
    # Exercise scheduled checks with this fixture's default relative band.
    # Healthy idle hunting no longer offers an unnecessary recovery declaration.
    r.c.config['goal_level']=5
    r.world.name = ''
    for level in (3, 4):
        r.c.level.last_attempt_at = 0
        await r.choose(f'player_level_{level}')
        assert r.c.level.last_confirmed_level == level
    r.world.name = 'Young Wolf'; r.world.target_level = 1
    await r.choose('reject_selected_target')
    assert not any(name.startswith('attack_mob_') for name in r.sage.calls[-1]['options'])
    assert not r.c.stopped and not r.c.success and not r.casts()


@scenario
async def test_three_ineffective_corrected_probes_require_abandon_and_preserve_debt(r):
    await r.choose('attack_mob_level_1')
    for index in range(3):
        r.world.error = ('Target not in line of sight' if index % 2 == 0 else 'Cannot see target')
        await r.choose('position_error')
        await r.choose('turn_left')
        await r.choose('motion_useful')
        await r.choose('attack_mob_level_1')
    r.world.error = 'Cannot see target'
    await r.choose('position_error')
    debt = r.c.hunt.correction_rounds
    assert debt >= 3
    await r.choose('reject_selected_target')
    assert not any(name.startswith('attack_mob_') for name in r.sage.calls[-1]['options'])
    r.world.name = ''
    await r.choose('target_cleared')
    await r.choose('target_enemy')
    r.world.name = 'Young Wolf'
    await r.choose('reject_selected_target')
    assert r.c.hunt.correction_rounds >= debt
    assert not r.c.success and not r.c.stopped
    # A linked search turn followed by actual reacquisition and fresh living
    # assessment establishes a local attempt, not measured translation or a GUID.
    r.world.name = ''
    await r.choose('target_cleared')
    await r.choose('turn_right')
    assert r.c.hunt.pending['purpose'] == 'search'
    await r.choose('motion_useful')
    await r.choose('target_enemy')
    r.world.name = 'Young Wolf'
    r.world.target_level = 1
    r.world.error = ''
    from test_grind_committed_combat import add_current_observer
    add_current_observer(r)
    await r.choose('attack_mob_level_1')
    assert r.c.hunt.encounter == 2 and r.c.hunt.correction_rounds == 0
    assert any(history['correction_rounds']>=debt for history in r.c.hunt.target_history.values())
    assert not r.c.hunt.credited_kills
