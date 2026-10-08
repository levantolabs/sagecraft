"""Completed cast uncertainty cannot monopolize later nonoffensive hunting."""
import asyncio
from copy import deepcopy

import pytest

from sage_wow.agent.grind_hunt_entry import idle_acquisition, route_arrival
from test_grind_loot_integration import LootRig


async def retained(r, producer='archive'):
    await r.start();r.c.config['committed_combat']=True
    await r.choose('attack_mob_level_1')
    h=r.c.hunt
    if producer=='archive':h.archive_pending('offline selection disappearance')
    else:h.resolve('unknown',r.world.capture(),{},pending=h.pending,linked=True)
    h.encounter_ended=True;h.selected_presence=False;h.phase='search';h.compact_stage='recovery'
    h.remember_approach()
    h.recent_combat['handoff_requested']=True
    r.world.name=''
    return h


@pytest.mark.parametrize('producer',['archive','resolve'])
def test_completed_unknown_cast_reaches_concrete_tab_without_erasing_history(tmp_path,producer):
    async def run():
        r=LootRig(tmp_path)
        try:
            h=await retained(r,producer)
            assert h.cast_obligation and not h.cast_blocks_acquisition()
            debt=deepcopy((h.cast_obligation,h.target_history,h.action_failures,h.progress_facts))
            casts=list(r.casts())
            h.unassessed=[]  # Bounded history eviction is not renewed combat authority or a new block.
            await r.choose('target_enemy')
            assert set(r.sage.calls[-1]['options'])=={'target_enemy','turn_left','turn_right','forward'}
            assert 'Recovery reason:' not in r.sage.calls[-1]['prompt']
            assert (h.cast_obligation,h.target_history,h.action_failures,h.progress_facts)==debt
            assert h.pending['family']=='target' and r.casts()==casts and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('ineffective',[False,True])
@pytest.mark.parametrize('bad',['partial','dispatch_unknown','error','wrong_owner','wrong_encounter',
    'wrong_target','active_error','ongoing_encounter','input_unverified','unsupported_obligation','missing_receipt','missing_owner'])
def test_only_attributed_completed_outcome_uncertainty_is_nonblocking(tmp_path,bad,ineffective):
    async def run():
        r=LootRig(tmp_path)
        try:
            h=await retained(r)
            if ineffective:
                from sage_wow.agent.grind_search import INEFFECTIVE_COMPLETED_CAST
                h.cast_obligation=INEFFECTIVE_COMPLETED_CAST
            if bad=='partial':h.recent_combat['receipt']['completed']=False
            elif bad=='dispatch_unknown':h.recent_combat['receipt']['dispatch_unknown']=True
            elif bad=='error':h.recent_combat['receipt']['error']='failed'
            elif bad=='wrong_owner':h.recent_combat['target_history_key']='other'
            elif bad=='wrong_encounter':h.recent_combat['encounter_id']+=1
            elif bad=='wrong_target':h.recent_combat['target']['name']='Different creature'
            elif bad=='active_error':h.cast_error={'status':'active','kind':'range'}
            elif bad=='ongoing_encounter':h.encounter_ended=False
            elif bad=='input_unverified':h.input_effect_unverified=True
            elif bad=='unsupported_obligation':h.cast_obligation='Unreconciled command state'
            elif bad=='missing_owner':h.recent_combat['target_history_key']=h.combat_history_key=None
            else:h.recent_combat={}
            assert h.cast_blocks_acquisition()
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            target['hud']={'frame_id':frame.frame_id,'player_health':1.,'health_confidence':1.}
            assert not idle_acquisition(r.c,target,None,frame)
        finally:await r.close()
    asyncio.run(run())


def test_arrival_and_both_planning_handoffs_share_debt_semantics(tmp_path):
    async def run():
        r=LootRig(tmp_path)
        try:
            h=await retained(r);frame=r.world.capture();debt=h.cast_obligation
            h.phase='travel';h.plan={'area_id':'patch','request_id':'dest','area':{
                'coordinate':[30.,75.],'zone_reference':'Coldridge Valley','arrival_radius':.6}}
            measurement={'frame_id':frame.frame_id,'captured_at':frame.captured_at,
                'position':[30.,75.],'position_status':'readable_proposal','zone_proposals':['Coldridge Valley']}
            assert route_arrival(r.c,frame,{},measurement)
            assert h.phase=='search' and h.cast_obligation==debt and not h.progress_facts
            h.hunt_arrival=None;h.confirmed_target_absences=3
            assert h.plan_empty_search(frame,{'name':None})
            assert h.cast_obligation==debt and h.search_revision==0
            h.strategy_required=None;h.planning_requested=False;h.phase='travel';h.compact_stage='recovery';h.unclear=3
            assert h.plan_unresolved_recovery(frame,{'name':None})
            assert h.cast_obligation==debt and h.search_revision==0 and not h.progress_facts
        finally:await r.close()
    asyncio.run(run())
