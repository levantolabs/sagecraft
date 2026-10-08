"""Live-run arrival -> selection regressions. Explicit fake world/input only."""
import asyncio
from types import SimpleNamespace

import pytest

from sage_wow.agent.grind_hunt_entry import idle_acquisition, route_arrival, refresh_arrival
from sage_wow.agent.grind_search import HuntState
from test_grind_hunting_progression import configure
from test_grind_progression_scouting import PolicyRig
from test_grind_travel_recovery import events


async def approaching(r):
    await r.start_policy(soft=True); configure(r)
    r.world.name=''; r.world.target_level=None
    r.zone='Coldridge Valley'; r.position=[29.,75.6]
    h=r.c.hunt; h.last_level=None; h.level_changed(3)
    await r.choose('choose_area:coldridge_southeast_troggs')
    await r.choose('probe_forward')
    r.position=[30.1,75.4]
    return h


def test_arrival_targets_in_same_cycle_without_arrival_vote_or_replanning(tmp_path):
    async def run():
        r=PolicyRig(tmp_path)
        try:
            h=await approaching(r)
            h.action_failures['old_combat_failure']=2
            h.unclear=5; h.compact_stage='recovery'
            before=len(r.sage.calls)
            await r.choose('target_enemy')
            assert len(r.sage.calls)==before+1
            assert set(r.sage.calls[-1]['options'])=={'target_enemy','turn_left','turn_right','forward'}
            assert h.phase=='search' and h.search_revision==1 and not h.strategy_required
            assert h.pending['family']=='target' and h.hunt_arrival
            assert h.action_failures['old_combat_failure']==2 and not h.credited_kills
            assert len(events(r,'grind_hunting_arrival'))==1
            assert not events(r,'grind_recovery_planning_requested')
            assert 'CURRENT world view' in r.sage.calls[-1]['prompt']
        finally:await r.close()
    asyncio.run(run())


def test_arrival_null_does_not_send_input_or_replan_an_unsearched_patch(tmp_path):
    async def run():
        r=PolicyRig(tmp_path)
        try:
            h=await approaching(r);keys=list(r.physical_keys())
            for _ in range(5):await r.choose(None)
            assert r.physical_keys()==keys and not r.casts()
            assert h.phase=='search' and h.search_revision==1
            assert h.plan['area_id']=='coldridge_southeast_troggs'
            assert 'target_enemy' in r.sage.calls[-1]['options']
            assert 'cannot_assess' not in r.sage.calls[-1]['options']
            assert len(events(r,'grind_hunting_arrival'))==1
            assert not events(r,'grind_recovery_planning_requested')
            assert not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interruption',['stop','focus','generation'])
def test_arrived_action_still_requires_current_input_authority(tmp_path,interruption):
    async def run():
        r=PolicyRig(tmp_path)
        try:
            await approaching(r);keys=list(r.physical_keys())
            async def interrupt():
                if interruption=='stop':r.c.stop('offline stop')
                elif interruption=='focus':r.c.pause_focus()
                else:r.c.cycle._input_generation+=1
            r.sage.hook=interrupt
            result=await r.choose('target_enemy')
            assert result.status!='dispatched' and r.physical_keys()==keys
            assert not r.casts() and not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_recovery_acquisition_has_current_image_and_no_unsupported_reinspection(tmp_path):
    async def run():
        r=PolicyRig(tmp_path)
        try:
            await r.start_policy(soft=True);configure(r)
            r.world.name='';r.world.target_level=None
            h=r.c.hunt;h.phase='search';h.selected_presence=False
            h.compact_stage='recovery';h.planning_requested=False
            h.recent_combat={'handoff_requested':True,'source_image':'unused historical combat'}
            await r.choose('target_enemy')
            assert set(r.sage.calls[-1]['options'])=={'target_enemy','turn_left','turn_right','forward'}
            assert 'Recovery reason:' not in r.sage.calls[-1]['prompt']
            assert r.provider_views[-1]['size']==(500,300)
            assert h.recent_combat['source_image']=='unused historical combat'
        finally:await r.close()
    asyncio.run(run())


