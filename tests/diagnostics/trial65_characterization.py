"""Characterize current defects using synthetic captures/Sage/input, never live.

These checks assert the observed defect, not a repaired acceptance behavior.
Run with repository conftest plugin and both live-disable flags.
"""
import json
from test_grind_committed_combat import scenario, add_current_observer


async def facing_failure(r):
    add_current_observer(r)
    await r.choose('attack_mob_level_1')
    r.world.error='Target needs to be in front of you'
    await r.choose('position_error')
    assert r.c.hunt.cast_error['status']=='active'
    r.world.error=''


@scenario
async def test_later_positive_combat_context_has_no_error_reassessment_path(r):
    await facing_failure(r)
    r.c.combat_log_context=json.dumps({'event':'SPELL_DAMAGE','own_source':True,
        'dest_name':'Young Wolf','details':['585','Smite','2','14'],
        'claim':'Synthetic delayed positive hit on retained encounter for this diagnostic'})
    await r.choose(None)
    options=r.sage.calls[-1]['options']
    assert 'SPELL_DAMAGE' in r.sage.calls[-1]['prompt']
    assert 'attack_mob_level_1' not in options
    assert not {'own_damaged_alive','damaged_alive','error_not_supported'} & options.keys()
    assert r.c.hunt.cast_error['status']=='active' and r.c.hunt.cast_obligation
    assert not r.c.hunt.credited_kills


@scenario
async def test_failed_clear_can_route_selected_living_fight_into_travel(r):
    await facing_failure(r)
    for direction in ('turn_left','turn_left','turn_right','turn_right'):
        await r.choose(direction)
        await r.choose('motion_no_useful_effect')
    await r.choose('reject_selected_target')
    assert 'attack_mob_level_1' not in r.sage.calls[-1]['options']
    await r.choose('clear_failed')
    await r.choose('change_search_strategy')
    await r.choose('explore_visible')
    assert r.c.hunt.phase=='travel' and r.world.name=='Young Wolf'
    await r.choose(None)
    options=r.sage.calls[-1]['options']
    assert 'attack_mob_level_1' not in options and 'urgent_state' in options
    assert r.c.hunt.cast_obligation and not r.c.hunt.credited_kills
