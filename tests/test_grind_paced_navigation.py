import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sage_wow.agent import grind_navigation_recovery as recovery
from test_grind_travel_acceptance import TravelRig
from test_grind_travel_menu import exhaust
from test_active_attacker_recovery import install_hud


def test_exhausted_navigation_can_reconsider_without_refunding_history(tmp_path, monkeypatch):
    async def run():
        clock = [100.]
        monkeypatch.setattr(recovery, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        r = TravelRig(tmp_path, clean=False)
        try:
            await r.travel()
            install_hud(r, target_health=None)
            await exhaust(r)
            state = recovery.record(r.c)
            debt = deepcopy(r.c.hunt.travel_policy['retry_menu'])
            failures = deepcopy(r.c.hunt.action_failures)
            calls = len(r.sage.calls)
            clock[0] += 29.
            result = await r.choose(None)
            r.sage.answers.clear()
            assert result.status == 'grind_navigation_wait' and len(r.sage.calls) == calls
            clock[0] += 2.
            await r.choose(None)
            assert len(r.sage.calls) == calls + 1
            assert 'urgent_state' in r.sage.calls[-1]['options']
            assert len(r.sage.calls[-1]['options']) <= 6
            assert r.c.hunt.travel_policy['retry_menu'] == debt
            assert r.c.hunt.action_failures == failures
            assert state['terminal_null'] and not recovery.available(state)
            assert not r.physical_keys() and not r.c.hunt.progress_facts
            await r.choose(None)
            r.sage.answers.clear()
            assert len(r.sage.calls) == calls + 1
            clock[0] += 31.
            result = await r.action('turn_left')
            assert result.receipt['completed']
            assert r.controls['turn_left']['keycode'] in r.physical_keys()
            assert r.c.hunt.action_failures == failures
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['unfinished', 'threat', 'pending', 'world', 'input',
                                  'mana', 'health', 'selected', 'player_error', 'stale'])
def test_retained_cast_navigation_still_requires_current_safe_travel(tmp_path, monkeypatch, fault):
    from sage_wow.agent.grind_resources import hud_resources

    async def run():
        clock = [100.]
        monkeypatch.setattr(recovery, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        r = TravelRig(tmp_path, clean=False)
        try:
            await r.travel()
            install_hud(r, target_health=None)
            h = r.c.hunt
            h.cast_obligation = 'Current linked facing error requires observed correction'
            await exhaust(r)
            clock[0] += 31.
            frame = r.world.capture()
            target = {**await r.c.target_proposal(frame), 'hud': hud_resources(r.c, frame)}
            if fault == 'unfinished': h.encounter_ended = False
            elif fault == 'threat': h.active_threat = {'current': True}
            elif fault == 'pending': h.pending = {'family': 'combat'}
            elif fault == 'world': r.c.require_world = True
            elif fault == 'input': h.input_effect_unverified = True
            elif fault == 'mana': target['hud']['player_mana'] = 0
            elif fault == 'health': target['hud']['player_health'] = .1
            elif fault == 'selected': target['name'] = 'Current creature'
            elif fault == 'player_error': h.cast_error = {'status': 'active', 'kind': 'standing'}
            else: frame = replace(frame, captured_at='2020-01-01T00:00:00+00:00')
            before = deepcopy(recovery.record(r.c))
            # These are the actual candidates from the original guarded menu.
            candidates = list(r.sage.calls[-1]['options'].values())
            assert recovery.reconsider(r.c, frame, target, candidates) == ([], None)
            assert recovery.record(r.c) == before
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('retained_cast', [False, True])
def test_physical_navigation_reconsideration_does_not_require_hunting_admission(tmp_path, monkeypatch, retained_cast):
    async def run():
        clock = [100.]
        monkeypatch.setattr(recovery, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        r = TravelRig(tmp_path, clean=False)
        try:
            await r.travel()
            install_hud(r, target_health=None)
            h = r.c.hunt
            if retained_cast:
                h.cast_obligation = 'Current linked facing error requires observed correction'
                assert h.encounter_ended and h.cast_blocks_acquisition()
            await exhaust(r)
            state = recovery.record(r.c)
            before = deepcopy((h.action_failures, h.target_history, h.cast_obligation))
            calls = len(r.sage.calls)
            if retained_cast:
                assert state['requests'] == 0 and recovery.available(state)
            clock[0] += 31.
            await r.choose(None)
            assert len(r.sage.calls) == calls + 1
            assert not {'target_enemy', 'begin_hunt'} & r.sage.calls[-1]['options'].keys()
            assert all(name == 'urgent_state' or option.binding['type'] in {'keypress', 'keypress_chord'}
                       for name, option in r.sage.calls[-1]['options'].items())
            clock[0] += 31.
            result = await r.action('turn_left')
            assert result.receipt['completed'] and result.receipt['possible_input']
            assert (h.action_failures, h.target_history, h.cast_obligation) == before
            assert not h.progress_facts and not h.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_unspent_review_without_available_hunting_choices_can_still_move(tmp_path,monkeypatch):
    from test_grind_travel_scout import setup
    from test_grind_navigation_recovery import destinations,exhaust_pages
    async def run():
        clock=[100.]
        monkeypatch.setattr(recovery,'time',SimpleNamespace(monotonic=lambda:clock[0]))
        r,_=await setup(tmp_path,monkeypatch)
        try:
            destinations(r);h=r.c.hunt
            r.c.config['fixed_hunting_area']=h.fixed_hunting_area='offline_patch'
            await exhaust_pages(r)
            await r.action('target_enemy')
            await r.choose('no_selected_frame')
            state=recovery.record(r.c)
            assert state['requests']==1 and recovery.available(state)
            before=deepcopy((h.action_failures,h.target_history,h.travel_policy['retry_menu']))
            result=await r.choose(None);r.sage.answers.clear()
            assert result.status=='grind_navigation_wait'
            assert r.c.navigation_wait['readiness']['reason']=='no_eligible_hunting_choices'
            calls=len(r.sage.calls)
            clock[0]+=31.
            await r.choose(None)
            assert len(r.sage.calls)==calls+1
            assert not any(x=='target_enemy' or x.startswith('choose_area:') for x in r.sage.calls[-1]['options'])
            clock[0]+=31.
            result=await r.action('turn_left')
            assert result.receipt['completed'] and result.receipt['possible_input']
            assert state['requests']==1 and recovery.available(state)
            assert (h.action_failures,h.target_history,h.travel_policy['retry_menu'])==before
            assert not h.credited_kills
        finally:await r.close()
    asyncio.run(run())
