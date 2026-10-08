"""An explicit user hunting-ground lock changes destinations, not input authority."""
import asyncio
from copy import deepcopy

import pytest
from test_grind_travel_acceptance import TravelRig
from sage_wow.agent.grind_only import settings
from sage_wow.agent.grind_progression import fixed_region_current
from sage_wow.agent.grind_search import measure


@pytest.mark.parametrize('inside,depleted',[(False,False),(True,False),(True,True),(False,True)])
def test_fixed_ground_returns_or_searches_without_area_tours(tmp_path,inside,depleted):
    async def run():
        r=TravelRig(tmp_path)
        try:
            await r.start()
            h=r.c.hunt
            area={'id':'troggs','label':'Trogg grounds','coordinate':[10.,10.],
                'coordinate_space':'client_displayed_zone_coordinates','zone_reference':r.zone,
                'arrival_radius':.6,'expected_level_range':[1,4],
                'source':{'kind':'offline_fixture','claim':'Synthetic hunting ground'}}
            h.catalog={'troggs':area,'boars':{**area,'id':'boars'}}
            h.learned={'other':{**area,'id':'other'}}
            r.c.config['fixed_hunting_area']=h.fixed_hunting_area='troggs'
            h.hunting_progression_by_player_level={1:{'primary_targets':['Rockjaw Trogg'],'area_ids':['troggs']}}
            h.phase='choose_area';h.planning_requested=True;h.compact_stage='planning'
            h.strategy_required={'reason':'empty_local_search','search_revision':h.search_revision}
            h.failures['target']=2
            if depleted:h.area_results['troggs']={'status':'depleted_or_unsupported_hypothesis'}
            r.position=[10.,10.] if inside else [15.,10.]
            before=deepcopy(h.failures)
            result=await r.choose('search_here' if inside else 'choose_area:troggs')
            options=r.sage.calls[-1]['options']
            assert not {'explore_visible','choose_area:boars','choose_area:other'} & options.keys()
            assert ('search_here' in options)==inside
            assert ('choose_area:troggs' in options)==(not inside)
            assert h.phase==('search' if inside else 'travel')
            assert h.failures==before and not r.physical_keys() and not result.receipt['possible_input']
            assert not h.credited_kills and not h.target_dead_observed
            assert 'do not tour other areas' in h.target_preference(1)
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['wrong_zone','missing_position','wrong_frame','old_measurement','unknown_precision'])
def test_local_membership_requires_current_coordinate_evidence(tmp_path,fault):
    async def run():
        r=TravelRig(tmp_path)
        try:
            await r.start()
            r.c.config['fixed_hunting_area']='troggs'
            r.c.hunt.catalog={'troggs':{'coordinate':[10.,10.],'zone_reference':r.zone,'arrival_radius':.6}}
            frame=r.world.capture();m=measure(r.c.profile,frame,r.world.ocr(frame.image_path),r.world.ocr)
            assert fixed_region_current(r.c,frame,m)
            if fault=='wrong_zone':m['zone_proposals']=['Elsewhere'];m['zone_normalized_keys']=['elsewhere']
            elif fault=='missing_position':m['position']=None
            elif fault=='wrong_frame':m['frame_id']='other'
            elif fault=='old_measurement':m['captured_at']='2020-01-01T00:00:00+00:00'
            else:m['position_status']='unknown'
            assert not fixed_region_current(r.c,frame,m)
        finally:await r.close()
    asyncio.run(run())


def test_unknown_fixed_area_rejected(tmp_path):
    async def run():
        r=TravelRig(tmp_path)
        try:
            r.c.profile.values['grind_only']['fixed_hunting_area']='not_in_catalog'
            with pytest.raises(ValueError,match='fixed_hunting_area'):settings(r.c.profile)
        finally:await r.close()
    asyncio.run(run())


def test_local_wait_preserves_history_without_input(tmp_path):
    async def run():
        r=TravelRig(tmp_path)
        try:
            await r.start()
            h=r.c.hunt
            r.c.config['fixed_hunting_area']=h.fixed_hunting_area='troggs'
            h.catalog={'troggs':{'id':'troggs','label':'Trogg grounds','coordinate':[10.,10.],
                'zone_reference':r.zone,'arrival_radius':.6,'source':{'kind':'offline_fixture','claim':'Test only'}}}
            h.phase='choose_area';h.planning_requested=True;h.compact_stage='planning'
            h.action_failures={'target:0:target_enemy':2}
            h.target_history={'old':{'unknown_cast_effect':True}}
            before=(deepcopy(h.action_failures),deepcopy(h.target_history),h.search_revision)
            result=await r.choose('wait_for_local_opportunity')
            assert not result.receipt['possible_input'] and not r.physical_keys()
            assert (h.action_failures,h.target_history,h.search_revision)==before
            assert h.phase=='choose_area' and h.planning_requested and not h.credited_kills
            import time
            assert 0<r.c.wait_until-time.time()<=5
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('drift',[None,'old_epoch','wrong_owner','new_selection','unverified_input','threat'])
def test_local_relocation_clears_unresolved_selection_before_searching(tmp_path,drift):
    async def run():
        from test_grind_committed_combat import add_current_observer
        from test_grind_cast_inspection_exit import unchanged_casts
        r=TravelRig(tmp_path)
        try:
            await r.start()
            h=r.c.hunt
            r.c.config['committed_combat']=True
            r.c.config['fixed_hunting_area']=h.fixed_hunting_area='troggs'
            h.catalog={'troggs':{'id':'troggs','label':'Local hunting grounds','coordinate':[10.,10.],
                'zone_reference':r.zone,'arrival_radius':.6,'source':{'kind':'offline_fixture'}}}
            r.world.name='Young Wolf';r.world.target_level=1
            original_proposal=r.c.target_proposal
            await unchanged_casts(r)
            cast_id=h.pending['receipt']['receipt_id']
            await r.choose('change_search_strategy')
            add_current_observer(r)
            assert h.strategy_required['selected_exit']['owner']==h.combat_history_key
            before=deepcopy(h.action_failures)
            keys=list(r.physical_keys())
            if drift:
                async def change_before_dispatch():
                    if drift=='old_epoch':h.strategy_required['selected_exit']['session_epoch']='expired'
                    elif drift=='wrong_owner':h.strategy_required['selected_exit']['owner']='another encounter'
                    elif drift=='new_selection':r.world.name='Different Wolf'
                    elif drift=='unverified_input':h.input_effect_unverified=True
                    elif drift=='threat':h.active_threat={'source':'new attacker'}
                r.sage.hook=change_before_dispatch
            clear=await r.choose('reject_selected_target')
            options=r.sage.calls[-1]['options']
            assert 'search_here' not in options and 'choose_area:troggs' not in options
            if drift:
                assert clear.status!='dispatched' and r.physical_keys()==keys
                assert not h.credited_kills
                return
            assert clear.receipt['possible_input']
            guards=[e['payload'] for e in r.store.recent(30) if e['event_type']=='dispatch_guard_checked']
            assert guards[-1]['approved']
            assert h.action_failures==before and not h.credited_kills
            assert any(x['receipt_id']==cast_id and x['outcome']=='unknown' for x in h.outcomes)
            assert h.pending['family']=='clear'
            r.world.name='';r.world.target_level=None
            r.c.target_proposal=original_proposal
            await r.choose('target_cleared')
            assert h.pending is None
            await r.choose('target_enemy')
            assert h.pending['family']=='target' and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())
