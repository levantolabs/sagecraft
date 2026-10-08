"""User-authorized target→one cast, using fake input/capture and no network."""
import asyncio
from copy import deepcopy

import pytest
from sage_wow.agent.grind_acquisition import learn_creature, local_target
from test_grind_product_spec import Rig


def enable(r):
    r.c.config.update(target_opening_cast=True, committed_combat=True,
        target_name_box=[205, 95, 360, 120])
    r.world.target_level=1
    learn_creature(r.c, {'own_source':True, 'event':'PARTY_KILL',
        'dest_guid':'Creature-0-offline-1', 'dest_name':'Young Wolf',
        'source_guid':'Player-offline', 'source_name':'Test Player', 'timestamp':'offline'})


async def acquire(r):
    r.world.name=''
    r.c.hunt.compact_stage='acquire'
    r.c.hunt.selected_presence=False
    await r.choose('target_enemy')
    r.world.name='Young Wolf'
    return deepcopy(r.c.hunt.pending['receipt'])


async def process(r):
    r.c.wait_until=0
    return await r.c.process(r.world.capture())


def smites(r):
    return [e for e in r.backend.events if e[0]=='text' and e[1]=='/cast [harm,nodead] Smite']


def test_first_acquisition_casts_once_without_another_sage_call_or_menu_change(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            enable(r);await r.start();tab=await acquire(r)
            before=len(r.sage.calls);options=r.sage.calls[-1]['options']
            assert 'target_enemy' in options and 'acquire_and_engage' not in options
            assert options['target_enemy'].binding=={'type':'keypress','keycode':r.controls['target_enemy']['keycode'],'hold_seconds':.08}
            assert 'immediately attempts one opening Smite' in options['target_enemy'].description
            result=await process(r)
            assert result.status=='dispatched' and result.decision is None
            assert len(r.sage.calls)==before and len(smites(r))==1
            receipt=result.receipt
            assert receipt['authorization_type']=='user_authorized_target_opener'
            assert receipt['authorization_id']==tab['receipt_id']
            assert receipt['request_id']==tab['request_id']
            assert receipt['selected_binding']['type']=='cast_guarded'
            assert r.c.target_opener is None and r.c.hunt.pending['family']=='combat'
            assert not r.c.hunt.credited_kills
            # The next frame must return to Sage, not repeat the automatic opener.
            await r.choose('damaged_alive')
            assert len(r.sage.calls)==before+1 and len(smites(r))==1
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('missing',['known_creature_type','one_numeric_level','living_bar'])
def test_opener_reports_the_actual_missing_local_evidence(tmp_path,missing):
    async def run():
        import json
        r=Rig(tmp_path)
        try:
            enable(r);await r.start();await acquire(r)
            if missing=='known_creature_type':r.c.opening_creatures.clear()
            elif missing=='one_numeric_level':r.world.target_level=None
            else:r.world.health='black'
            r.sage.answers.append(None)
            await process(r)
            payload=json.loads(r.store.connection.execute(
                "select payload_json from events where event_type='grind_target_opener_local_check' order by rowid desc limit 1").fetchone()[0])
            assert missing in payload['failed_predicates']
            assert not smites(r)
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['travel', 'choose_area', 'search'])
@pytest.mark.parametrize('locally_known', [True, False])
def test_completed_targeting_is_assessed_before_resuming_prior_search_task(tmp_path, phase, locally_known):
    """Live-run regression: travel consumed a turn before assessing its recovery Tab."""
    async def run():
        r=Rig(tmp_path)
        try:
            enable(r);await r.start();tab=await acquire(r)
            h=r.c.hunt;h.phase=phase;h.planning_requested=phase=='choose_area'
            h.strategy_required={'reason':'unchanged_search', 'captured_at':tab['occurred_at'],
                'frame_id':tab['source_frame_id'], 'search_revision':h.search_revision}
            debt=deepcopy(h.strategy_required)
            async def unexpected_travel(*args):
                pytest.fail('Travel preempted completed targeting before its effect was assessed')
            r.c.travel=unexpected_travel
            before=len(r.sage.calls)
            if not locally_known:
                r.c.opening_creatures.clear()
                r.sage.answers.append(None)
            result=await process(r)
            if locally_known:
                assert result.status=='dispatched' and result.decision is None
                assert len(r.sage.calls)==before and len(smites(r))==1
                assert h.phase=='fight' and h.pending['family']=='combat'
                await r.choose('damaged_alive')
                assert len(r.sage.calls)==before+1 and len(smites(r))==1
            else:
                assert len(r.sage.calls)==before+1 and not smites(r)
                assert 'explore_visible' not in r.sage.calls[-1]['options']
                assert h.pending['receipt']['receipt_id']==tab['receipt_id']
            # Acquiring/casting is not proof that the failed search moved.
            assert h.strategy_required==debt and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('invalid', ['generation', 'missing_pending', 'partial'])
