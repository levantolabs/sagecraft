"""Ended target errors retain combat debt without replacing the hunting task."""
import asyncio
from copy import deepcopy

import pytest

from test_grind_loot_integration import LootRig
from test_grind_loot_budget import enable,entry,tick
from test_active_attacker_recovery import start,install_hud,smites
from test_grind_committed_combat import install_transient_error


async def ended_error(r,kind='range',text='Out of range',queued=None,late=False,reject_global=False):
    await start(r);enable(r);hud,_=install_hud(r)
    await r.choose('attack_mob_level_1')
    install_transient_error(r,kind,text)
    if queued:
        post=r.c.hunt.pending['receipt']['execution']['post_cast_observations'][0]
        cue={'kind':queued,'text':'retained '+queued}
        if late:
            r.c.hunt.pending['receipt']['execution']['post_cast_observations'].append({**deepcopy(post),'observed_error_cues':[cue]})
        elif reject_global:
            post['observed_error_cues'].insert(0,cue)
            await r.choose('error_not_supported')
        else:post['observed_error_cues'].append(cue)
    await r.choose('position_error' if kind in {'range','facing','los'} else 'cast_error_observed')
    original=r.c.target_proposal
    async def target(frame):
        value=await original(frame)
        return value if r.world.name else {**value,'visual_observation':{'selected_hud':'absent'}}
    r.c.target_proposal=target
    r.world.name='';r.world.error='';hud['target_health']=None
    await r.choose('inspect_recent_corpse')
    await r.loot(None);entry(r)['spent']=30.;await tick(r)
    assert r.c.hunt.encounter_ended and r.c.loot.data['outcome']=='skipped_unverified'
    return hud


@pytest.mark.parametrize('kind,text', [('range','Out of range'),('facing','Target is not in front of you'),
    ('los','Target not in line of sight'),('no_target','No target'),('dead_target','Target is dead'),
    ('resist','Resist'),('evade','Evade')])
def test_target_local_error_then_skipped_loot_returns_to_real_hunting(tmp_path,kind,text):
    async def run():
        r=LootRig(tmp_path)
        try:
            await ended_error(r,kind,text);h=r.c.hunt
            debt=deepcopy((h.cast_error,h.cast_obligation,h.target_history,h.action_failures,h.credited_kills))
            assert not h.cast_blocks_acquisition()
            await r.choose('target_enemy')
            assert set(r.sage.calls[-1]['options'])=={'target_enemy','forward','turn_left','turn_right'}
            assert h.pending['family']=='target' and len(smites(r))==1
            assert (h.cast_error,h.cast_obligation,h.target_history,h.action_failures,h.credited_kills)==debt
        finally:await r.close()
    asyncio.run(run())


def test_real_handoff_keeps_old_range_debt_through_same_name_opener(tmp_path):
    from test_grind_acquisition_opening import enable as opening,process
    async def run():
        r=LootRig(tmp_path)
        try:
            hud=await ended_error(r);h=r.c.hunt;owner=h.combat_history_key
            debt=deepcopy(h.target_history[owner]);opening(r)
            # The fake empty HUD has no target badge while no unit is selected.
            r.world.target_level=None
            await r.choose('target_enemy')
            r.world.name='Young Wolf';r.world.target_level=1;hud['target_health']=1.
            result=await process(r)
            assert result.status=='dispatched' and result.decision is None
            assert len(smites(r))==2 and h.combat_history_key!=owner
            assert h.target_history[owner]['cast_obligation']==debt['cast_obligation']
            assert h.target_history[owner]['cast_error']==debt['cast_error']
            assert not h.cast_obligation and h.cast_error is None and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('queued',['mana','standing','cooldown','interrupted','unavailable_spell','unknown'])
def test_queued_global_constraint_never_becomes_idle_target_error(tmp_path,queued):
    async def run():
        r=LootRig(tmp_path)
        try:
            await ended_error(r,queued=queued);h=r.c.hunt
            before=deepcopy((h.cast_error,h.target_history))
            assert h.cast_blocks_acquisition()
            assert (h.cast_error,h.target_history)==before and len(smites(r))==1
        finally:await r.close()
    asyncio.run(run())


def test_later_postcast_global_cue_is_checked_even_without_feedback_queue(tmp_path):
    async def run():
        r=LootRig(tmp_path)
        try:
            await ended_error(r,queued='mana',late=True);h=r.c.hunt
            h.target_history[h.combat_history_key].pop('unassessed_error_feedback')
            assert h.cast_blocks_acquisition() and len(smites(r))==1
        finally:await r.close()
    asyncio.run(run())


def test_rejected_global_cue_does_not_block_later_ended_local_error(tmp_path):
    async def run():
        r=LootRig(tmp_path)
        try:
            await ended_error(r,queued='mana',reject_global=True);h=r.c.hunt
            assert h.cast_error['assessment_source']['rejected_error_cues']
            assert not h.cast_blocks_acquisition()
            await r.choose('target_enemy')
            assert set(r.sage.calls[-1]['options'])=={'target_enemy','forward','turn_left','turn_right'}
            assert len(smites(r))==1 and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('bad',['ongoing','owner','encounter','receipt','scope','epoch','target','hash','error_hash',
    'partial','unknown_input','obligation','global_error','missing_source','historical_continuity'])
def test_target_error_handoff_requires_attributed_completed_history(tmp_path,bad):
    async def run():
        r=LootRig(tmp_path)
        try:
            await ended_error(r);h=r.c.hunt;s=h.cast_error['assessment_source']
            assert not h.cast_blocks_acquisition()
            if bad=='ongoing':h.encounter_ended=False
            elif bad=='owner':s['target_history_key']='other'
            elif bad=='encounter':h.cast_error['encounter_id']+=1
            elif bad=='receipt':h.cast_error['cast_receipt_id']='other'
            elif bad=='scope':s['source_scope']['source']='other'
            elif bad=='epoch':s['receipt']['session_epoch']='other'
            elif bad=='target':s['target']['name']='Other creature'
            elif bad=='hash':s['source_hash']='other'
            elif bad=='error_hash':s['position_error_evidence']['sha256']='other'
            elif bad=='partial':s['receipt']['completed']=False
            elif bad=='unknown_input':h.input_effect_unverified=True
            elif bad=='obligation':h.cast_obligation='Unreconciled capability state'
            elif bad=='global_error':h.cast_error['kind']='mana'
            elif bad=='missing_source':h.cast_error['assessment_source']=None
            else:s['target_continuity']+=1
            assert h.cast_blocks_acquisition() and len(smites(r))==1 and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())
