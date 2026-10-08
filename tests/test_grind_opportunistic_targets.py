"""Regional selected-target policy through fake controller/capture/input only."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import time

import pytest

from sage_wow.agent.grind_acquisition import learn_creature
from sage_wow.agent.grind_progression import selected_policy, primary, request
from sage_wow.agent.grind_search import measure
from test_grind_hunting_progression import ENTRY, configure
from test_grind_progression_scouting import PolicyRig


async def rig(tmp_path, own=3):
    r = PolicyRig(tmp_path)
    await r.start_policy(own, soft=True)
    configure(r)
    for entry in r.c.hunt.hunting_progression_by_player_level.values():
        entry['opportunistic_targets'] = ['Ragged Young Wolf', 'Ragged Timber Wolf']
    r.c.config['hunting_progression_by_player_level'] = deepcopy(r.c.hunt.hunting_progression_by_player_level)
    r.world.name = 'Ragged Young Wolf'; r.world.target_level = 1
    r.zone = 'Coldridge Valley'; r.position = [29.8, 75.1]
    return r


async def snapshot(r):
    frame = r.world.capture(); rows = await r.c.rows(frame)
    return frame, await r.c.target_proposal(frame), measure(r.c.profile, frame, rows, r.c.ocr)


def execute(fn):
    def run(tmp_path):
        asyncio.run(fn(tmp_path))
    return run


@pytest.mark.parametrize('extra', [None, 'Wolf', ['Small Crag Boar'], ['Wolf','wolf'], [''], ['Wolf']*17])
def test_opportunistic_names_validate(tmp_path, extra):
    from sage_wow.agent.grind_only import settings
    from test_grind_only import profile
    p = profile(tmp_path)
    p.values['grind_only']['hunting_progression_by_player_level'] = {3: {**ENTRY, 'opportunistic_targets': extra}}
    with pytest.raises(ValueError):settings(p)


@pytest.mark.parametrize('own', [3,4])
@pytest.mark.parametrize('area_id', ENTRY['area_ids'])
def test_any_configured_region_permits_exact_level_one_wolf(tmp_path, own, area_id):
    async def run():
        r=await rig(tmp_path,own)
        try:
            h=r.c.hunt; area=h.catalog[area_id]; r.position=area['coordinate'][:]
            frame,target,m=await snapshot(r)
            p=selected_policy(r.c,target,frame,m)
            assert p['kind']=='opportunistic_here' and p['region_id']==area_id
            assert not primary(h,own,target['name'])
            h.choose(h.catalog['coldridge_west_boars'],frame,'elsewhere',own)
            await r.choose('attack_mob_level_1')
            assert r.casts() and h.plan['phase']=='search' and h.phase=='fight'
            assert h.latest_patch is None and not h.learned
            assert not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['outside','missing','conflicting_zone','conflicting_coordinates','wrong_space','stale_frame','stale_time','unsupported','other_level'])
def test_region_facts_distinguish_unknown_from_outside_and_unsupported(tmp_path,fault):
    async def run():
        r=await rig(tmp_path)
        try:
            frame,target,m=await snapshot(r)
            if fault=='outside':m['position']=[31.,73.3]
            if fault=='missing':m['position']=None;m['position_status']='unreadable'
            if fault=='conflicting_zone':m['zone_proposals']=['Coldridge Valley','Elsewhere']
            if fault=='conflicting_coordinates':m['position_status']='conflict'
            if fault=='wrong_space':m['coordinate_space']='world'
            if fault=='stale_frame':m['frame_id']='historical'
            if fault=='stale_time':m['captured_at']='historical'
            if fault=='unsupported':target['name']='Snow Leopard'
            if fault=='other_level':r.c.level.last_confirmed_level=5
            expected='outside_primary_region' if fault=='outside' else 'unsupported_species' if fault=='unsupported' else 'primary' if fault=='other_level' else 'ground_unknown'
            assert selected_policy(r.c,target,frame,m)['kind']==expected
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('planned', [False,True])
def test_selected_startup_and_ordinary_travel_fulfill_only_final_placement_then_local_hunt(tmp_path,planned):
    async def run():
        r=await rig(tmp_path)
        try:
            h=r.c.hunt;frame,_,_=await snapshot(r)
            if planned:
                h.choose(h.catalog['coldridge_west_boars'],frame,'older-boar-plan',3)
                request(h,3,frame);h.phase='travel';h.planning_requested=False
            else:
                h.plan=None;h.phase='choose_area';h.planning_requested=True;h.compact_stage='planning'
            h.progression_pending=True
            await r.choose('attack_mob_level_1')
            assert not h.progression_pending and h.strategy_required is None
            assert h.pending['family']=='combat' and h.phase=='fight'
            if planned:assert h.plan['phase']=='search' and h.plan['request_id']=='older-boar-plan'
            else:assert h.plan is None
            await r.choose('damaged_alive')
            await r.choose('attack_mob_level_1')
            r.life='dead';r.world.health='black'
            await r.choose('dead')
            r.world.name='';r.world.target_level=None
            await r.choose(None)
            assert h.phase=='search' and 'target_enemy' in r.sage.calls[-1]['options']
            assert not any(n.startswith('choose_area:') for n in r.sage.calls[-1]['options'])
            assert h.latest_patch is None and not h.learned
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('location', [None,[31.,73.3]])
def test_final_guard_departure_or_unknown_blocks_discretionary_cast_and_keeps_placement(tmp_path,location):
    async def run():
        r=await rig(tmp_path)
        try:
            h=r.c.hunt;h.progression_pending=True
            async def depart():r.position=location
            r.sage.hook=depart
            result=await r.choose('attack_mob_level_1')
            assert result.status=='dispatch_guard_rejected' and not r.casts() and h.progression_pending
            r.sage.hook=None
            await r.choose(None)
            assert not any(n.startswith('attack_mob_level_') for n in r.sage.calls[-1]['options'])
            if location is None:
                assert 'membership is unknown' in r.sage.calls[-1]['prompt']
                assert not h.strategy_required and not h.rejected_signatures
                await r.choose('reject_selected_target')
                assert not h.strategy_required and not h.rejected_signatures
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('location', [None,[31.,73.3]])
def test_current_threat_allows_cast_on_unknown_or_outside_without_fulfilling_placement(tmp_path,location):
    from sage_wow.agent.grind_encounter_recovery import capture_scope
    async def run():
        r=await rig(tmp_path)
        try:
            h=r.c.hunt;h.progression_pending=True
            frame,_,_=await snapshot(r)
            h.active_threat={'kind':'current_incoming_attack','scope':capture_scope(r.c,frame),'occurred_at':time.time()}
            r.position=location
            await r.choose('attack_mob_level_1')
            assert r.casts() and h.progression_pending
            assert h.latest_patch is None
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation',['policy','configured_policy','level','band','configured_band','plan','encounter','owner','generation','epoch','revision','target','hash'])
def test_guard_rechecks_authority_after_fresh_measurement(tmp_path,mutation):
    async def run():
        r=await rig(tmp_path)
        try:
            frame,target,m=await snapshot(r);h=r.c.hunt;proof={}
            guard=await r.c.guard(frame,target,exact_level=1,strategy_state=proof)
            original=r.c.target_proposal
            async def mutated(fresh):
                actual=await original(fresh)
                if mutation=='policy':h.hunting_progression_by_player_level[3]['opportunistic_targets']=[]
                if mutation=='configured_policy':r.c.config['hunting_progression_by_player_level'][3]['opportunistic_targets']=[]
                if mutation=='level':r.c.level.last_confirmed_level=4
                if mutation=='band':h.target_bands_by_player_level[3]=[2,4]
                if mutation=='configured_band':r.c.config['target_bands_by_player_level'][3]=[2,4]
                if mutation=='plan':h.plan={'request_id':'later-choice'}
                if mutation=='encounter':h.encounter+=1
                if mutation=='owner':h.combat_history_key='other'
                if mutation=='generation':r.c.cycle._input_generation+=1
                if mutation=='epoch':r.c.cycle.session_epoch='other'
                if mutation=='revision':r.c.revision+=1
                if mutation=='target':target['name']='Ragged Timber Wolf'
                if mutation=='hash':
                    from pathlib import Path
                    Path(frame.image_path).write_bytes(Path(frame.image_path).read_bytes()+b'changed')
                return actual
            r.c.target_proposal=mutated
            checked=await guard(frame)
            assert not checked.approved and not proof and not r.casts()
        finally:await r.close()
    asyncio.run(run())


@execute
async def test_primary_learned_patch_survives_wolf_and_wolf_only_patch_cannot_grant_region(tmp_path):
    r=await rig(tmp_path)
    try:
        h=r.c.hunt;r.position=[28.,74.]
        frame,_,m=await snapshot(r)
        h.encounter_seen(1,frame,m,'Ragged Young Wolf')
        assert not h.learned and h.latest_patch is None
        h.encounter_ended=True
        h.encounter_seen(2,frame,m,'Rockjaw Trogg')
        original=deepcopy(h.latest_patch);learned=deepcopy(h.learned)
        assert selected_policy(r.c,{'name':'Ragged Young Wolf'},frame,m)['kind']=='opportunistic_here'
        h.encounter_ended=True;h.encounter_seen(1,replace(frame,frame_id='wolf-frame'),m,'Ragged Young Wolf')
        assert h.latest_patch==original and h.learned==learned
    finally:await r.close()


@pytest.mark.parametrize('known',[False,True])
def test_automatic_opener_uses_same_region_guard_and_preserves_type_fallback(tmp_path,known):
    async def run():
        r=await rig(tmp_path)
        try:
            r.c.config.update(target_opening_cast=True,target_name_box=[205,95,360,120])
            r.c.opening_creatures={}
            if known:
                learn_creature(r.c,{'own_source':True,'event':'PARTY_KILL','dest_guid':'Creature-offline',
                    'dest_name':'Ragged Young Wolf','source_name':'Test Player','source_guid':'Player-offline'})
            h=r.c.hunt;r.world.name='';r.world.target_level=None
            await r.choose('target_enemy')
            h.progression_pending=True
            r.world.name='Ragged Young Wolf';r.world.target_level=1
            before=len(r.sage.calls);r.c.wait_until=0
            if not known:r.sage.answers.append('attack_mob_level_1')
            result=await r.c.process(r.world.capture())
            assert result.status=='dispatched' and r.casts() and not h.progression_pending
            assert len(r.sage.calls)==before+(not known)
            assert h.latest_patch is None and not h.learned
        finally:await r.close()
    asyncio.run(run())


@execute
async def test_exact_level_five_and_wrong_type_or_life_keep_independent_attack_vetoes(tmp_path):
    r=await rig(tmp_path)
    try:
        for mob,kind,life in [(5,'creature','alive'),(1,'player','alive'),(1,'creature','dead')]:
            r.world.target_level=mob;r.kind=kind;r.life=life
            await r.choose(None)
            assert not any(n.startswith('attack_mob_level_') for n in r.sage.calls[-1]['options'])
            assert not r.casts()
    finally:await r.close()


@execute
async def test_depleted_primary_patch_is_not_reopened_by_wolf_threat_encounter(tmp_path):
    r=await rig(tmp_path)
    try:
        h=r.c.hunt;r.position=[28.,74.]
        frame,target,m=await snapshot(r)
        h.encounter_seen(2,frame,m,'Rockjaw Trogg')
        patch=deepcopy(h.latest_patch)
        h.area_results[patch['id']]['status']='depleted_or_unsupported_hypothesis'
        assert selected_policy(r.c,target,frame,m)['kind']=='outside_primary_region'
        h.encounter_ended=True;h.encounter_seen(1,frame,m,'Ragged Young Wolf')
        assert h.area_results[patch['id']]['status']=='depleted_or_unsupported_hypothesis'
        assert h.latest_patch==patch
        assert selected_policy(r.c,target,frame,m)['kind']=='outside_primary_region'
        r.position=h.catalog['coldridge_southeast_troggs']['coordinate'][:]
        h.area_results['coldridge_southeast_troggs']={'status':'depleted_or_unsupported_hypothesis'}
        frame,target,m=await snapshot(r)
        assert selected_policy(r.c,target,frame,m)['kind']=='outside_primary_region'
    finally:await r.close()


@execute
async def test_explicit_exit_retains_authority_after_threat_only_wolf_cast(tmp_path):
    from sage_wow.agent.grind_encounter_recovery import capture_scope
    r=await rig(tmp_path)
    try:
        h=r.c.hunt;r.position=[31.,73.3];frame,_,_=await snapshot(r)
        request(h,3,frame)
        h.active_threat={'kind':'current_incoming_attack','scope':capture_scope(r.c,frame),'occurred_at':time.time()}
        await r.choose('attack_mob_level_1')
        await r.choose('damaged_alive')
        h.active_threat=None
        h.request_recovery('offline_unresolved_encounter',r.c.last_signature)
        await r.choose('change_search_strategy')
        assert h.strategy_required['selected_exit']
        count=len(r.casts())
        await r.choose('choose_area:coldridge_west_boars')
        assert h.phase=='travel' and h.plan['phase']=='travel'
        await r.choose(None)
        assert not any(n.startswith('attack_mob_level_') for n in r.sage.calls[-1]['options'])
        assert len(r.casts())==count and h.phase=='travel'
    finally:await r.close()


@execute
async def test_accepted_wolf_optional_loot_retirement_returns_to_existing_local_plan(tmp_path):
    from test_grind_loot_budget import enable, entry, tick
    r=await rig(tmp_path)
    try:
        h=r.c.hunt;frame,_,_=await snapshot(r)
        h.choose(h.catalog['coldridge_west_boars'],frame,'older-plan',3)
        r.controls['interact_target']={'keycode':3,'verified_from':'offline fake'}
        enable(r)
        await r.choose('attack_mob_level_1')
        r.life='dead';r.world.health='black'
        await r.choose('dead')
        assert h.plan['phase']=='search' and h.loot_request
        r.world.name='';r.world.target_level=None
        await r.choose(None)
        assert r.c.loot.pending
        entry(r)['spent']=30.
        await tick(r)
        assert not r.c.loot.pending and r.c.loot.data['outcome']=='skipped_unverified'
        await r.choose(None)
        assert h.phase=='search' and h.plan['phase']=='search'
        assert 'target_enemy' in r.sage.calls[-1]['options']
        assert not h.learned and not h.credited_kills
    finally:await r.close()


@pytest.mark.parametrize('location',[None,[31.,73.3]])
def test_opener_final_baseline_unknown_or_outside_with_threat_does_not_reuse_inside_guard_proof(tmp_path,monkeypatch,location):
    from sage_wow.agent.grind_encounter_recovery import capture_scope
    import sage_wow.agent.grind_search as search
    async def run():
        r=await rig(tmp_path)
        try:
            r.c.config.update(target_opening_cast=True,target_name_box=[205,95,360,120])
            r.c.opening_creatures={}
            learn_creature(r.c,{'own_source':True,'event':'PARTY_KILL','dest_guid':'Creature-offline',
                'dest_name':'Ragged Young Wolf','source_name':'Test Player'})
            h=r.c.hunt;r.world.name='';r.world.target_level=None
            await r.choose('target_enemy')
            h.progression_pending=True;r.world.name='Ragged Young Wolf';r.world.target_level=1
            frame,_,_=await snapshot(r)
            h.active_threat={'kind':'current_incoming_attack','scope':capture_scope(r.c,frame),'occurred_at':time.time()}
            original=search.measure;reads={}
            def intermittent(profile,fresh,rows,ocr):
                result=original(profile,fresh,rows,ocr)
                reads[fresh.frame_id]=reads.get(fresh.frame_id,0)+1
                if reads[fresh.frame_id]>1:
                    result['position']=location
                    result['position_status']='readable_proposal' if location else 'unreadable'
                return result
            monkeypatch.setattr(search,'measure',intermittent)
            monkeypatch.setattr('sage_wow.agent.grind_only.measure',intermittent)
            r.c.wait_until=0
            result=await r.c.process(r.world.capture())
            assert result.status=='dispatched' and r.casts() and h.progression_pending
            assert not h.latest_patch
        finally:await r.close()
    asyncio.run(run())


@execute
async def test_fresh_guard_image_mutation_is_rejected_before_region_proof(tmp_path):
    from pathlib import Path
    r=await rig(tmp_path)
    try:
        frame,target,_=await snapshot(r);proof={};original=r.c.target_proposal
        guard=await r.c.guard(frame,target,exact_level=1,strategy_state=proof)
        async def corrupt(fresh):
            actual=await original(fresh)
            Path(fresh.image_path).write_bytes(Path(fresh.image_path).read_bytes()+b'changed')
            return actual
        r.c.target_proposal=corrupt
        checked=await guard(frame)
        assert not checked.approved and not proof
    finally:await r.close()


@pytest.mark.parametrize('fresh_level',[None,2])
def test_guard_preserves_proved_offer_numeral_through_ocr_dropout_and_rejects_conflicting_number(tmp_path,fresh_level):
    async def run():
        r=await rig(tmp_path)
        try:
            frame,target,_=await snapshot(r);proof={};original=r.c.target_proposal
            guard=await r.c.guard(frame,target,exact_level=1,strategy_state=proof)
            async def numeric_read(fresh):
                actual=await original(fresh)
                # Exact unchanged image and target badge; only numeric OCR availability changes.
                actual['levels']=[] if fresh_level is None else [fresh_level]
                return actual
            r.c.target_proposal=numeric_read
            checked=await guard(frame)
            assert checked.approved==(fresh_level is None)
            if fresh_level is None:assert checked.evidence['predicates']['target_badge'] and proof['result']['kind']=='opportunistic_here'
            else:assert 'no_level_conflict' in checked.evidence['failed_predicates'] and not proof
            assert not r.casts()
        finally:await r.close()
    asyncio.run(run())
