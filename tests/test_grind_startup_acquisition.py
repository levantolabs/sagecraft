"""First-session targeting and already-in-region entry, with explicit fakes."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from sage_wow.agent.grind_hunt_entry import idle_acquisition, route_arrival
from sage_wow.agent.grind_search import HuntState
from test_grind_hunting_progression import configure
from test_grind_progression_scouting import PolicyRig
from test_grind_travel_recovery import events


async def fresh_start(r):
    await r.start_policy(soft=True); configure(r)
    r.world.name=''; r.world.target_level=None
    r.zone='Coldridge Valley'; r.position=[30.,75.1]
    original=r.c.target_proposal
    async def unassessed(frame):
        target=await original(frame)
        # Runtime startup has no attributed selected-target assessment yet.
        for key in ('visual_observation','visual_provenance','eligibility'):
            target.pop(key,None)
        return target
    r.c.target_proposal=unassessed
    h=r.c.hunt
    assert h.selected_presence is None and not h.recent_moves
    h.last_level=None; h.level_changed(3)
    return h


@pytest.mark.parametrize('stage',['acquire','recovery'])
def test_unassessed_startup_offers_concrete_acquisition_without_inventing_absence(tmp_path,stage):
    async def run():
        r=PolicyRig(tmp_path)
        try:
            h=await fresh_start(r)
            h.progression_pending=False; h.phase='search'; h.compact_stage=stage
            await r.choose('target_enemy')
            assert set(r.sage.calls[-1]['options'])=={'target_enemy','turn_left','turn_right','forward'}
            assert h.selected_presence is None and h.confirmed_target_absences==0
            assert h.pending['family']=='target' and not r.casts() and not h.credited_kills
            assert 'No target is currently selected' not in r.sage.calls[-1]['prompt']
            assert events(r,'grind_compact_question')[-1]['concrete_action_menu']
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('exhausted',[False,True])
def test_fresh_run_already_in_region_enters_search_with_original_debt(tmp_path,exhausted):
    async def run():
        r=PolicyRig(tmp_path)
        try:
            h=await fresh_start(r)
            await r.choose('choose_area:coldridge_southeast_troggs')
            strategy=deepcopy(h.strategy_required)
            h.action_failures['retained_combat_method']=2
            if exhausted:
                for action in ('turn_left','turn_right','forward'):
                    h.action_failures[h.motion_key(action,'search')]=2
                h.failures['target']=2; h.last_completed_target_active_at=h.active_seconds
            keys=list(r.physical_keys())
            if exhausted:
                result=await r.c.process(r.world.capture())
                assert result.status=='grind_reobserve'
                assert h.phase=='choose_area' and h.planning_requested
                assert h.failures['target']==2 and r.physical_keys()==keys
            else:
                await r.choose('target_enemy')
                assert h.phase=='search' and h.pending['family']=='target'
                assert set(r.sage.calls[-1]['options'])=={'target_enemy','turn_left','turn_right','forward'}
                assert r.physical_keys()==keys+[r.controls['target_enemy']['keycode']]
            assert h.strategy_required==strategy and h.search_revision==0 and h.sector==0
            assert h.action_failures['retained_combat_method']==2
            assert not events(r,'grind_search_sector_changed') and not h.recent_moves
            assert not h.progress_facts and not h.credited_kills and not r.casts()
            arrival=events(r,'grind_hunting_arrival')[-1]
            assert arrival['outcome']=='observed_current_hunting_location'
            assert not arrival['displacement_observed'] and not arrival['progress_credited']
            assert not arrival['input_authorized']
            assert h.selected_presence is None and not h.confirmed_target_absences
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('cue',['name','levels','visual_present','visual_name','visual_level','bar'])
def test_unknown_selection_menu_preserves_positive_cues(cue):
    h=HuntState({},'offline')
    c=SimpleNamespace(hunt=h,heal_pending=None,config={'critical_health_threshold':.3})
    target={'hud':{'frame_id':'current','player_health':1.,'health_confidence':1.}}
    if cue=='name':target['name']='Trogg'
    elif cue=='levels':target['levels']=[2]
    elif cue=='visual_present':target['visual_observation']={'selected_hud':'present'}
    elif cue=='visual_name':target['visual_observation']={'name':'Trogg'}
    elif cue=='visual_level':target['visual_observation']={'level':2}
    elif cue=='bar':target['hud'].update(target_health=0.,target_health_confidence=1.)
    assert not idle_acquisition(c,target,None,SimpleNamespace(frame_id='current'))


@pytest.mark.parametrize('bad',['outside','missing','ambiguous_zone','wrong_zone','stale_measurement','pending','scope','focus'])
def test_location_only_entry_requires_current_matching_geometry_and_scope(bad):
    h=HuntState({},'offline'); h.phase='travel'
    h.plan={'area_id':'troggs','request_id':'dest','area':{'coordinate':[30.1,75.6],
        'zone_reference':'Coldridge Valley','arrival_radius':.6}}
    frame=SimpleNamespace(frame_id='current',captured_at='now')
    measurement={'frame_id':'current','captured_at':'now','position':[30.,75.1],
        'position_status':'readable_proposal','zone_proposals':['Coldridge Valley']}
    c=SimpleNamespace(hunt=h,heal_pending=None,current=lambda:bad!='focus',fresh=lambda f:True,
        cycle=SimpleNamespace(session_epoch='epoch',_scope_error=lambda f:'bad' if bad=='scope' else None),
        event=lambda *args:None)
    if bad=='outside':measurement['position']=[28.,75.]
    elif bad=='missing':measurement['position']=None
    elif bad=='ambiguous_zone':measurement['zone_proposals'].append('Other Zone')
    elif bad=='wrong_zone':measurement['zone_proposals']=['Other Zone']
    elif bad=='stale_measurement':measurement['frame_id']='old'
    elif bad=='pending':h.pending={'family':'target'}
    assert not route_arrival(c,frame,{},measurement)
    assert h.phase=='travel' and not h.hunt_arrival and not h.search_changes