def test_exhausted_action_menu_routes_to_destinations_without_a_block_or_input(tmp_path):
    async def run():
        r=PolicyRig(tmp_path)
        try:
            await r.start_policy(soft=True);configure(r)
            r.world.name='';r.world.target_level=None
            h=r.c.hunt;h.phase='search';h.selected_presence=False;h.compact_stage='acquire'
            for a in ('turn_left','turn_right','forward'):
                h.action_failures[h.motion_key(a,'search')]=2
            h.failures['target']=2;h.last_completed_target_active_at=h.active_seconds
            before=len(r.sage.calls);keys=list(r.physical_keys())
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_reobserve' and len(r.sage.calls)==before
            assert h.phase=='choose_area' and h.planning_requested and not h.blocked
            assert r.physical_keys()==keys and h.failures['target']==2
            assert h.strategy_required['reason']=='exhausted_local_actions'
            assert not h.strategy_required['action_authority']
            assert h.search_revision==0
            await r.choose('choose_area:coldridge_southeast_troggs')
            assert h.phase=='travel' and r.physical_keys()==keys and not h.credited_kills
            assert h.strategy_required and h.failures['target']==2 and h.search_revision==0
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interrupt',['resources','ui'])
def test_local_translation_invalidates_arrival_before_interrupt_can_replace_pending(tmp_path,monkeypatch,interrupt):
    async def run():
        from sage_wow.agent.cycle import CycleResult
        r=PolicyRig(tmp_path)
        try:
            h=await approaching(r)
            await r.choose('forward')
            assert h.hunt_arrival and h.pending['purpose']=='search'
            r.position=None;keys=list(r.physical_keys());r.c.wait_until=0
            if interrupt=='resources':
                r.c.config['encounter_resources']=True
                async def resource(*args):
                    assert h.hunt_arrival is None
                    h.archive_pending('offline resource interruption')
                    return CycleResult('offline_resource_interrupt')
                monkeypatch.setattr('sage_wow.agent.grind_resources.process_resources',resource)
            else:
                r.c.require_world=True;r.sage.answers.append(None)
            await r.c.process(r.world.capture())
            assert h.hunt_arrival is None and r.physical_keys()==keys
            assert events(r,'grind_hunt_arrival_invalidated')[-1]['reason']=='unobserved_local_translation'
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('presence',[None,False,True])
@pytest.mark.parametrize('obligation',['pending','cast','loot','threat','unknown_input','encounter','blocked','heal','mana','selected','disengage','critical_health','unknown_health','stale_health'])
def test_action_menu_does_not_erase_unfinished_work(obligation,presence):
    h=HuntState({},'offline');h.selected_presence=presence
    c=SimpleNamespace(hunt=h,heal_pending=None,config={'critical_health_threshold':.3})
    target={'name':None,'visual_observation':{'selected_hud':'absent'},
        'hud':{'frame_id':'current','player_health':1.,'health_confidence':1.}};pending=None
    if obligation=='pending':pending={'family':'combat'}
    elif obligation=='cast':h.cast_obligation='unfinished'
    elif obligation=='loot':h.loot_request={'unfinished':True}
    elif obligation=='threat':h.active_threat={'fresh':True}
    elif obligation=='unknown_input':h.input_effect_unverified=True
    elif obligation=='encounter':h.encounter_ended=False
    elif obligation=='blocked':h.blocked={'reason':'offline'}
    elif obligation=='heal':c.heal_pending={'unfinished':True}
    elif obligation=='mana':h.no_mana=True
    elif obligation=='selected':target.update(name='Burly Rockjaw Trogg',visual_observation={'selected_hud':'present'})
    elif obligation=='disengage':h.disengagement={'active':True}
    elif obligation=='critical_health':target['hud']['player_health']=.2
    elif obligation=='unknown_health':target['hud']['player_health']=None
    else:target['hud']['frame_id']='old'
    assert not idle_acquisition(c,target,pending,SimpleNamespace(frame_id='current'))


@pytest.mark.parametrize('failure',['pending','cast','loot','threat','input','encounter','blocked','heal','mana','disengage','stale','scope','not_arrived','unbound_measurement'])
def test_arrival_cannot_bypass_scope_or_obligations(failure):
    h=HuntState({},'offline');h.phase='travel'
    h.plan={'area_id':'troggs','request_id':'destination'}
    c=SimpleNamespace(hunt=h,heal_pending=None,current=lambda:True,fresh=lambda f:True,
        cycle=SimpleNamespace(session_epoch='epoch',_scope_error=lambda f:None),event=lambda *a:None)
    h.current_geometry=lambda m:{'available':True,'inside_radius':failure!='not_arrived'}
    h.travel_search_evidence=lambda *a:None if failure=='unbound_measurement' else {'zone':['coldridge valley']}
    if failure in {'pending','blocked'}:setattr(h,failure,{'unfinished':True})
    elif failure=='cast':h.cast_obligation='unfinished'
    elif failure=='loot':h.loot_request={'unfinished':True}
    elif failure=='threat':h.active_threat={'fresh':True}
    elif failure=='input':h.input_effect_unverified=True
    elif failure=='encounter':h.encounter_ended=False
    elif failure=='heal':c.heal_pending={'unfinished':True}
    elif failure=='mana':h.no_mana=True
    elif failure=='disengage':h.disengagement={'active':True}
    elif failure=='stale':c.fresh=lambda f:False
    elif failure=='scope':c.cycle._scope_error=lambda f:'invalid'
    assert not route_arrival(c,SimpleNamespace(frame_id='current',captured_at='offline'),{},{});assert h.phase=='travel'


@pytest.mark.parametrize('change',['missing_ocr','ambiguous_ocr','departure','zone','destination','epoch','unknown_input','local_translation','local_turn'])
def test_arrival_context_lifetime_is_not_action_authority(change):
    h=HuntState({},'offline');h.plan={'request_id':'dest','area':{'coordinate':[30.,75.],
        'zone_reference':'Coldridge Valley','arrival_radius':.6}}
    h.hunt_arrival={'session_epoch':'epoch','destination_request_id':'dest','zone':['coldridge valley'],
        'last_movement_receipt':None,'input_authorized':False}
    c=SimpleNamespace(hunt=h,cycle=SimpleNamespace(session_epoch='epoch'),event=lambda *args:None)
    measurement={'position':None,'position_status':'missing','zone_proposals':[]}
    if change=='departure':measurement.update(position=[29.,74.],position_status='readable_proposal',zone_proposals=['Coldridge Valley'])
    elif change=='ambiguous_ocr':measurement['zone_proposals']=['Coldridge Valley','I Coldridge Valley']
    elif change=='zone':measurement['zone_proposals']=['Different Zone']
    elif change=='destination':h.plan['request_id']='other'
    elif change=='epoch':c.cycle.session_epoch='other'
    elif change=='unknown_input':h.input_effect_unverified=True
    elif change in {'local_translation','local_turn'}:h.pending={'family':'motion',
        'action':'forward' if change=='local_translation' else 'turn_left','receipt':{'possible_input':True}}
    refresh_arrival(c,measurement)
    assert bool(h.hunt_arrival)==(change in {'missing_ocr','ambiguous_ocr','local_turn'})
