"""Natural sensing/clear ownership; every provider and motor is offline fake."""
import asyncio

import pytest

from test_grind_inspection_source_handoff import SourceRig
from test_grind_interrupted_sensing import interrupted, positive
from test_grind_selected_task_exit import disposed, exhaust, counters, readable_world
from test_grind_target_inspection_contract import ContractRig
from test_grind_opportunistic_targets import rig as policy_rig
from test_grind_travel_recovery import events
from sage_wow.agent import grind_inspection as inspection, grind_relocation as relocation
from sage_wow.perception.ocr import TextObservation


async def renewed_unknown(r, monkeypatch, *, overlay=False):
    old,owner,history,_,_=await interrupted(r,monkeypatch)
    await r.choose('no_selected_frame')
    r.world.scenery_patches=[((420,170,450,200),'#986743')]
    if overlay:r.world.scenery_patches.append(((200,215,490,240),'#938273'))
    tab=await r.choose('target_enemy')
    assert tab.status=='dispatched'
    assert tab.receipt['source_frame_id']==tab.receipt['dispatch_frame_id']
    assert r.c.hunt.pending['sensing_boundary_witness']['receipt_id']==tab.receipt['receipt_id']
    await positive(r,numeral='unknown')
    assert r.c.target_inspection_episode['id']!=old['id']
    await r.observe_target()
    assert r.c.target_inspection_episode['attempts']==2
    assert r.c.hunt.target_history[owner]==history
    await r.choose('change_search_strategy');await r.choose('explore_visible');await exhaust(r)
    await r.choose('reject_selected_target')
    pending=r.c.hunt.pending;intent=r.c.hunt.strategy_required['selected_exit']
    assert pending['purpose']=='selected_task_abandon'
    r.world.name='';r.world.target_level=None;r.world.scenery_patches=[]
    r.answers.update(selected_hud='absent',name='other_or_unknown',level='unknown',target_kind='unknown',life_state='unknown')
    return pending,intent,owner,history


@pytest.mark.parametrize('first',[None,'cannot_assess'])
@pytest.mark.parametrize('overlay',[False,True])
def test_changed_empty_scenery_tab_then_clear_retains_owner_until_visible_answer(tmp_path,monkeypatch,first,overlay):
    async def run():
        r=SourceRig(tmp_path/'renewed')
        try:
            pending,intent,owner,history=await renewed_unknown(r,monkeypatch,overlay=overlay)
            before=counters(r.c.hunt)
            await r.choose(first)
            assert r.c.hunt.pending is pending and not pending['outcome_consumed']
            assert counters(r.c.hunt)==before
            await r.choose('target_cleared')
            assert intent['disposition']=='closed' and relocation.typed_release(r.c.hunt)
            assert r.c.hunt.pending is None and counters(r.c.hunt)==before
            assert r.c.hunt.target_history[owner]==history and not r.c.hunt.credited_kills
            assert len(r.events('grind_selected_clear_assessment_admitted'))==2
            assert r.events('grind_target_inspection_clear_absence')[-1]['observer_attempts_granted']==0
            question=r.sage.calls[-1]
            assert question['instructions']=='Is a selected target portrait with an attached health bar visible now?'
            assert 'task' not in question['options']['target_cleared'].description
        finally:await r.close()
    asyncio.run(run())


