"""Interrupted sensing through real controller offers, fake providers and input.

The body point proposal is synthetic and the existing loot clock advances 21s.
No final acquisition stage, episode, pending receipt or offensive debt is seeded.
"""
import asyncio
from copy import deepcopy
import time

import pytest

from test_grind_inspection_source_handoff import SourceRig
from test_grind_exact_band_observation import start_band
from test_grind_loot_budget import enable
from sage_wow.agent import grind_inspection as inspection, grind_loot, grind_loot_budget
from sage_wow.agent.cycle import ActionCandidate


async def interrupted(r, monkeypatch):
    await start_band(r,4)
    r.c.config['target_bands_by_player_level'][4]=[1,4]
    r.c.hunt.target_bands_by_player_level[4]=[1,4]
    r.c.config['target_opening_cast']=True
    enable(r)
    r.backend.mouse_button=lambda *args:r.backend.events.append(('mouse',*args))
    r.backend.mouse_move=lambda *args:r.backend.events.append(('mouse_move',*args))
    r.world.scenery_patches=[((90,245,110,265),'#7b6e63')]
    monkeypatch.setattr(grind_loot,'_corpse_choices',lambda *args:[ActionCandidate(
        'corpse_select_0','Offline synthetic body point',{'type':'click','image_x':100,'image_y':255})])
    clock={'offset':0.};real_clock=grind_loot_budget.monotonic
    monkeypatch.setattr(grind_loot_budget,'monotonic',lambda:real_clock()+clock['offset'])
    assert (await r.observe_target()).status=='grind_reobserve'
    cast=await r.choose('attack_mob_level_2')
    assert cast.status=='dispatched' and cast.receipt['possible_input']
    owner=r.c.hunt.combat_history_key
    r.world.name='';r.world.target_level=None
    r.answers.update(selected_hud='absent',name='other_or_unknown',level='unknown',target_kind='unknown',life_state='unknown')
    assert (await r.observe_target()).status=='grind_reobserve'
    old=deepcopy(r.c.target_inspection_episode)
    assert old['disposition']=='source_resolved_absent'
    assert (await r.choose('inspect_recent_corpse')).status=='dispatched'
    assert r.c.hunt.encounter_ended and r.c.hunt.cast_obligation and r.c.hunt.pending is None
    assert r.c.hunt.unassessed[-1]['receipt']['receipt_id']==cast.receipt['receipt_id']
    history=deepcopy(r.c.hunt.target_history[owner])
    click=await r.choose('corpse_select_0')
    assert click.status=='dispatched' and click.receipt['possible_input']
    assert click.receipt['generation_before']==old['source_absence']['generation']
    assert click.receipt['generation_after']==click.receipt['generation_before']+3
    clock['offset']=21.
    assert (await r.choose('loot_inspect')).status=='dispatched'
    assert (await r.choose()).status=='grind_reobserve'
    assert r.c.loot.data['outcome']=='skipped_unverified' and not r.c.loot.pending
    return old, owner, history, cast, click


async def positive(r, name='Rockjaw Trogg', numeral='1', *, expect_observer=True):
    r.world.name=name;r.world.target_level=1
    r.answers.update(selected_hud='present',name='other_or_unknown',level=numeral,target_kind='creature',life_state='alive')
    r.c.observer_last_at=0
    return await r.choose() if expect_observer else await r.choose(None)


