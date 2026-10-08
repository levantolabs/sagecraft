"""Live-run clear continuity and fresh eligible reacquisition, offline only."""
import asyncio
from copy import deepcopy

import pytest

from test_grind_product_spec import Rig
from test_grind_committed_combat import confirmed_target


@pytest.mark.parametrize('name,levels,approved',[
    ('Young Wolf',[],True),('Young Wolf®',[],True),
    ('Different Wolf',[],False),('Young Wolf',[2],False)])
def test_clear_guard_handles_missing_ocr_level_and_decoration_but_rejects_conflicts(tmp_path,name,levels,approved):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();original=r.c.target_proposal;state={'fresh':False}
            async def target(frame):
                raw=await original(frame)
                return {**raw,'name':name if state['fresh'] else 'Young Wolf',
                    'levels':levels if state['fresh'] else [1]}
            r.c.target_proposal=target
            async def next_view():state['fresh']=True
            r.sage.hook=next_view
            result=await r.choose('reject_selected_target')
            assert result.status==('dispatched' if approved else 'dispatch_guard_rejected')
            assert (r.controls['escape']['keycode'] in r.physical_keys())==approved
            assert not r.casts()
        finally:await r.close()
    asyncio.run(run())


async def rejected_then_reacquired(r,*,tab=True,observation=True):
    await r.start();r.c.config['committed_combat']=True
    await r.choose('reject_selected_target')
    h=r.c.hunt;history=h.target_history[h.approach['history_key']]
    assert history['rejections']==1
    r.world.name='';await r.choose('target_cleared')
    if tab:await r.choose('target_enemy')
    r.world.name='Young Wolf'
    original=r.c.target_proposal
    async def target(frame):
        raw=await original(frame)
        return confirmed_target(raw) if observation and raw.get('name') else raw
    r.c.target_proposal=target
    return history


def test_fresh_linked_tab_reassesses_rejection_without_new_life_or_erasing_history(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            history=await rejected_then_reacquired(r);h=r.c.hunt
            key=history['key'];prior_outcomes=deepcopy(h.outcomes)
            prior_lives=dict(h.target_lives);tab=h.pending['receipt']['receipt_id']
            await r.choose('attack_mob_level_1')
            assert h.approach['history_key']==key and h.target_history[key] is history
            assert h.target_lives==prior_lives and history['rejections']==1
            assert history['rejection_reassessment']['target_receipt_id']==tab
            assert h.outcomes[:len(prior_outcomes)]==prior_outcomes
            assert not h.credited_kills
            # A subsequent explicit rejection supersedes this reconsideration.
            await r.choose('damaged_alive');await r.choose('reject_selected_target')
            assert history['rejections']==2 and history['rejection_reassessment']['rejections']==1
            r.world.name='';await r.choose('target_cleared')
            r.world.name='Young Wolf';await r.choose(None)
            assert not any(name.startswith('attack_mob') for name in r.sage.calls[-1]['options'])
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('condition',['no_tab','stale_observation','partial_tab','input_uncertain','combat_debt','active_cast_error','exhausted_corrections'])
def test_reacquisition_retains_history_and_requires_fresh_input_authority(tmp_path,condition):
    async def run():
        r=Rig(tmp_path)
        try:
            history=await rejected_then_reacquired(r,tab=condition!='no_tab',observation=condition!='stale_observation')
            h=r.c.hunt
            if condition=='partial_tab':h.pending['receipt'].update(completed=False,dispatch_unknown=True)
            elif condition=='input_uncertain':h.input_effect_unverified=True
            elif condition=='combat_debt':history['combat_failures']=2
            elif condition=='exhausted_corrections':history['correction_rounds']=3
            elif condition=='active_cast_error':
                history['cast_obligation']='Current linked range error requires observed correction'
                h.cast_error={'kind':'range','status':'active','encounter_id':h.encounter}
            if condition=='input_uncertain':
                # General input-integrity recovery owns this state before any
                # combat menu; it must not dispatch an attack or retire debt.
                r.sage.answers.append(None);r.c.wait_until=0
                await r.c.process(r.world.capture())
            else:
                await r.choose(None)
                # The revised product contract permits one Sage-selected probe
                # after verified acquisition. Old gameplay failures stay logged
                # but do not prove the newly selected unit remains unreachable.
                probe=condition in {'combat_debt','active_cast_error','exhausted_corrections'}
                assert any(name.startswith('attack_mob') for name in r.sage.calls[-1]['options'])==probe
            assert 'rejection_reassessment' not in history and history['rejections']==1
            assert not r.casts() and not h.credited_kills
            if condition=='combat_debt':assert history['combat_failures']==2
            if condition=='exhausted_corrections':assert history['correction_rounds']==3
            if condition=='active_cast_error':assert history['cast_obligation'] and h.cast_error['status']=='active'
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['focus','generation','later_rejection','source_hash',
    'tab_family','tab_action','tab_possible_input','tab_epoch','tab_generation','tab_completed_at'])
