"""Plan-bound travel retries with fake frames, provider and native backend."""
import asyncio
from copy import deepcopy

import pytest

from test_grind_travel_acceptance import TravelRig
from test_active_attacker_recovery import install_hud, native_fact
from test_grind_travel_recovery import events
from sage_wow.agent.grind_travel_recovery import resume


async def stalled(r):
    await r.travel()
    hud,bars=install_hud(r,target_health=None)
    h=r.c.hunt
    h.strategy_required={'reason':'unsuccessful_local_search','search_revision':h.search_revision,
        'captured_at':r.world.capture().captured_at}
    h.action_failures[h.motion_key('forward','search')]=2
    h.cast_obligation='Retained old range constraint; not offensive authority'
    await r.action('probe_forward')
    await r.choose(None);await r.choose(None)
    assert h.compact_stage=='recovery' and h.recovery_requested.get('travel_task')
    return h,hud,bars


@pytest.mark.parametrize('clean',[False,True])
def test_two_travel_abstentions_retry_same_task_then_guarded_detour(tmp_path,clean):
    async def run():
        r=TravelRig(tmp_path,clean=clean)
        try:
            h,_,_=await stalled(r)
            plan=deepcopy(h.plan);strategy=deepcopy(h.strategy_required)
            debt=deepcopy(h.action_failures);history=deepcopy(h.target_history)
            failures=deepcopy(h.travel_failures);revision=h.search_revision
            # The next finite page retains the chosen task/debt and exposes
            # diagonal alternatives, rather than reoffering the first page.
            await r.action('detour_backward_right')
            assert h.pending['purpose']=='travel' and h.pending['action']=='backward_right'
            assert h.plan==plan and h.strategy_required==strategy
            assert h.action_failures==debt and h.target_history==history and h.travel_failures==failures
            assert h.search_revision==revision and not h.progress_facts and not h.credited_kills
            assert h.cast_obligation=='Retained old range constraint; not offensive authority'
            assert not r.casts() and not r.pressed
            assert [x['reasoning_mode'] for x in events(r,'sage_request_started')[-3:]]==['off','off','auto']
            assert 'Continue the hunting destination you already chose.' in r.sage.calls[-1]['prompt']
            assert 'old encounter' not in r.sage.calls[-1]['prompt']
            assert all('change_search_strategy' not in call['options'] for call in r.sage.calls)
            assert events(r,'grind_travel_question_resumed')
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['epoch','generation','continuity','search_revision','encounter','owner',
    'plan','strategy','phase','reason','pending','blocked','unknown_input','threat','healing','mana',
    'loot','disengagement','world','stop','name','level','visual_presence','visual_name','visual_level',
    'target_bar','conflict','health','stale_health','health_confidence'])
def test_retry_does_not_override_changed_task_or_current_work(tmp_path,change):
    async def run():
        r=TravelRig(tmp_path)
        try:
            h,hud,_=await stalled(r)
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            if change=='epoch':r.c.cycle.session_epoch='other'
            elif change=='generation':r.c.cycle._input_generation+=1
            elif change=='continuity':h.target_continuity+=1
            elif change=='search_revision':h.search_revision+=1
            elif change=='encounter':h.encounter+=1
            elif change=='owner':h.combat_history_key='other'
            elif change=='plan':h.plan['request_id']='new-destination'
            elif change=='strategy':h.strategy_required['reason']='new-purpose'
            elif change=='phase':h.phase='recover'
            elif change=='reason':h.request_recovery('actual_survival_emergency')
            elif change=='pending':h.pending={'family':'combat'}
            elif change=='blocked':h.blocked={'reason':'blocked_timing'}
            elif change=='unknown_input':h.input_effect_unverified=True
            elif change=='threat':h.active_threat={'current':True}
            elif change=='healing':r.c.heal_pending={'current':True}
            elif change=='mana':h.no_mana=True
            elif change=='loot':h.loot_request={'current':True}
            elif change=='disengagement':h.disengagement={'current':True}
            elif change=='world':r.c.require_world=True
            elif change=='stop':r.c.stop('offline stop')
            elif change=='name':target['name']='Young Wolf'
            elif change=='level':target['levels']=[1]
            elif change=='visual_presence':target['visual_observation']={'selected_hud':'present'}
            elif change=='visual_name':target['visual_observation']={'name':'Young Wolf'}
            elif change=='visual_level':target['visual_observation']={'level':1}
            elif change=='target_bar':target['hud']['target_health']=0.
            elif change=='conflict':target['observation_conflict']={'current':True}
            elif change=='health':target['hud']['player_health']=.1
            elif change=='stale_health':target['hud']['frame_id']='other'
            else:target['hud']['health_confidence']=.1
            before=list(r.physical_keys())
            assert not resume(r.c,frame,target,h.pending,h.phase)
            assert h.compact_stage=='recovery' and r.physical_keys()==before
        finally:await r.close()
    asyncio.run(run())