@pytest.mark.parametrize('name',['Rockjaw Trogg','Ragged Young Wolf'])
def test_natural_interrupted_corpse_loot_boundary_tab_observer(tmp_path,monkeypatch,name):
    async def run():
        r=SourceRig(tmp_path/name)
        try:
            old,owner,history,cast,click=await interrupted(r,monkeypatch)
            assess=await r.choose('no_selected_frame')
            assert assess.status=='dispatched' and not assess.receipt['possible_input']
            e=r.c.target_inspection_episode;lineage=e['lineage'];key=lineage['question_key']
            assert e['id']==old['id'] and e['source_absence']['generation']==old['source_absence']['generation']
            assert e['boundary_absence']['generation']==click.receipt['generation_after']
            assert e['boundary_absence']['assessment_receipt']==assess.receipt
            assert r.c.hunt.question_debt[key]==1
            tab=await r.choose('target_enemy')
            assert tab.status=='dispatched' and tab.receipt['possible_input']
            assert r.c.hunt.pending['sensing_boundary_witness']['receipt_id']==tab.receipt['receipt_id']
            selected=await positive(r,name)
            assert selected.status=='grind_reobserve'
            new=r.c.target_inspection_episode
            assert new['id']!=old['id'] and new['attempts']==1 and len(r.wire)==3
            assert new['sensing_proof']['receipt_id']==tab.receipt['receipt_id']
            assert key not in r.c.hunt.question_debt and r.c.hunt.pinned_boundary_question is None
            assert r.c.hunt.target_history[owner]==history and r.c.hunt.cast_obligation
            assert r.c.hunt.pending['receipt']['receipt_id']==tab.receipt['receipt_id']
            assert not r.c.hunt.credited_kills and r.c.target_opener is None
            await r.choose(None)
            assert 'no_selected_frame' not in r.sage.calls[-1]['options']
            assert new['id']==r.c.target_inspection_episode['id']
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('age',[59,60,61])
def test_missed_boundary_uses_only_original_residual_allowance(tmp_path,monkeypatch,age):
    async def run():
        r=SourceRig(tmp_path/str(age))
        try:
            old,owner,history,_,_=await interrupted(r,monkeypatch)
            # Simulate a missed boundary question, preserving real ordinary Tab
            # behavior and its actual receipt. This grants no pre-input witness.
            monkeypatch.setattr(inspection,'needs_boundary',lambda *args:False)
            tab=await r.choose('target_enemy')
            assert tab.status=='dispatched' and 'sensing_boundary_witness' not in r.c.hunt.pending
            e=r.c.target_inspection_episode
            r.c.hunt.active_seconds=e['started_active_at']+age
            r.c.hunt.active_tick=None
            await positive(r,expect_observer=age<60)
            assert r.c.target_inspection_episode['id']==old['id']
            assert e['started_active_at']==old['started_active_at']
            assert e['source_absence']['invalidated_by'] and 'sensing_proof' not in e
            assert len(r.wire)==(3 if age<60 else 2)
            assert e['attempts']==(2 if age<60 else 1)
            assert r.c.hunt.target_history[owner]==history
            if age>=60:
                options=r.sage.calls[-1]['options']
                assert 'no_selected_frame' not in options and 'reinspect_selected_frame' not in options
                assert 'reject_selected_target' not in options
                exit=await r.choose('change_search_strategy')
                assert exit.status=='dispatched'
                assert r.c.hunt.strategy_required['selected_exit']['kind']=='selected_sensing'
                assert r.c.hunt.target_history[owner]==history
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['null','unclear','exception','cancel','invalidated','ui'])
def test_boundary_actual_admission_debt_survives_outcomes_and_caps_requests(tmp_path,monkeypatch,failure):
    async def run():
        r=SourceRig(tmp_path/failure)
        try:
            await interrupted(r,monkeypatch)
            async def mutate():
                if failure=='cancel':raise asyncio.CancelledError()
                if failure=='invalidated':r.c.revision+=1
            r.sage.hook=mutate if failure in {'cancel','invalidated'} else None
            answer={'null':None,'unclear':'cannot_assess','exception':RuntimeError('offline timeout'),
                'cancel':None,'invalidated':'no_selected_frame','ui':'ui_blocked'}[failure]
            try:await r.choose(answer)
            except asyncio.CancelledError:
                assert failure=='cancel'
            r.sage.hook=None;r.sage.answers.clear()
            e=r.c.target_inspection_episode;lineage=e['lineage'];key=lineage['question_key']
            assert r.c.hunt.question_debt[key]==1 and len(lineage['request_ids'])==1
            assert not e.get('boundary_absence')
            admitted=r.events('grind_interrupted_boundary_admitted')
            assert admitted[0]['reasoning']=='off'
            # Exercise the ordinary unrelated/reserved store; only its live
            # admission-counted entry remains pinned under bounded history.
            for i in range(70):
                r.c.hunt.question('unrelated:'+str(i));r.c.hunt.question_answer(unclear=True)
            assert len(r.c.hunt.question_debt)==64 and r.c.hunt.question_debt[key]==1
            r.c.hunt.question('reserved-ui',reserved=True);r.c.hunt.question_answer()
            assert r.c.hunt.question_debt[key]==1
            if failure=='ui':
                await r.choose('world_normal_confirmed')
            await r.choose('cannot_assess')
            assert r.c.hunt.question_debt[key]==2 and len(lineage['request_ids'])==2
            admitted=r.events('grind_interrupted_boundary_admitted')
            assert admitted[-1]['reasoning']==('auto' if failure in {'null','unclear'} else 'off')
            count=len(r.sage.calls)
            await r.choose(None)
            assert len(r.sage.calls)==count+1 and r.c.hunt.question_debt[key]==2
            assert {'target_enemy','turn_left','turn_right','forward'} <= r.sage.calls[-1]['options'].keys()
            assert not {'no_selected_frame','reinspect_selected_frame','cannot_assess'} & r.sage.calls[-1]['options'].keys()
            assert len(r.events('grind_interrupted_boundary_admitted'))==2
            assert key==r.c.hunt.pinned_boundary_question
            assert all(x.get('question_id')!=key for x in r.c.hunt.question_history)
            assert all(x['accounting_kind']=='admitted_boundary_requests' for x in admitted)
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation',['pending','positive','epoch','configuration','world','heal','threat','resources','hash'])
def test_boundary_awaited_response_cannot_replace_stronger_current_work(tmp_path,monkeypatch,mutation):
    async def run():
        from PIL import Image,ImageDraw
        r=SourceRig(tmp_path/mutation)
        try:
            await interrupted(r,monkeypatch)
            async def change():
                if mutation=='pending':
                    r.c.hunt.pending={'family':'motion','receipt':{'receipt_id':'new-real-owner'}}
                    r.replacement=r.c.hunt.pending
                elif mutation=='positive':r.c.hunt.selection_revision+=1
                elif mutation=='epoch':r.c.cycle.session_epoch='new-epoch'
                elif mutation=='configuration':r.c.config['target_name_box']=[201,95,410,120]
                elif mutation=='world':r.c.require_world=True
                elif mutation=='heal':r.c.heal_pending={'receipt_id':'current-heal'}
                elif mutation=='threat':r.c.hunt.active_threat={'source':'new-current-threat'}
                elif mutation=='resources':r.c.current_hud={'player_health':.1,'health_confidence':1.}
                else:
                    # Mutate the pinned actual source, not just its composition.
                    path=pinned[0]['frame'].image_path
                    with Image.open(path) as raw:
                        image=raw.copy();ImageDraw.Draw(image).point((211,101),fill='red');image.save(path)
            pinned=[];base=inspection.boundary_request
            def prepare(*args):
                request=base(*args);pinned.append(request);return request
            monkeypatch.setattr(inspection,'boundary_request',prepare)
            r.sage.hook=change
            await r.choose('no_selected_frame')
            e=r.c.target_inspection_episode
            assert not e.get('boundary_absence') and not r.c.hunt.confirmed_target_absences
            assert r.c.hunt.question_debt[e['lineage']['question_key']]==1
            if mutation=='pending':assert r.c.hunt.pending is r.replacement
            assert len(r.wire)==2
        finally:await r.close()
    asyncio.run(run())