def test_offered_reassessment_rechecks_authority_before_offensive_dispatch(tmp_path,change):
    async def run():
        r=Rig(tmp_path)
        try:
            history=await rejected_then_reacquired(r);before=list(r.backend.events)
            async def changed():
                if change=='focus':r.c.pause_focus()
                elif change=='generation':r.c.cycle._input_generation+=1
                elif change=='later_rejection':history['rejections']+=1
                elif change=='tab_family':r.c.hunt.pending['family']='combat'
                elif change=='tab_action':r.c.hunt.pending['action']='forward'
                elif change=='tab_possible_input':r.c.hunt.pending['receipt']['possible_input']=False
                elif change=='tab_epoch':r.c.hunt.pending['receipt']['session_epoch']='retired'
                elif change=='tab_generation':r.c.hunt.pending['receipt']['generation_after']+=1
                elif change=='tab_completed_at':
                    from datetime import datetime,timedelta,timezone
                    r.c.hunt.pending['receipt']['occurred_at']=(datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat()
                else:
                    # Mutate the exact captured request source, not the
                    # separately composed image shown to the fake provider.
                    source=r.world.directory/f'acceptance-{r.world.count}.png'
                    source.write_bytes(b'changed request source')
            r.sage.hook=changed
            result=await r.choose('attack_mob_level_1')
            assert 'attack_mob_level_1' in r.sage.calls[-1]['options']
            assert result.status!='dispatched' and r.backend.events==before
            assert 'rejection_reassessment' not in history
            assert not r.casts() and not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_clear_source_mutated_during_fresh_analysis_is_rejected(tmp_path):
    async def run():
        from pathlib import Path
        from PIL import Image
        r=Rig(tmp_path)
        try:
            await r.start();original=r.c.rows;state={}
            async def source_identified():
                state['source']=r.world.directory/f'acceptance-{r.world.count}.png'
            async def fresh_analysis(frame):
                rows=await original(frame)
                if state.get('source') and Path(frame.image_path)!=state['source']:
                    with Image.open(state.pop('source')) as image:
                        changed=image.copy();changed.putpixel((490,290),(255,0,0));changed.save(image.filename)
                return rows
            r.c.rows=fresh_analysis;r.sage.hook=source_identified
            result=await r.choose('reject_selected_target')
            assert result.status=='dispatch_guard_rejected'
            assert r.controls['escape']['keycode'] not in r.physical_keys()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['tab_generation','later_rejection'])
def test_reassessment_is_revalidated_after_fresh_offensive_guard_analysis(tmp_path,change):
    async def run():
        r=Rig(tmp_path)
        try:
            history=await rejected_then_reacquired(r);original=r.c.rows;armed=[False]
            before=list(r.backend.events)
            async def source_accepted():armed[0]=True
            async def fresh_analysis(frame):
                rows=await original(frame)
                if armed[0]:
                    armed[0]=False
                    if change=='tab_generation':r.c.hunt.pending['receipt']['generation_after']+=1
                    else:history['rejections']+=1
                return rows
            r.sage.hook=source_accepted;r.c.rows=fresh_analysis
            result=await r.choose('attack_mob_level_1')
            assert result.status!='dispatched' and r.backend.events==before
            assert 'rejection_reassessment' not in history
        finally:await r.close()
    asyncio.run(run())