def test_two_unclear_clear_answers_charge_once_and_reach_existing_recovery(tmp_path,monkeypatch):
    async def run():
        r=SourceRig(tmp_path/'unknown')
        try:
            readable_world(r)
            pending,intent,owner,history=await renewed_unknown(r,monkeypatch)
            before=r.c.hunt.failures['clear']
            await r.choose(None);await r.choose('cannot_assess')
            assert r.c.hunt.pending is None and pending['outcome_consumed']
            assert intent['disposition']=='assessment_exhausted' and not relocation.typed_release(r.c.hunt)
            assert r.c.hunt.failures['clear']==before+1
            assert len(intent['clear_assessments'][pending['receipt']['receipt_id']]['request_ids'])==2
            calls=len(r.sage.calls)
            result=await r.choose('target_enemy')
            assert len(r.sage.calls)>calls and result.status=='dispatched'
            assert r.c.hunt.pending['family']=='target'
            assert 'sensing_boundary_witness' not in r.c.hunt.pending
            assert result.detail!='Current absence remains unknown; boundary question allowance spent'
            assert r.c.hunt.failures['clear']==before+1
            assert r.c.hunt.target_history[owner]==history and not r.c.hunt.credited_kills
            # A guarded travel Tab supplies no new observer allowance. Its
            # unresolved actual selection can still leave through the same
            # existing strategy/clear route, with an independent real receipt.
            r.world.name='Rockjaw Trogg';r.world.target_level=1
            r.answers.update(selected_hud='present',name='other_or_unknown',level='unknown',target_kind='creature',life_state='alive')
            await r.choose('change_search_strategy');await r.choose('explore_visible');await exhaust(r)
            await r.choose('reject_selected_target')
            second=r.c.hunt.pending;second_intent=r.c.hunt.strategy_required['selected_exit']
            assert second['receipt']['receipt_id']!=pending['receipt']['receipt_id']
            r.world.name='';r.world.target_level=None
            await r.choose(None);await r.choose('cannot_assess')
            assert second_intent['disposition']=='assessment_exhausted' and r.c.hunt.pending is None
            assert r.c.hunt.failures['clear']==before+2
            assert len(r.events('grind_selected_clear_assessment_admitted'))==4
            assert not relocation.typed_release(r.c.hunt)
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['receipt','source','marker'])
def test_clear_assessment_await_cannot_consume_mutated_pending(tmp_path,monkeypatch,change):
    async def run():
        r=ContractRig(tmp_path/change)
        try:
            values,intent=await disposed(r,monkeypatch);await exhaust(r);await r.choose('reject_selected_target')
            pending=r.c.hunt.pending;r.world.name='';r.world.target_level=None;values['target_health']=None
            async def mutate():
                if change=='receipt':pending['receipt']={**pending['receipt'],'receipt_id':'replacement'}
                elif change=='source':pending['source_hash']='replacement'
                else:pending['selected_task_abandon']={**pending['selected_task_abandon'],'episode_id':'replacement'}
            r.sage.hook=mutate
            await r.choose('target_cleared')
            assert r.c.hunt.pending is pending and not pending['outcome_consumed']
            assert intent['disposition']=='handed_off' and not relocation.typed_release(r.c.hunt)
            assert not r.events('grind_selected_task_closed') and not r.c.hunt.credited_kills
            r.sage.hook=None;calls=len(r.sage.calls)
            await r.choose(None);r.sage.answers.clear()
            assert len(r.sage.calls)==calls and r.c.hunt.pending is pending
            assert not pending['outcome_consumed'] and r.c.hunt.failures['clear']==0
            assert intent['disposition']=='assessment_exhausted'
        finally:await r.close()
    asyncio.run(run())


def test_second_clear_response_invalidated_then_cap_disposes_original_once(tmp_path,monkeypatch):
    async def run():
        r=ContractRig(tmp_path/'invalidated-cap')
        try:
            values,intent=await disposed(r,monkeypatch);await exhaust(r);await r.choose('reject_selected_target')
            pending=r.c.hunt.pending;r.world.name='';r.world.target_level=None;values['target_health']=None
            await r.choose(None)
            async def invalidate():r.c.revision+=1
            r.sage.hook=invalidate
            await r.choose('target_cleared')
            r.sage.hook=None
            assert r.c.hunt.pending is pending and not pending['outcome_consumed']
            assert intent['disposition']=='handed_off' and not relocation.typed_release(r.c.hunt)
            assert len(r.events('grind_selected_clear_assessment_admitted'))==2
            calls=len(r.sage.calls)
            await r.choose(None);r.sage.answers.clear()
            assert len(r.sage.calls)==calls and r.c.hunt.pending is None
            assert pending['outcome_consumed'] and r.c.hunt.failures['clear']==1
            assert intent['disposition']=='assessment_exhausted' and not relocation.typed_release(r.c.hunt)
            assert intent['clear']['receipt']==pending['receipt']
            assert not r.events('grind_selected_task_closed')
            await r.choose(None)
            assert r.c.hunt.failures['clear']==1
            assert len(r.events('grind_selected_clear_assessment_admitted'))==2
        finally:await r.close()
    asyncio.run(run())