def test_boundary_pre_admission_rejection_and_duplicate_hook_do_not_charge(tmp_path,monkeypatch):
    async def run():
        r=SourceRig(tmp_path/'admission')
        try:
            await interrupted(r,monkeypatch)
            original=inspection.start_boundary
            first=[]
            def rejected(c,request,request_id):
                first.append(request);c.revision+=1
                return original(c,request,request_id)
            monkeypatch.setattr(inspection,'start_boundary',rejected)
            count=len(r.sage.calls)
            result=await r.choose()
            assert len(r.sage.calls)==count and result.status=='grind_reobserve'
            request=first[0];key=request['key'];assert r.c.hunt.question_debt[key]==0
            monkeypatch.setattr(inspection,'start_boundary',original)
            await r.choose('no_selected_frame')
            assert r.c.hunt.question_debt[key]==1
            with pytest.raises(ValueError):original(r.c,request,request['lineage']['request_ids'][0])
            assert r.c.hunt.question_debt[key]==1
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('disabled',[False,True])
def test_natural_unavailable_selected_task_travel_typed_clear_preserves_old_combat(tmp_path,monkeypatch,disabled):
    async def run():
        from test_grind_selected_task_exit import exhaust, counters
        r=SourceRig(tmp_path/str(disabled))
        try:
            old,owner,history,_,_=await interrupted(r,monkeypatch)
            monkeypatch.setattr(inspection,'needs_boundary',lambda *args:False)
            await r.choose('target_enemy')
            if disabled:r.c.config['selected_target_observer']['enabled']=False
            else:
                r.c.hunt.active_seconds=old['started_active_at']+61;r.c.hunt.active_tick=None
            await positive(r,expect_observer=False)
            assert (await r.choose('change_search_strategy')).status=='dispatched'
            h=r.c.hunt;intent=h.strategy_required['selected_exit']
            assert intent['kind']=='selected_sensing' and intent['episode']['id']==old['id']
            assert (await r.choose('explore_visible')).status=='dispatched'
            await exhaust(r)
            before=counters(h);before_clear=deepcopy(h.action_failures)
            assert (await r.choose('reject_selected_target')).status=='dispatched'
            assert h.pending['purpose']=='selected_task_abandon'
            r.world.name='';r.world.target_level=None
            assert (await r.choose('target_cleared')).status=='dispatched'
            assert intent['disposition']=='closed' and h.pending is None
            assert h.target_history[owner]==history and h.cast_obligation
            assert counters(h)==before and h.action_failures==before_clear
            assert r.c.target_inspection_episode['id']==old['id']
            assert r.c.target_inspection_episode['attempts']==old['attempts'] and len(r.wire)==2
        finally:await r.close()
    asyncio.run(run())


