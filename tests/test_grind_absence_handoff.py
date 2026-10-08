"""Current accepted absence closes only its original encounter; all IO is fake."""
import asyncio
from copy import deepcopy

import pytest

from test_grind_opportunistic_targets import rig, snapshot
from test_grind_committed_combat import install_transient_error


async def facing(r, *, absent=True):
    r.world.target_level=3
    await r.choose('attack_mob_level_3')
    if absent:r.world.name='';r.world.target_level=None
    install_transient_error(r,'facing','Target is not in front of you')
    await r.choose('position_error')
    assert r.c.hunt.pending is None and not r.c.hunt.encounter_ended


def debt(h):
    history=deepcopy(h.target_history[h.combat_history_key])
    history.pop('selection_loss',None);history.pop('encounter_ended',None)
    return deepcopy((h.cast_obligation,h.cast_error,h.failures,h.action_failures,
        h.correction_rounds,h.unresolved_motion,h.retry_credit,h.recent_combat,history))


def test_natural_absent_facing_owner_reaches_guarded_hunting_without_progress_or_refund(tmp_path):
    async def run():
        r=await rig(tmp_path)
        try:
            await facing(r);h=r.c.hunt;before=debt(h);owner=h.combat_history_key
            progress=deepcopy(h.progress_facts);keys=len(r.physical_keys())
            await r.choose('no_selected_frame')
            assert h.encounter_ended and h.terminal_reason=='lost_unknown'
            assert not h.cast_blocks_acquisition() and debt(h)==before
            assert h.combat_history_key==owner and len(r.physical_keys())==keys
            assert h.progress_facts==progress and not h.target_dead_observed and not h.credited_kills
            fact=h.target_history[owner]['selection_loss']
            assert fact['assessment']==r.c.cycle.last_receipt and not fact['assessment']['possible_input']
            assert fact['combat_receipt_id']==h.recent_combat['receipt']['receipt_id']
            await r.choose(None)
            assert set(r.sage.calls[-1]['options'])=={'target_enemy','forward','turn_left','turn_right'}
            await r.choose('target_enemy')
            assert h.pending['family']=='target' and debt(h)==before
            assert not r.c.navigation_wait
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['owner','receipt','source','epoch','generation','config','world','threat','heal','pending'])
def test_response_cannot_close_changed_owner_or_bypass_stronger_work(tmp_path,fault):
    async def run():
        r=await rig(tmp_path)
        try:
            await facing(r);h=r.c.hunt;owner=h.combat_history_key
            async def change():
                if fault=='owner':h.combat_history_key='different-owner'
                elif fault=='receipt':h.recent_combat['receipt']['receipt_id']='different-receipt'
                elif fault=='source':h.recent_combat['source_hash']='changed-source'
                elif fault=='epoch':r.c.cycle.session_epoch='different-epoch'
                elif fault=='generation':r.c.cycle._input_generation+=1
                elif fault=='config':r.c.config['move_seconds']+=.01
                elif fault=='world':r.c.require_world=True
                elif fault=='threat':h.active_threat={'kind':'current_incoming_attack'}
                elif fault=='heal':r.c.heal_pending={'offline_current_recovery':True}
                else:h.pending={'family':'combat','receipt':{'receipt_id':'replacement','completed':False}}
            r.sage.hook=change
            await r.choose('no_selected_frame')
            assert not h.encounter_ended and 'selection_loss' not in h.target_history[owner]
            assert not h.credited_kills and not h.target_dead_observed
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('answer',[None,RuntimeError('offline failure')])
def test_absence_without_accepted_answer_cannot_close(tmp_path,answer):
    async def run():
        r=await rig(tmp_path)
        try:
            await facing(r);h=r.c.hunt
            await r.choose(answer)
            assert not h.encounter_ended and not h.target_history[h.combat_history_key].get('selection_loss')
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('cue',['name','level','bar','visual'])
def test_current_positive_selection_vetoes_absence_handoff(tmp_path,cue):
    async def run():
        r=await rig(tmp_path)
        try:
            await facing(r);original=r.c.target_proposal
            async def conflict(frame):
                target=await original(frame)
                if cue=='name':target['name']='Ragged Young Wolf'
                elif cue=='level':target['levels']=[3]
                elif cue=='bar':target['hud']={'frame_id':frame.frame_id,'target_health':.5,'target_health_confidence':1}
                else:target['visual_observation']['selected_hud']='present'
                return target
            r.c.target_proposal=conflict
            await r.choose(None)
            h=r.c.hunt
            assert not h.encounter_ended and not h.target_history[h.combat_history_key].get('selection_loss')
        finally:await r.close()
    asyncio.run(run())


def test_same_name_reappearance_and_real_view_change_retain_spent_correction_methods(tmp_path):
    async def run():
        r=await rig(tmp_path)
        try:
            await facing(r,absent=False);h=r.c.hunt
            for _ in range(2):
                await r.choose('turn_left');await r.choose('motion_no_useful_effect')
            owner=h.combat_history_key;before=deepcopy(h.action_failures)
            r.world.name='';r.world.target_level=None
            await r.choose('no_selected_frame')
            assert h.encounter_ended
            await r.choose('turn_right')
            r.world.name='Ragged Young Wolf';r.world.target_level=3
            await r.choose(None)
            assert h.combat_history_key==owner and h.action_failures==before
            assert not h.pixel_renewal_allowed({'name':r.world.name})
            assert 'turn_left' not in r.sage.calls[-1]['options']
            assert 'attack_mob_level_3' not in r.sage.calls[-1]['options']
            assert h.cast_error['status']=='active' and not h.retry_credit
        finally:await r.close()
    asyncio.run(run())


def test_real_focus_archive_then_absence_does_not_erase_unknown_on_fresh_same_name(tmp_path):
    async def run():
        r=await rig(tmp_path)
        try:
            r.world.target_level=3;await r.choose('attack_mob_level_3')
            h=r.c.hunt;owner=h.combat_history_key
            r.c.pause_focus();assert r.c.resume_focus()
            r.world.name='';r.world.target_level=None
            await r.choose('world_normal_confirmed');await r.choose('no_selected_frame')
            assert h.encounter_ended and h.cast_obligation
            before=h.cast_obligation
            r.world.name='Ragged Young Wolf';r.world.target_level=3
            frame,target,_=await snapshot(r)
            assert not h.confirm_living_target(target,frame)
            assert h.cast_obligation==before and h.combat_history_key==owner
        finally:await r.close()
    asyncio.run(run())


def test_nonlocal_player_constraint_is_not_resolved_by_absence(tmp_path):
    async def run():
        r=await rig(tmp_path)
        try:
            r.world.target_level=3;await r.choose('attack_mob_level_3')
            r.world.name='';r.world.target_level=None
            install_transient_error(r,'standing','You must be standing')
            await r.choose('cast_error_observed')
            h=r.c.hunt;before=debt(h)
            await r.choose('no_selected_frame')
            assert h.encounter_ended and h.cast_blocks_acquisition() and debt(h)==before
            assert h.cast_error['kind']=='standing' and h.cast_error['status']=='active'
        finally:await r.close()
    asyncio.run(run())