def test_old_or_incomplete_targeting_does_not_override_current_travel(tmp_path, invalid):
    async def run():
        from sage_wow.agent.cycle import CycleResult
        r=Rig(tmp_path)
        try:
            enable(r);await r.start();await acquire(r)
            r.c.hunt.phase='travel'
            if invalid=='generation':r.c.cycle._input_generation+=1
            elif invalid=='missing_pending':r.c.hunt.pending=None
            else:r.c.hunt.pending['receipt'].update(completed=False,dispatch_unknown=True)
            async def observed_travel(*args):return CycleResult('offline_travel')
            r.c.travel=observed_travel
            assert (await process(r)).status=='offline_travel'
            assert not smites(r)
        finally:await r.close()
    asyncio.run(run())


def test_same_name_new_acquisition_drops_old_range_block_but_preserves_failed_attempt(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            enable(r);await r.start()
            await r.choose('attack_mob_level_1')
            r.world.error='Out of range';await r.choose('position_error')
            h=r.c.hunt;old_key=h.approach['history_key']
            old=deepcopy(h.target_history[old_key]);assert h.cast_obligation
            r.world.error=''
            await acquire(r);before=len(r.sage.calls)
            result=await process(r)
            assert result.status=='dispatched' and len(r.sage.calls)==before
            assert len(smites(r))==2 and not h.cast_obligation and h.cast_error is None
            assert h.approach['history_key']!=old_key
            assert h.target_history[old_key]['cast_obligation']==old['cast_obligation']
            assert h.target_history[old_key]['cast_error']==old['cast_error']
            assert not h.credited_kills
            # Old range state cannot reappear on the second cast either.
            await r.choose('attack_mob_level_1')
            assert len(smites(r))==3 and not h.cast_obligation
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['missing','unknown_name','wrong_level','dead','self','low_health',
    'unreadable_level','source_changed','generation','partial_tab','expiry','pause'])
def test_unclear_invalid_or_stale_acquisition_never_autocasts(tmp_path,change):
    async def run():
        r=Rig(tmp_path)
        try:
            enable(r);await r.start();await acquire(r)
            if change=='missing':r.world.name=''
            elif change=='unknown_name':r.world.name='Unknown Creature'
            elif change=='wrong_level':r.world.target_level=9
            elif change=='dead':r.world.health='black'
            elif change=='self':r.world.name='Test Player'
            elif change=='low_health':r.world.player_health='black'
            elif change=='unreadable_level':r.world.target_level=None
            elif change=='generation':r.c.cycle._input_generation+=1
            elif change=='partial_tab':r.c.hunt.pending['receipt'].update(completed=False,dispatch_unknown=True)
            elif change=='expiry':r.c.target_opener['deadline']=0
            elif change=='pause':r.c.pause_focus()
            elif change=='source_changed':
                original=r.c.guard
                async def changed_guard(frame,target,**kw):
                    check=await original(frame,target,**kw)
                    async def check_changed(current):
                        r.world.name='Different Wolf'
                        return await check(current)
                    return check_changed
                r.c.guard=changed_guard
            # Falling back to Sage is expected; no automatic input is permitted.
            r.sage.answers.append(None)
            await process(r)
            assert not smites(r)
            assert r.c.target_opener is None
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['pause','generation','pending','level_policy'])
def test_authority_and_target_policy_are_rechecked_after_guard(tmp_path,change):
    async def run():
        r=Rig(tmp_path)
        try:
            enable(r);await r.start();await acquire(r)
            original=r.c.guard
            async def changed_guard(frame,target,**kw):
                check=await original(frame,target,**kw)
                async def changed(current):
                    result=await check(current)
                    if change=='pause':r.c.pause_focus()
                    elif change=='generation':r.c.cycle._input_generation+=1
                    elif change=='pending':r.c.hunt.pending=None
                    else:r.c.hunt.target_bands_by_player_level[1]=[2,3]
                    return result
                return changed
            r.c.guard=changed_guard;r.sage.answers.append(None)
            await process(r)
            assert not smites(r) and r.c.target_opener is None
        finally:await r.close()
    asyncio.run(run())