def test_positive_boundary_with_numeric_ocr_keeps_unused_clock_unstarted(tmp_path,monkeypatch):
    async def run():
        r=SourceRig(tmp_path/'unstarted')
        try:
            old,*_=await interrupted(r,monkeypatch)
            await r.choose('no_selected_frame');await r.choose('target_enemy')
            r.drop_level=False
            await positive(r,expect_observer=False)
            e=r.c.target_inspection_episode
            assert e['id']!=old['id'] and e['attempts']==0 and e['started_active_at'] is None
            r.c.hunt.active_seconds+=65;r.c.hunt.active_tick=None
            await r.choose('attack_mob_level_1')
            assert e['attempts']==0 and e['started_active_at'] is None
            r.drop_level=True
            await r.observe_target()
            assert e['attempts']==1 and e['started_active_at'] is not None
            assert len(r.wire)==3 and r.events('dispatch_guard_checked')[-1]['approved']
        finally:await r.close()
    asyncio.run(run())


def test_residual_success_cache_expiry_and_spent_clock_do_not_veto_real_combat(tmp_path,monkeypatch):
    async def run():
        r=SourceRig(tmp_path/'ongoing')
        try:
            old,*_=await interrupted(r,monkeypatch)
            monkeypatch.setattr(inspection,'needs_boundary',lambda *args:False)
            await r.choose('target_enemy');await positive(r)
            e=r.c.target_inspection_episode
            assert e['id']==old['id'] and e['attempts']==2
            first=await r.choose('attack_mob_level_1')
            assert first.status=='dispatched' and first.receipt['possible_input']
            # Expire the actual cached provider timestamp, keep the spent ledger.
            r.c.target_observation['result']['captured_at']='2020-01-01T00:00:00+00:00'
            r.c.hunt.active_seconds=e['started_active_at']+65;r.c.hunt.active_tick=None
            r.drop_level=False
            # A pending cast still needs its ordinary feedback when the
            # visual living cache expires. Supply a real changed health frame
            # and accepted assessment before the next guarded cast.
            r.world.health='yellow'
            assert (await r.choose('damaged_alive')).status=='dispatched'
            second=await r.choose('attack_mob_level_1')
            assert second.status=='dispatched' and second.receipt['possible_input']
            assert e is r.c.target_inspection_episode and e['attempts']==2 and len(r.wire)==3
            r.drop_level=True;r.c.observer_last_at=0
            await r.choose(None)
            assert len(r.wire)==3 and e['id']==r.c.target_inspection_episode['id']
            assert r.c.hunt.pending['receipt']['receipt_id']==second.receipt['receipt_id']
            assert not r.c.hunt.pending['outcome_consumed'] and not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation',['boundary_hash','boundary_configuration','boundary_age'])
