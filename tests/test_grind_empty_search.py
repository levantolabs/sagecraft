"""Startup: empty selection retries must hand off to navigation."""
import asyncio
from copy import deepcopy

import pytest

from sage_wow.agent.grind_search import TARGET_RETRY_SECONDS
from test_grind_product_spec import Rig
from test_grind_committed_combat import add_current_observer


async def empty_start(r):
    await r.start();r.world.name=''
    for n in range(3):
        if n==2:
            r.c.hunt.active_seconds+=TARGET_RETRY_SECONDS+1
        await r.choose('target_enemy');await r.choose('no_selected_frame')
    return r.c.hunt


def test_empty_start_hands_off_without_resetting_debt_or_moving_until_sage_chooses(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            h=await empty_start(r)
            before=deepcopy(h.action_failures);keys=r.physical_keys()
            await r.choose('explore_visible')
            call=r.sage.calls[-1]
            assert 'search_here' not in call['options'] and 'target_enemy' not in call['options']
            assert 'Repeated Tab attempts' in call['instructions']
            assert 'An empty target HUD is normal during navigation' in call['prompt']
            assert 'Find the actual portrait' not in call['prompt']
            assert h.phase=='travel' and h.strategy_required['reason']=='empty_local_search'
            assert h.action_failures==before and h.search_revision==0 and r.physical_keys()==keys
            await r.choose('detour_backward')
            assert r.physical_keys()==keys+[r.controls['backward']['keycode']]
            assert h.pending['purpose']=='travel' and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_uncertain_destination_can_return_to_physical_recovery_without_replanning_forever(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            h=await empty_start(r)
            for _ in range(2):await r.choose(None)  # Protocol abstention; no uncertain action option.
            assert not h.planning_requested and h.recovery_requested
            await r.choose('turn_left')
            assert 'explore_visible' not in r.sage.calls[-1]['options']
            assert h.failures['target']==3 and h.search_revision==0
            await r.choose('motion_useful')
            assert h.search_revision==1 and h.strategy_required is None
            await r.choose('target_enemy')
        finally:await r.close()
    asyncio.run(run())


def test_a_fresh_living_target_interrupts_empty_search_planning_and_still_uses_cast_guard(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            h=await empty_start(r)
            await r.choose(None)
            r.c.config['committed_combat']=True;add_current_observer(r)
            r.world.name='Young Wolf'
            await r.choose('attack_mob_level_1')
            assert sum('Smite' in event[1] for event in r.casts())==1 and h.strategy_required is None
            assert not h.planning_requested and not h.credited_kills
            assert 'explore_visible' not in r.sage.calls[-1]['options']
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('unresolved',['pending','input_uncertain','threat','cast','selected','unknown_outcomes'])
def test_empty_search_cannot_override_an_unresolved_obligation(tmp_path,unresolved):
    async def run():
        r=Rig(tmp_path)
        try:
            h=await empty_start(r)
            if unresolved=='pending':h.pending={'family':'target','receipt':{}}
            elif unresolved=='input_uncertain':h.input_effect_unverified=True
            elif unresolved=='threat':h.active_threat={'source':'incoming_damage'}
            elif unresolved=='cast':h.cast_obligation='unknown prior cast'
            elif unresolved=='unknown_outcomes':h.confirmed_target_absences=0
            else:h.selected_presence=True
            assert h.plan_empty_search(r.world.capture(),{'name':''}) is None
            assert not h.planning_requested and h.strategy_required is None
        finally:await r.close()
    asyncio.run(run())