def test_fresh_player_or_friendly_observation_vetoes_catalog_name(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            enable(r);await r.start();frame=r.world.capture()
            from sage_wow.agent.grind_resources import hud_resources
            target=await r.c.raw_target_proposal(frame)
            for kind in ('player','friendly_or_self'):
                assert local_target(r.c,frame,{**target,'visual_observation':{
                    'target_kind':kind}},hud_resources(r.c,frame)) is None
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('bounds,accepted',[
    ({'x':204,'y':100,'width':156,'height':15},True),
    ({'x':210,'y':150,'width':140,'height':15},False),
    ({'x':190,'y':100,'width':170,'height':15},False),
])
def test_ocr_name_edges_may_cross_name_band_but_not_selected_hud(tmp_path,bounds,accepted):
    async def run():
        from dataclasses import replace
        from sage_wow.agent.grind_resources import hud_resources
        r=Rig(tmp_path)
        try:
            enable(r);await r.start();frame=r.world.capture()
            target=await r.c.raw_target_proposal(frame)
            target['name_row']=replace(target['name_row'],bounds=bounds)
            assert (local_target(r.c,frame,target,hud_resources(r.c,frame)) is not None)==accepted
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('fact',[
    {'own_source':False,'event':'PARTY_KILL','dest_guid':'Creature-0-1','dest_name':'Young Wolf'},
    {'own_source':True,'event':'PARTY_KILL','dest_guid':'Player-0-1','dest_name':'Young Wolf'},
    {'own_source':True,'event':'SPELL_CAST_FAILED','dest_guid':'Creature-0-1','dest_name':'Young Wolf'},
])
def test_creature_catalog_does_not_learn_from_unattributed_player_or_failed_cast(fact):
    from types import SimpleNamespace
    c=SimpleNamespace()
    assert not learn_creature(c,fact) and not getattr(c,'opening_creatures',{})


def test_clear_ignores_health_animation_but_rechecks_selected_identity(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            enable(r);await r.start()
            async def animation():r.world.health='yellow'
            r.sage.hook=animation
            result=await r.choose('reject_selected_target')
            assert result.status=='dispatched'
            assert r.controls['escape']['keycode'] in r.physical_keys()
            assert not smites(r)
        finally:await r.close()
    asyncio.run(run())


def test_sage_can_open_after_reacquisition_even_when_automatic_opening_is_disabled(tmp_path):
    async def run():
        from test_grind_committed_combat import add_current_observer
        r=Rig(tmp_path)
        try:
            r.c.config['committed_combat']=True
            r.world.target_level=1
            await r.start();await r.choose('attack_mob_level_1')
            r.world.error='Out of range';await r.choose('position_error')
            h=r.c.hunt;old_key=h.approach['history_key'];old=deepcopy(h.target_history[old_key])
            r.world.error='';await acquire(r);add_current_observer(r)
            await r.choose('attack_mob_level_1')
            assert len(smites(r))==2 and not h.cast_obligation and h.cast_error is None
            assert h.approach['history_key']!=old_key
            assert h.target_history[old_key]['cast_error']==old['cast_error']
            assert r.c.cycle.last_receipt['authorization_type']=='sage_decision'
            assert not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_many_same_name_acquisitions_remain_bounded_without_inventing_deaths(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            enable(r);await r.start()
            for _ in range(28):
                await acquire(r)
                assert (await process(r)).status=='dispatched'
                r.world.error='Out of range';await r.choose('position_error');r.world.error=''
            assert len(smites(r))==28 and len(r.c.hunt.target_history)<=24
            assert not r.c.stopped and not r.c.hunt.credited_kills
            assert not any(x.get('completed') for x in r.c.hunt.target_history.values())
            events=r.store.connection.execute("select count(*) from events where event_type='grind_acquisition_reassessed'").fetchone()[0]
            assert events==28
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('level,expected',[(1,True),(2,True),(3,True),(4,True),(5,False)])
def test_local_opener_preserves_level_three_allowed_band(tmp_path,level,expected):
    async def run():
        r=Rig(tmp_path)
        try:
            enable(r);await r.start()
            r.c.level.last_confirmed_level=3;r.c.hunt.target_bands_by_player_level={3:[1,4]}
            await acquire(r);r.world.target_level=level
            if not expected:r.sage.answers.append(None)
            await process(r)
            assert bool(smites(r))==expected
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('when',['before_text','after_text'])
def test_partial_opening_stops_without_replaying_target_permission(tmp_path,when):
    async def run():
        r=Rig(tmp_path)
        try:
            enable(r);await r.start();await acquire(r)
            original=r.backend.text
            def broken(value):
                if when=='after_text':original(value)
                raise RuntimeError('offline uncertain cast submission')
            r.backend.text=broken
            result=await process(r)
            assert result.status=='execution_failed' and r.c.stopped
            assert r.c.reason=='partial_or_unknown_grind_input'
            assert r.c.target_opener is None
            before=len(smites(r));await process(r)
            assert len(smites(r))==before and before<=1
        finally:await r.close()
    asyncio.run(run())