def test_ordinary_tab_witness_revalidated_before_input_without_changing_tab_authority(tmp_path,monkeypatch,mutation):
    async def run():
        r=SourceRig(tmp_path/mutation)
        try:
            old,*_=await interrupted(r,monkeypatch)
            await r.choose('no_selected_frame')
            async def invalidate():
                fact=r.c.target_inspection_episode['boundary_absence']
                if mutation=='boundary_hash':fact['source_sha256']='tampered'
                elif mutation=='boundary_configuration':fact['configuration']={}
                else:fact['captured_at']='2020-01-01T00:00:00+00:00'
            r.sage.hook=invalidate
            tab=await r.choose('target_enemy');r.sage.hook=None
            assert tab.status=='dispatched' and tab.receipt['possible_input']
            assert 'sensing_boundary_witness' not in r.c.hunt.pending
            await positive(r)
            assert r.c.target_inspection_episode['id']==old['id']
            assert r.c.target_inspection_episode['attempts']==2 and len(r.wire)==3
            assert 'sensing_proof' not in r.c.target_inspection_episode
        finally:await r.close()
    asyncio.run(run())


def test_valid_second_boundary_answer_can_bind_positive_edge_without_third_request(tmp_path,monkeypatch):
    async def run():
        r=SourceRig(tmp_path/'second-valid')
        try:
            old,*_=await interrupted(r,monkeypatch)
            await r.choose(None)
            await r.choose('no_selected_frame')
            lineage=r.c.target_inspection_episode['lineage'];key=lineage['question_key']
            assert r.c.hunt.question_debt[key]==2
            tab=await r.choose('target_enemy')
            await positive(r)
            assert len(lineage['request_ids'])==2 and lineage['retired_by']['receipt_id']==tab.receipt['receipt_id']
            assert r.c.target_inspection_episode['id']!=old['id'] and r.c.target_inspection_episode['attempts']==1
            assert key not in r.c.hunt.question_debt
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('episode_state',['none','unavailable','deferred'])
def test_projected_cache_conflict_is_bookkeeping_without_admission_or_crash(tmp_path,episode_state):
    async def run():
        from sage_wow.agent import grind_perception
        r=SourceRig(tmp_path/episode_state)
        try:
            await start_band(r,4)
            await r.observe_target();await r.choose(None)
            assert r.c.target_inspection_episode is None
            frame=r.world.capture();raw=await r.c.raw_target_proposal(frame)
            # Direct lower-level conflict fixture: current OCR contradicts the
            # cached observer identity. No pending state or debt is fabricated.
            raw['name']='Different Creature'
            if episode_state!='none':
                r.c.target_inspection_episode=inspection._episode(r.c,frame,raw)
                if episode_state=='unavailable':r.c.target_inspection_episode['attempts']=2
                else:r.c.observer_last_at=time.time()
            prior=deepcopy(r.c.target_inspection_episode);calls=len(r.wire)
            result=grind_perception.enrich(r.c,frame,raw)
            assert result['observation_conflict']
            assert len(r.wire)==calls
            if prior is None:assert r.c.target_inspection_episode is None
            else:
                e=r.c.target_inspection_episode
                assert e['id']==prior['id'] and e['attempts']==prior['attempts'] and e['started_active_at']==prior['started_active_at']
            assert not r.events('grind_target_inspection_attempt')[1:]
        finally:await r.close()
    asyncio.run(run())