def test_incoming_attacker_preempts_retry_in_actual_play(tmp_path,monkeypatch):
    from sage_wow.agent import grind_resources
    async def run():
        r=TravelRig(tmp_path)
        try:
            h,_,bars=await stalled(r)
            r.c.config.update(encounter_resources=True,committed_combat=True)
            monkeypatch.setattr(grind_resources,'hud_resources',bars)
            r.c.combat_log_facts=[native_fact(event='SWING_DAMAGE',own=False)]
            before=list(r.physical_keys())
            await r.choose(None)
            assert h.active_threat and h.phase!='travel'
            assert 'detour_strafe_left' not in r.sage.calls[-1]['options']
            assert r.physical_keys()==before and not r.casts()
        finally:await r.close()
    asyncio.run(run())


def test_stop_during_retry_provider_call_vetoes_movement(tmp_path):
    async def run():
        r=TravelRig(tmp_path)
        try:
            await stalled(r);before=list(r.physical_keys())
            async def stop():r.c.stop('offline provider-time stop')
            r.sage.hook=stop
            result=await r.choose('detour_backward')
            assert result.status!='dispatched' and r.physical_keys()==before
            assert r.c.hunt.pending is None and not r.pressed
        finally:await r.close()
    asyncio.run(run())


def test_retry_retains_original_travel_dispatch_deadline(tmp_path,monkeypatch):
    from sage_wow.agent import cycle
    from types import SimpleNamespace
    import time
    async def run():
        r=TravelRig(tmp_path)
        try:
            await stalled(r);before=list(r.physical_keys())
            async def expire():
                monkeypatch.setattr(cycle,'time',SimpleNamespace(
                    time=lambda:time.time()+13,monotonic=time.monotonic))
            r.sage.hook=expire
            result=await r.choose('detour_backward')
            assert result.status!='dispatched' and r.physical_keys()==before
            assert r.c.hunt.pending is None and not r.pressed
        finally:await r.close()
    asyncio.run(run())


def test_real_pending_clear_is_assessed_before_travel_retry(tmp_path):
    async def run():
        r=TravelRig(tmp_path)
        try:
            h,_,_=await stalled(r)
            r.c.config['committed_combat']=True
            h.phase='search'
            r.world.name='Young Wolf';r.world.target_level=1
            cleared=await r.choose('reject_selected_target')
            assert h.pending['family']=='clear' and cleared.receipt['completed']
            receipt=deepcopy(h.pending);before=list(r.physical_keys())
            r.world.name='';r.world.target_level=None
            await r.choose(None)
            assert h.pending==receipt
            assert r.sage.calls[-1]['instructions']=='Did the rejected selection clear?'
            await r.choose('target_cleared')
            assert r.sage.calls[-1]['instructions']=='Did the rejected selection clear?'
            assert 'detour_backward' not in r.sage.calls[-1]['options']
            assert h.pending is None and h.outcomes[-1]['outcome']=='target_cleared'
            assert r.physical_keys()==before and not r.casts()
        finally:await r.close()
    asyncio.run(run())
