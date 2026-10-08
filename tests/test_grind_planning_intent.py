"""Live-run planning exit with a still-selected living target; offline only."""
import asyncio
from copy import deepcopy

import pytest

from test_grind_product_spec import Rig
from test_grind_committed_combat import add_current_observer


async def start(r):
    await r.start()
    r.c.config['committed_combat']=True
    add_current_observer(r)


def test_failed_range_approach_can_plan_and_guardedly_travel_with_living_hud(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            await start(r)
            await r.choose('attack_mob_level_1')
            r.world.error='Out of range'
            await r.choose('position_error')
            for _ in range(2):
                await r.choose('forward')
                await r.choose('motion_no_useful_effect')
            # The target remains visibly selected after two completed clears.
            for _ in range(2):
                await r.choose('reject_selected_target')
                await r.choose('clear_failed')
            h=r.c.hunt;history=h.target_history[h.approach['history_key']]
            failures=deepcopy(h.action_failures);outcomes=deepcopy(h.outcomes)
            history_before=deepcopy(history);error=deepcopy(h.cast_error)
            lives=deepcopy(h.target_lives);casts=list(r.casts())
            assert h.motion_count('forward','cast_correction',history['key'])==2
            assert h.failures['clear']==2 and h.cast_obligation
            await r.choose('change_search_strategy')
            options=r.sage.calls[-1]['options']
            assert 'forward' not in options and 'reject_selected_target' not in options
            assert not any(name.startswith('attack_mob') for name in options)
            assert h.planning_requested and h.phase=='choose_area'
            assert h.strategy_required['search_revision']==h.search_revision
            assert h.context()['strategy_required']==h.strategy_required
            await r.choose('explore_visible')
            assert 'search_here' not in r.sage.calls[-1]['options']
            assert h.phase=='travel' and not h.planning_requested
            before=list(r.physical_keys())
            result=await r.choose('detour_backward')
            guards=[event['payload'] for event in r.store.recent(20)
                if event['event_type']=='dispatch_guard_checked']
            assert guards[-1]['chosen']=='detour_backward' and guards[-1]['approved']
            assert guards[-1]['source_frame_id']!=guards[-1]['dispatch_frame_id']
            assert 'begin_hunt' not in r.sage.calls[-1]['options']
            assert result.receipt['completed'] and result.receipt['possible_input']
            assert r.physical_keys()==before+[r.controls['backward']['keycode']]
            assert h.pending['purpose']=='travel' and h.pending['action']=='backward'
            assert h.action_failures==failures and h.outcomes[:len(outcomes)]==outcomes
            assert history==history_before and h.cast_error==error and h.cast_obligation
            assert h.target_lives==lives and r.casts()==casts
            assert not h.credited_kills and not h.target_dead_observed
        finally:
            await r.close()
    asyncio.run(run())


def test_normal_planning_and_travel_can_enter_unchanged_search(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            await start(r)
            h=r.c.hunt;h.phase='choose_area';h.planning_requested=True
            assert h.strategy_required is None
            await r.choose('search_here')
            assert h.phase=='search' and h.strategy_required is None
            h.phase='choose_area';h.planning_requested=True
            await r.choose('explore_visible')
            await r.choose('begin_hunt')
            assert h.phase=='search' and h.search_revision==0
            assert not r.physical_keys() and not r.casts()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('action,feedback',[
    ('attack_mob_level_1','casting'),
    ('reject_selected_target','clear_failed'),
    ('target_enemy',None),
    ('forward','motion_no_useful_effect'),
])
def test_pending_input_feedback_precedes_requested_planning(tmp_path,action,feedback):
    async def run():
        r=Rig(tmp_path)
        try:
            await start(r)
            if action in {'target_enemy','forward'}:
                # Acquire first; the next captured view then has a fresh living HUD.
                r.world.name='';r.c.hunt.compact_stage='acquire'
                original=r.c.target_proposal
                async def absent_or_present(frame):
                    target=await original(frame)
                    return target if target.get('name') else {**target,'visual_observation':None}
                r.c.target_proposal=absent_or_present
            await r.choose(action)
            r.world.name='Young Wolf'
            h=r.c.hunt;pending=h.pending
            h.phase='choose_area';h.planning_requested=True
            await r.choose(feedback)
            options=r.sage.calls[-1]['options']
            assert 'explore_visible' not in options and 'search_here' not in options
            if action=='target_enemy':
                assert any(name.startswith('attack_mob') for name in options)
                assert h.pending is pending
            elif action=='attack_mob_level_1':
                assert 'casting' in options and h.pending is pending
            elif action=='reject_selected_target':
                assert 'target_cleared' in options and h.pending is None
            else:
                assert 'motion_useful' in options and h.pending is None
            assert not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())