def test_disabled_without_episode_has_stable_typed_selected_work_exit(tmp_path):
    async def run():
        r=SourceRig(tmp_path/'disabled-no-ledger')
        try:
            await start_band(r,4)
            r.c.config['selected_target_observer']['enabled']=False
            await r.choose(None)
            work=inspection.current_work(r.c)
            assert work and work['kind']=='selected_work_without_observer_allowance'
            assert r.c.target_inspection_episode is None and not r.wire
            assert 'no_selected_frame' not in r.sage.calls[-1]['options']
            assert 'reinspect_selected_frame' not in r.sage.calls[-1]['options']
            assert 'reject_selected_target' not in r.sage.calls[-1]['options']
            result=await r.choose('change_search_strategy')
            assert result.status=='dispatched'
            assert r.c.hunt.strategy_required['selected_exit']['episode_id']==work['id']
            assert r.c.target_inspection_episode is None and not r.wire
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('numeral',['1','unknown'])
def test_residual_positive_identity_pixels_prevent_ocr_relabel_budget_refund(tmp_path,monkeypatch,numeral):
    async def run():
        r=SourceRig(tmp_path/numeral)
        try:
            old,*_=await interrupted(r,monkeypatch)
            monkeypatch.setattr(inspection,'needs_boundary',lambda *args:False)
            await r.choose('target_enemy');await positive(r,numeral=numeral)
            e=r.c.target_inspection_episode
            positive_source=deepcopy(e['residual_work'])
            assert e['attempts']==2 and positive_source['source_image']!=e['source_image']
            original=r.c.raw_target_proposal
            async def mislabeled(frame):return {**await original(frame),'name':'Burly Rockjaw Trogg'}
            r.c.raw_target_proposal=mislabeled
            r.c.observer_last_at=0
            await r.choose(None)
            assert r.c.target_inspection_episode is e and e['id']==old['id'] and e['attempts']==2
            assert len(r.wire)==3 and not r.events('grind_target_inspection_retired')[-1:]
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation',['epoch','revision','configuration','source_hash'])
def test_anonymous_projection_conflict_has_no_authority_after_owner_changes(tmp_path,mutation):
    async def run():
        from sage_wow.agent import grind_perception
        from PIL import Image,ImageDraw
        r=SourceRig(tmp_path/mutation)
        try:
            await start_band(r,4);await r.observe_target();await r.choose(None)
            assert r.c.target_inspection_episode is None
            frame=r.world.capture();raw=await r.c.raw_target_proposal(frame)
            bad={**raw,'name':'Wrong OCR name'}
            assert grind_perception.enrich(r.c,frame,bad)['observation_conflict']
            conflict=deepcopy(r.c.target_inspection_conflict)
            generation=r.c.cycle.input_generation;continuity=r.c.hunt.target_continuity
            if mutation=='epoch':r.c.cycle.invalidate('offline_clean_focus')
            elif mutation=='revision':r.c.revision+=1
            elif mutation=='configuration':r.c.config['target_name_box']=[201,95,410,120]
            else:
                with Image.open(conflict['source_image']) as original:
                    image=original.copy();ImageDraw.Draw(image).point((212,103),fill='red');image.save(conflict['source_image'])
            result=inspection.unresolved(r.c,raw,frame)
            assert 'observation_conflict' not in result
            assert r.c.target_inspection_conflict==conflict  # Retained diagnostic only.
            assert r.c.cycle.input_generation==generation and r.c.hunt.target_continuity==continuity
            assert r.c.target_inspection_episode is None and len(r.wire)==1
        finally:await r.close()
    asyncio.run(run())


async def committed_reinspection(r):
    await start_band(r,4)
    assert (await r.observe_target()).status=='grind_reobserve'
    await r.choose(None);await r.choose(None)
    r.c.observer_last_at=0
    result=await r.choose('reinspect_selected_frame')
    assert result.status=='dispatched' and not result.receipt['possible_input']
    operation=r.c.target_reinspection_operation
    assert operation['state']=='committed'
    return operation


