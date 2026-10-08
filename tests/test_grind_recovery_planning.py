"""An empty recovery question cannot shadow destination review forever."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from sage_wow.agent.grind_search import HuntState
from test_grind_hunting_progression import configure
from test_grind_progression_scouting import PolicyRig


async def stalled_travel(r):
    await r.start_policy(soft=True); configure(r)
    r.world.name=''; r.world.target_level=None
    r.zone='Coldridge Valley'; r.position=[28.6,74.7]
    h=r.c.hunt; h.last_level=None; h.level_changed(3)
    await r.choose('choose_area:coldridge_southeast_troggs')
    r.c.request_recovery('repeated_null_provider_answer')
    return h


def test_protocol_abstention_before_arrival_hands_off_to_plan_then_guarded_travel(tmp_path):
    async def run():
        r=PolicyRig(tmp_path)
        try:
            h=await stalled_travel(r)
            h.action_failures['retained_failed_method']=2
            debt=deepcopy(h.action_failures); strategy=deepcopy(h.strategy_required)
            progress=deepcopy(h.progress_facts); keys=list(r.physical_keys())
            for _ in range(3):
                await r.choose(None)
                assert 'target_enemy' in r.sage.calls[-1]['options']
                assert 'cannot_assess' not in r.sage.calls[-1]['options']
            question=h.active_question; unclear=h.question_debt[question]
            await r.choose('choose_area:coldridge_west_boars')
            call=r.sage.calls[-1]
            assert 'search_here' not in call['options'] and 'target_enemy' not in call['options']
            assert 'choose_area:coldridge_southeast_troggs' in call['options']
            assert h.plan['area_id']=='coldridge_west_boars' and h.phase=='travel'
            assert h.compact_stage!='recovery' and not h.planning_requested
            assert h.action_failures==debt and h.strategy_required==strategy
            assert h.question_debt[question]==unclear and h.search_revision==0
            assert h.progress_facts==progress and not h.credited_kills
            assert r.physical_keys()==keys and not r.casts()
            await r.choose('probe_forward')
            assert r.physical_keys()==keys+[r.controls['forward']['keycode']]
            assert h.pending['purpose']=='travel' and not h.progress_facts
        finally: await r.close()
    asyncio.run(run())


def ready_state():
    h=HuntState({},'offline');h.phase='travel';h.compact_stage='recovery'
    h.unclear=3;h.selected_presence=False
    return h


@pytest.mark.parametrize('obligation', ['pending','uncertain_input','threat','cast','loot',
    'encounter','blocked','unknown_selection','selected','fresh_retry'])
def test_planning_handoff_cannot_override_current_obligations(obligation):
    h=ready_state();target={'name':None};frame=SimpleNamespace(frame_id='fresh',captured_at='offline')
    if obligation=='pending':h.pending={'family':'motion','receipt':{'completed':True}}
    elif obligation=='uncertain_input':h.input_effect_unverified=True
    elif obligation=='threat':h.active_threat={'current':True}
    elif obligation=='cast':h.cast_obligation='Unresolved cast correction'
    elif obligation=='loot':h.loot_request={'suspected':True}
    elif obligation=='encounter':h.encounter_ended=False
    elif obligation=='blocked':h.blocked={'reason':'blocked_timing'}
    elif obligation=='unknown_selection':h.selected_presence=None
    elif obligation=='selected':target={'name':'Small Crag Boar','visual_observation':{'selected_hud':'present'}}
    else:h.unclear=2
    assert h.plan_unresolved_recovery(frame,target) is None
    assert not h.planning_requested and h.recovery_plan_revision is None


def test_handoff_does_not_ping_pong_or_renew_attempts_on_focus_or_plan_change():
    h=ready_state();frame=SimpleNamespace(frame_id='fresh',captured_at='offline')
    h.action_failures={'motion:old':2};h.unresolved_motion={'unknown':2}
    fact=h.plan_unresolved_recovery(frame,{'name':None})
    assert fact and not fact['action_authority'] and not fact['progress_credit']
    assert h.strategy_required['reason']=='unresolved_recovery_question'
    h.request_recovery('fresh_focus_or_different_plan')
    assert h.plan_unresolved_recovery(frame,{'name':None}) is None
    assert not h.planning_requested and h.search_revision==0
    assert h.action_failures=={'motion:old':2} and h.unresolved_motion=={'unknown':2}
    h.search_revision+=1  # The normal observed-change path owns this revision.
    assert h.plan_unresolved_recovery(frame,{'name':None})


def test_failed_planning_returns_to_physical_recovery_without_repeating_handoff(tmp_path):
    async def run():
        r=PolicyRig(tmp_path)
        try:
            h=await stalled_travel(r)
            for _ in range(3):await r.choose(None)
            for _ in range(2):await r.choose(None)
            assert not h.planning_requested and h.compact_stage=='recovery'
            await r.choose('forward')
            assert 'choose_area:coldridge_west_boars' not in r.sage.calls[-1]['options']
            assert h.pending['purpose']=='search' and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interruption', ['stop','deadline'])
def test_recovery_planning_choice_needs_current_scope(tmp_path, monkeypatch, interruption):
    from test_grind_travel_recovery import TravelClock
    async def run():
        r=PolicyRig(tmp_path)
        try:
            h=await stalled_travel(r)
            for _ in range(3):await r.choose(None)
            prior=deepcopy(h.plan);keys=list(r.physical_keys())
            clock=TravelClock(monkeypatch,r)
            async def interrupt():
                if interruption=='stop':r.c.stop('offline_operator_stop')
                else:clock.offset=13
            r.sage.hook=interrupt
            result=await r.choose('choose_area:coldridge_west_boars')
            assert result.status!='dispatched' and h.plan==prior
            assert r.physical_keys()==keys and not h.progress_facts
        finally:await r.close()
    asyncio.run(run())