def test_unsafe_clear_assessment_disposes_original_without_absence(tmp_path,monkeypatch):
    async def run():
        r=ContractRig(tmp_path/'unsafe-clear')
        try:
            values,intent=await disposed(r,monkeypatch);await exhaust(r);await r.choose('reject_selected_target')
            pending=r.c.hunt.pending;await r.choose(None)
            r.world.name='';r.world.target_level=None;values['target_health']=None
            r.c.hunt.no_mana=True
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            result=await relocation.decide_selected_exit(r.c,frame,target,{})
            assert result.status=='grind_reobserve' and r.c.hunt.pending is None
            assert pending['outcome_consumed'] and r.c.hunt.failures['clear']==1
            assert intent['disposition']=='assessment_exhausted' and not relocation.typed_release(r.c.hunt)
            assert not r.events('grind_selected_task_closed') and r.c.hunt.no_mana
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['positive_name','positive_number','live_bar','dead_bar','stale','generation','hash'])
def test_changed_blank_scenery_never_overrides_current_witness_veto(tmp_path,monkeypatch,fault):
    async def run():
        r=SourceRig(tmp_path/fault)
        try:
            await interrupted(r,monkeypatch);await r.choose('no_selected_frame')
            r.world.scenery_patches=[((420,170,450,200),'#986743')]
            frame=r.world.capture();target=await r.c.raw_target_proposal(frame)
            fact=inspection._fact(r.c.target_inspection_episode)
            if fault=='positive_name':target['name']='Rockjaw Trogg'
            elif fault=='positive_number':target['levels']=[1]
            elif fault in {'live_bar','dead_bar'}:
                original=__import__('sage_wow.agent.grind_resources',fromlist=['hud_resources']).hud_resources
                monkeypatch.setattr('sage_wow.agent.grind_resources.hud_resources',lambda c,f:
                    {**original(c,f),'target_health':1. if fault=='live_bar' else 0.,'target_health_confidence':1.})
            elif fault=='stale':fact['captured_at']='2020-01-01T00:00:00+00:00'
            elif fault=='generation':r.c.cycle._input_generation+=1
            else:fact['source_sha256']='changed'
            assert inspection.prepare_witness(r.c,frame,target) is None
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('caption',['Coldridge Valley,','Coldridge Valley.','Elsewhere'])
def test_actual_dispatch_caption_cleanup_retains_real_zone_conflicts(tmp_path,caption):
    async def run():
        r=await policy_rig(tmp_path,4)
        try:
            original=r.c.ocr;changed=False
            def ocr(path):
                rows=original(path)
                if changed:
                    rows += [TextObservation(caption,item.confidence,item.bounds)
                        for item in list(rows) if item.text=='Coldridge Valley']
                return rows
            r.c.ocr=ocr
            async def alter():
                nonlocal changed
                changed=True
            r.sage.hook=alter
            result=await r.choose('attack_mob_level_1')
            if caption=='Elsewhere':
                assert result.status=='dispatch_guard_rejected' and not r.casts()
                assert events(r,'dispatch_guard_checked')[-1]['evidence']['failed_predicates']==['hunting_strategy']
                assert 'hunting_strategy:ground_unknown' in result.detail
            else:assert result.status=='dispatched' and r.casts()
        finally:await r.close()
    asyncio.run(run())