@pytest.mark.parametrize('delay',['throttle','heal','world_wait','stale'])
def test_real_committed_factual_reinspection_waits_then_admits_once(tmp_path,delay):
    async def run():
        r=SourceRig(tmp_path/delay)
        try:
            op=await committed_reinspection(r)
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            count=len(r.wire);episode=r.c.target_inspection_episode
            original_fresh=r.c.fresh
            if delay=='throttle':r.c.observer_last_at=time.time()
            elif delay=='heal':r.c.heal_pending={'offline_temporary_task':True}
            elif delay=='world_wait':r.c.require_world=True
            else:r.c.fresh=lambda f:False
            if delay=='throttle':await r.choose(None)
            else:inspection.refresh_operation(r.c,frame,target)
            assert op['state']=='deferred' and not inspection.operation_ready(r.c,frame,target)
            assert r.c.target_inspection_episode is episode and len(r.wire)==count
            r.c.heal_pending=None;r.c.require_world=False;r.c.fresh=original_fresh
            r.c.observer_last_at=0
            r.c.revision+=1  # Same-task bookkeeping is not selected-source supersession.
            result=await r.choose()
            assert result.status=='grind_reobserve' and len(r.wire)==count+1
            assert op['state']=='admitted' and op['attempt']==1
            assert r.c.target_inspection_episode['id']==op['admitted_episode_id']
            await r.choose(None)
            assert len(r.wire)==count+1 and not r.physical()
            assert not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation',['epoch','generation','continuity','configuration','source_hash'])
def test_committed_factual_operation_cancels_on_actual_owner_supersession(tmp_path,mutation):
    async def run():
        from PIL import Image,ImageDraw
        r=SourceRig(tmp_path/mutation)
        try:
            op=await committed_reinspection(r);count=len(r.wire)
            if mutation=='epoch':r.c.cycle.invalidate('offline_focus_change')
            elif mutation=='generation':r.c.cycle._input_generation+=1
            elif mutation=='continuity':r.c.hunt.target_continuity+=1
            elif mutation=='configuration':r.c.config['target_name_box']=[201,95,410,120]
            else:
                with Image.open(op['source_image']) as source:
                    image=source.copy();ImageDraw.Draw(image).point((212,103),fill='red');image.save(op['source_image'])
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            inspection.refresh_operation(r.c,frame,target)
            assert op['state']=='cancelled' and not inspection.operation_ready(r.c,frame,target)
            assert len(r.wire)==count and 'admitted_episode_id' not in op
            assert not r.physical() and not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('color',['green','black'])
@pytest.mark.parametrize('project_before_blank',[False,True])
def test_source_only_nameless_bar_conflict_cannot_rebase_onto_later_blank(tmp_path,monkeypatch,color,project_before_blank):
    async def run():
        from PIL import Image,ImageDraw
        r=SourceRig(tmp_path/f'{color}-{project_before_blank}')
        try:
            await start_band(r,4)
            r.answers.update(selected_hud='absent',name='other_or_unknown',level='unknown',target_kind='unknown',life_state='unknown')
            await r.observe_target()  # Real unresolved named-HUD work owns inspection.
            r.world.name='';r.world.target_level=None
            base_capture=r.world.capture;present=True;presence_by_frame={}
            def capture():
                frame=base_capture()
                presence_by_frame[frame.frame_id]=present
                if present:
                    with Image.open(frame.image_path) as source:
                        image=source.copy();ImageDraw.Draw(image).rectangle((240,125,380,135),fill=color);image.save(frame.image_path)
                return frame
            r.world.capture=r.c.capture=capture
            if color=='black':
                # Black pixels alone measure unknown. Supply an explicitly
                # attributed zero-health fact to test the dead-bar contract.
                from sage_wow.agent import grind_resources
                original_bars=grind_resources.hud_resources
                def bars(c,frame):
                    result=original_bars(c,frame)
                    if presence_by_frame.get(frame.frame_id):
                        result.update(target_health=0.,target_health_confidence=1.)
                    return result
                monkeypatch.setattr(grind_resources,'hud_resources',bars)
            await r.observe_target()
            episode=r.c.target_inspection_episode
            assert episode['attempts']==2 and not episode.get('source_absence')
            if project_before_blank:
                await r.choose(None)
                assert 'no_selected_frame' not in r.sage.calls[-1]['options']
                assert not r.c.hunt.attempt_absence
            present=False
            result=await r.choose('no_selected_frame')
            assert result.status=='dispatched' and r.c.hunt.selected_presence is False
            assert r.c.target_inspection_episode is episode and episode['attempts']==2
            assert len(r.wire)==2 and not r.physical() and not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())
