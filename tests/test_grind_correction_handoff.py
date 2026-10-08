"""Correction history tolerates level OCR gaps; input permission remains fresh."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_grind_product_spec import Rig
from test_active_attacker_recovery import start, install_hud, smites
from test_grind_ineffective_cast_recovery import ineffective


async def correction(r, kind='range'):
    if kind == 'semantic':
        r.world.target_level = 1
        await ineffective(r)
    else:
        await start(r);install_hud(r)
        r.world.target_level = 1
        await r.choose('attack_mob_level_1')
        r.world.error = 'Out of range'
        await r.choose('position_error');r.world.error = ''
    await r.choose('forward')
    return r.c.hunt.pending


@pytest.mark.parametrize('kind', ['range', 'semantic'])
def test_numeric_gap_in_outcome_then_observer_restoration_reaches_cast(tmp_path, kind):
    async def run():
        r=Rig(tmp_path)
        try:
            await correction(r,kind)
            h=r.c.hunt;owner=h.combat_history_key
            before=len(smites(r));debt=deepcopy(h.action_failures)
            h.target_bands_by_player_level={1:[1,2]}
            r.world.target_level=None
            await r.choose('motion_useful')
            assert h.retry_credit and h.correction_evidence['target_continuity']==h.target_continuity
            proof=deepcopy(h.correction_evidence)
            await r.choose(None)
            assert not any(k.startswith('attack_mob') for k in r.sage.calls[-1]['options'])
            assert len(smites(r))==before and h.correction_evidence['receipt_id']==proof['receipt_id']
            r.world.target_level=1
            r.c.config['smite_burst_count']=3 if kind=='semantic' else 1
            await r.choose('attack_mob_level_1')
            assert len(smites(r))==before+1 and not h.retry_credit
            assert h.action_failures==debt and h.combat_history_key==owner and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['name','level','absence','dead','friendly','self','ambiguous','aba'])
def test_positive_selection_change_invalidates_completed_correction(tmp_path, change):
    async def run():
        r=Rig(tmp_path)
        try:
            await correction(r);await r.choose('motion_useful')
            h=r.c.hunt;target=await r.c.target_proposal(r.world.capture())
            before=len(smites(r))
            if change=='name':target['name']='Different Wolf'
            elif change=='level':target['levels']=[2]
            elif change=='absence':target['visual_observation']['selected_hud']='absent'
            elif change=='dead':target['visual_observation']['life_state']='dead'
            elif change=='friendly':target['visual_observation']['target_kind']='friendly_or_self'
            elif change=='self':target['self_target']=True
            elif change=='ambiguous':target['levels']=[1,2]
            else:
                h.selection_seen({**target,'name':'Different Wolf'})
            h.selection_seen(target)
            assert not h.retry_credit and h.correction_evidence is None
            assert h.cast_obligation and h.cast_error['status']=='active' and len(smites(r))==before
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['generation','epoch','source','size','source_hash','assessment_hash','owner','encounter','new_tab'])
def test_retained_proof_expires_without_erasing_original_constraint(tmp_path, change):
    from sage_wow.agent.grind_correction_handoff import expire, retry_current
    async def run():
        r=Rig(tmp_path)
        try:
            await correction(r);await r.choose('motion_useful')
            h=r.c.hunt;frame=r.world.capture();target=await r.c.target_proposal(frame)
            assert retry_current(r.c,frame,target)
            if change=='generation':r.c.cycle._input_generation+=1
            elif change=='epoch':r.c.cycle.session_epoch='other'
            elif change=='source':frame=replace(frame,source='other')
            elif change=='size':frame=replace(frame,width=501)
            elif change=='source_hash':h.correction_evidence['source_hash']='wrong'
            elif change=='assessment_hash':h.correction_evidence['assessment_hash']='wrong'
            elif change=='owner':h.approach={'history_key':'other'}
            elif change=='encounter':h.encounter+=1
            else:h.target_continuity+=1
            assert not retry_current(r.c,frame,target)
            expire(r.c,frame,target)
            assert not h.retry_credit and h.correction_evidence is None and h.cast_obligation
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['source_hash','continuity','receipt','replaced','consumed'])
def test_stale_useful_answer_does_not_renew_progress_or_methods(tmp_path,change):
    async def run():
        r=Rig(tmp_path)
        try:
            pending=await correction(r);h=r.c.hunt
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            before=h.last_progress_active_at;revision=h.target_history[h.combat_history_key]['method_revision']
            if change=='source_hash':pending['source_hash']='wrong'
            elif change=='continuity':h.target_continuity+=1
            elif change=='receipt':pending['receipt']['completed']=False
            elif change=='replaced':h.pending=deepcopy(pending)
            else:pending['outcome_consumed']=True
            result=SimpleNamespace(receipt={'request_id':'offline'},decision=None)
            await r.c.apply_choice(frame,target,{},pending,True,result,{'effect':'motion_useful','correction':True},'motion_useful',1)
            assert h.last_progress_active_at==before
            assert h.target_history[h.combat_history_key]['method_revision']==revision
            assert not h.retry_credit and h.cast_obligation
            debt=deepcopy(h.unresolved_motion)
            await r.c.apply_choice(frame,target,{},pending,True,result,{'effect':'motion_useful','correction':True},'motion_useful',1)
            assert h.unresolved_motion==debt
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['source_hash','generation','continuity','credit_reset'])
def test_guarded_retry_revalidates_proof_after_provider_answer(tmp_path,change):
    async def run():
        r=Rig(tmp_path)
        try:
            await correction(r);await r.choose('motion_useful')
            before=len(smites(r));h=r.c.hunt
            async def alter():
                if change=='source_hash':h.correction_evidence['source_hash']='wrong'
                elif change=='generation':r.c.cycle._input_generation+=1
                elif change=='continuity':h.target_continuity+=1
                else:h.retry_credit=False
            r.sage.hook=alter
            result=await r.choose('attack_mob_level_1')
            assert result.status!='dispatched' and len(smites(r))==before
        finally:await r.close()
    asyncio.run(run())


def test_typed_finisher_retains_offered_correction_requirement(tmp_path, monkeypatch):
    from sage_wow.agent import grind_resources
    async def run():
        r=Rig(tmp_path)
        try:
            await correction(r);await r.choose('motion_useful')
            hud,_=install_hud(r)
            hud.update(player_health=.29,target_health=.2)
            monkeypatch.setattr(grind_resources,'hud_resources',lambda c,f:{'frame_id':f.frame_id,**hud})
            r.c.config.update(encounter_resources=True,preserve_target_heal=True)
            before=len(smites(r))
            async def reset():r.c.hunt.retry_credit=False
            r.sage.hook=reset
            result=await r.choose('finish_fight')
            assert result.status=='precondition_failed' and len(smites(r))==before
            assert r.c.hunt.cast_obligation and r.c.hunt.cast_error['status']=='active'
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interruption',['credit_reset','focus'])
def test_returned_target_correction_restores_its_own_error(tmp_path,interruption):
    async def run():
        r=Rig(tmp_path)
        try:
            await start(r);install_hud(r)
            r.world.target_level=1
            await r.choose('attack_mob_level_1')
            r.world.error='Out of range';await r.choose('position_error')
            h=r.c.hunt;a=h.combat_history_key;a_cast=h.cast_error['cast_receipt_id']
            r.world.error='';r.world.name='Other Creature'
            await r.choose('attack_mob_level_1')
            r.world.error='Target not in line of sight';await r.choose('position_error')
            b=h.combat_history_key;h.remember_approach();b_state=deepcopy(h.target_history[b])
            r.world.error='';r.world.name='Young Wolf'
            await r.choose(None)
            await r.choose('turn_left');await r.choose('motion_useful')
            assert h.combat_history_key==a and h.retry_credit
            assert h.retry_credit_source['cast_receipt_id']==a_cast
            if interruption=='focus':r.c.pause_focus()
            else:h.retry_credit=False
            assert not h.retry_credit and h.cast_obligation
            assert h.cast_error['cast_receipt_id']==a_cast and h.cast_error['status']=='active'
            assert h.target_history[b]==b_state
        finally:await r.close()
    asyncio.run(run())


def test_second_error_on_same_cast_needs_its_own_correction(tmp_path):
    from test_grind_committed_combat import install_transient_error
    async def run():
        r=Rig(tmp_path)
        try:
            await start(r);install_hud(r)
            await r.choose('attack_mob_level_1')
            install_transient_error(r,'range','Out of range')
            h=r.c.hunt
            post=h.pending['receipt']['execution']['post_cast_observations'][0]
            post['observed_error_cues'].append({'kind':'facing','text':'Target is not in front of you'})
            await r.choose('position_error');await r.choose('forward');await r.choose('motion_useful')
            first=h.correction_evidence['receipt_id']
            await r.choose('retain_cast_error')
            await r.choose(None)
            assert h.cast_error['kind']=='facing' and h.cast_error['status']=='active'
            assert h.cast_obligation and not h.retry_credit
            assert 'attack_mob_level_1' not in r.sage.calls[-1]['options']
            await r.choose('turn_left');await r.choose('motion_useful')
            assert h.retry_credit and h.correction_evidence['receipt_id']!=first
            await r.choose('attack_mob_level_1')
            assert len(smites(r))==2 and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())
