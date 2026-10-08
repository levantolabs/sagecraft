"""Exhausted encounters have concrete exits; accepted relocation is task intent."""
import asyncio
from copy import deepcopy

import pytest

from test_grind_product_spec import Rig
from test_active_attacker_recovery import start, install_hud, smites, native_fact


async def exhausted(r, *, retired=False):
    await start(r);install_hud(r);r.world.target_level=1
    await r.choose('attack_mob_level_1')
    r.world.error='Out of range';await r.choose('position_error');r.world.error=''
    if retired:
        await r.choose('forward');await r.choose('motion_useful')
    h=r.c.hunt;h.correction_rounds=3;h.remember_approach()
    return h


@pytest.mark.parametrize('retired',[False,True])
def test_exhausted_attempt_offers_clear_or_relocation_not_pointless_corrections(tmp_path,retired):
    async def run():
        r=Rig(tmp_path)
        try:
            h=await exhausted(r,retired=retired)
            debt=deepcopy(h.action_failures);history=deepcopy(h.target_history)
            await r.choose(None);call=r.sage.calls[-1]
            assert set(call['options'])=={'reject_selected_target','change_search_strategy'}
            assert 'limit of 3 correction rounds' in call['prompt']
            assert 'does not mean this creature is dead or ineligible' in call['prompt']
            assert 'Clear this unresolved target' in call['instructions']
            await r.choose('reject_selected_target')
            assert h.pending['family']=='clear' and h.action_failures==debt
            assert h.target_history[h.combat_history_key]['correction_rounds']==3
            assert len(smites(r))==1 and not h.credited_kills
            r.world.name='';r.world.target_level=None
            original=r.c.target_proposal
            async def absent(frame):
                t=await original(frame)
                return {**t,'hud':{**t['hud'],'target_health':None},'eligibility':'absent',
                    'visual_observation':{'selected_hud':'absent','life_state':'unknown','target_kind':'unknown'},
                    'visual_provenance':{'continuity_frame_id':frame.frame_id}}
            r.c.target_proposal=absent
            await r.choose('target_cleared')
            assert h.selected_presence is False and h.pending is None
        finally:await r.close()
    asyncio.run(run())


async def relocation(r):
    h=await exhausted(r)
    await r.choose('change_search_strategy');intent=deepcopy(h.strategy_required['selected_exit'])
    await r.choose('explore_visible')
    assert h.plan['relocation_request_id']==intent['request_id']
    return h,intent


def test_ended_unknown_encounter_with_cast_constraint_can_leave_selected_work(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            h=await exhausted(r)
            h.encounter_ended=True
            h.remember_approach()
            debt=deepcopy(h.target_history);failures=deepcopy(h.action_failures)
            assert h.cast_obligation
            await r.choose('change_search_strategy')
            intent=deepcopy(h.strategy_required['selected_exit'])
            await r.choose('explore_visible')
            assert h.plan['relocation_request_id']==intent['request_id']
            assert 'change_search_strategy' not in r.sage.calls[-1]['options']
            await r.choose(None)
            await r.choose('detour_backward')
            assert h.pending['purpose']=='travel'
            assert h.target_history==debt and h.action_failures==failures
            assert len(smites(r))==1 and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_relocation_survives_unchanged_move_provider_nulls_and_numeric_refresh(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            h,intent=await relocation(r)
            await r.choose('detour_backward')
            cast_debt=deepcopy(h.target_history);failures=deepcopy(h.action_failures)
            progress=h.search_revision;before=len(smites(r))
            for level in (None,1):
                r.world.target_level=level
                await r.choose(None)
            assert h.compact_stage=='recovery'
            r.world.target_level=1
            await r.choose('detour_forward_left')
            assert 'Current task: Reach the selected hunting area.' in r.sage.calls[-1]['prompt']
            assert 'You chose to leave' in r.sage.calls[-1]['prompt']
            assert h.pending['purpose']=='travel' and h.pending['action']=='forward_left'
            requests=[e['payload'] for e in r.store.recent(100) if e['event_type']=='sage_request_started']
            assert requests[-1]['reasoning_mode']=='auto'
            assert h.strategy_required['selected_exit']==intent
            assert h.target_history==cast_debt and h.action_failures==failures
            assert h.search_revision==progress and len(smites(r))==before and not h.credited_kills
            assert any(e['event_type']=='grind_relocation_resumed' for e in r.store.recent(100))
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['epoch','owner','continuity','strategy','threat','healing','mana','pending','blocked','input','phase','reason','selected_absent'])
def test_relocation_resume_does_not_override_scope_or_current_obligations(tmp_path,change):
    from sage_wow.agent.grind_relocation import resume
    async def run():
        r=Rig(tmp_path)
        try:
            h,intent=await relocation(r)
            r.c.request_recovery('repeated_null_provider_answer')
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            if change=='epoch':intent['session_epoch']='other';h.strategy_required['selected_exit']=intent
            elif change=='owner':h.combat_history_key='other'
            elif change=='continuity':h.target_continuity+=1
            elif change=='strategy':h.plan['relocation_request_id']='other'
            elif change=='threat':h.active_threat={'current':True}
            elif change=='healing':r.c.heal_pending={'current':True}
            elif change=='mana':h.no_mana=True
            elif change=='pending':h.pending={'family':'clear'}
            elif change=='blocked':h.blocked={'reason':'blocked_timing'}
            elif change=='input':h.input_effect_unverified=True
            elif change=='phase':h.phase='recover'
            elif change=='reason':r.c.request_recovery('actual_survival_emergency')
            else:target['visual_observation']['selected_hud']='absent'
            assert not resume(r.c,frame,target,h.pending,h.phase)
            assert h.compact_stage=='recovery' and len(smites(r))==1
        finally:await r.close()
    asyncio.run(run())


def test_actual_attacker_still_interrupts_relocating_recovery(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            h,_=await relocation(r)
            r.c.request_recovery('repeated_null_provider_answer')
            r.c.combat_log_facts=[native_fact(event='SWING_DAMAGE',own=False)]
            await r.choose(None)
            assert h.phase!='travel' and h.active_threat
            assert 'detour_backward' not in r.sage.calls[-1]['options']
            assert 'disengage_threat' in r.sage.calls[-1]['options']
        finally:await r.close()
    asyncio.run(run())


def test_exhausted_clears_create_intent_only_on_accepted_destination(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            h=await exhausted(r)
            for _ in range(2):
                await r.choose('reject_selected_target');await r.choose('clear_failed')
            before=list(r.physical_keys());debt=deepcopy(h.action_failures)
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_reobserve' and h.planning_requested
            assert not h.strategy_required.get('selected_exit') and r.physical_keys()==before
            await r.choose('explore_visible')
            assert h.plan['relocation_request_id']==h.strategy_required['selected_exit']['request_id']
            # The first recovery page offers the bounded backward action.
            await r.choose(None)
            await r.choose('detour_backward')
            assert h.pending['purpose']=='travel' and h.action_failures==debt
            assert len(smites(r))==1 and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('healing',['unavailable','exhausted','disabled'])
def test_critical_health_with_unavailable_healing_does_not_resume_travel(tmp_path,monkeypatch,healing):
    from sage_wow.agent import grind_resources
    async def run():
        r=Rig(tmp_path)
        try:
            h,_=await relocation(r)
            hud,_=install_hud(r);hud['player_health']=.1
            monkeypatch.setattr(grind_resources,'hud_resources',lambda c,f:{'frame_id':f.frame_id,**hud})
            r.c.config.update(encounter_resources=True,preserve_target_heal=True)
            if healing=='unavailable':h.heal_unavailable=True
            elif healing=='exhausted':h.failures['recovery']=2
            else:r.c.config['preserve_target_heal']=False
            r.c.request_recovery('repeated_null_provider_answer')
            await r.choose(None)
            assert 'detour_backward' not in r.sage.calls[-1]['options']
            assert 'Current task: Reach the selected hunting area.' not in r.sage.calls[-1]['prompt']
            assert len(smites(r))==1
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('field',['session_epoch','target_continuity','owner','encounter_id'])
def test_new_destination_refreshes_expired_exit_intent(tmp_path,field):
    async def run():
        r=Rig(tmp_path)
        try:
            h,intent=await relocation(r)
            old=h.strategy_required['selected_exit']
            old[field]='expired' if isinstance(old[field],str) else old[field]+1
            h.phase='choose_area';h.planning_requested=True
            await r.choose('explore_visible')
            renewed=h.strategy_required['selected_exit']
            assert renewed['request_id']!=intent['request_id']
            assert h.plan['relocation_request_id']==renewed['request_id']
            await r.choose(None)
            await r.choose('detour_backward')
            assert h.pending['purpose']=='travel' and len(smites(r))==1
        finally:await r.close()
    asyncio.run(run())
