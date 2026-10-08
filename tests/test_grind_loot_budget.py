"""Optional leveling loot cannot monopolize hunting; explicit fake input only."""
import asyncio
from copy import deepcopy

import pytest

from sage_wow.agent import grind_loot as loot, grind_loot_budget as budget
from sage_wow.agent.cycle import ActionCandidate
from sage_wow.agent.grind_only import settings
from test_grind_loot_integration import LootRig, combat_portrait
from test_grind_inventory_policy import InventoryRig


def enable(r):
    policy={'character':'Test Player','goal_level':r.c.config['goal_level'],
        'selection_failures':2,'selection_seconds':20.,'total_seconds':30.}
    r.c.profile.values['grind_only'].update(loot_enabled=True,optional_loot_budget=policy)
    r.c.config.update(loot_enabled=True,committed_combat=True,optional_loot_budget=deepcopy(policy))


def entry(r):
    return next(reversed(budget.state(r.c)['sources'].values()))


async def tick(r):
    r.c.wait_until=0
    f=r.world.capture()
    return await loot.process_loot(r.c,f,await r.c.target_proposal(f),{},await r.c.rows(f))


@pytest.fixture
def point(monkeypatch):
    monkeypatch.setattr(loot,'_corpse_choices',lambda *args:[ActionCandidate('corpse_select_0',
        'Offline corpse point',{'type':'click','image_x':450,'image_y':260})])


def test_rejected_clicks_count_then_retire_and_tab_without_escape(tmp_path,monkeypatch,point):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();enable(r);r.death()
            monkeypatch.setattr(loot,'_corpse_point_continuity',lambda *a:{'approved':False})
            async def noop():pass
            r.sage.hook=noop
            for _ in range(2):
                result=await r.loot('corpse_select_0')
                assert result.status=='dispatch_guard_rejected'
            r.sage.hook=None
            assert entry(r)['failures']==2 and r.c.loot.data['attempts']==0
            old=deepcopy((r.c.hunt.failures,r.c.hunt.selected_presence,r.c.hunt.progress_facts))
            calls=len(r.sage.calls)
            assert (await tick(r)).status=='grind_reobserve'
            assert len(r.sage.calls)==calls and not loot.loot_route_pending(r.c)
            assert r.c.loot.data['outcome']=='skipped_unverified' and not r.physical_keys()
            assert (r.c.hunt.failures,r.c.hunt.selected_presence,r.c.hunt.progress_facts)==old
            await r.choose('target_enemy')
            assert set(r.sage.calls[-1]['options'])=={'target_enemy','turn_left','turn_right','forward'}
            assert r.physical_keys()==[r.controls['target_enemy']['keycode']]
            assert not budget.handoff_pending(r.c) and not r.c.hunt.credited_kills and not r.casts()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('method',['click','f'])
def test_successful_loot_still_verifies_and_returns_to_hunting(tmp_path,point,method):
    async def run():
        r=LootRig(tmp_path);r.backend.mouse_button=lambda *a:r.backend.events.append(('mouse',*a))
        try:
            await r.start();enable(r);r.death()
            if method=='click':await r.loot('corpse_select_0')
            else:
                await r.loot('loot_remaining');await r.loot('loot_selected_corpse');await r.loot('interact_target')
            await r.loot('loot_verified');await r.loot('loot_verified')
            assert not r.c.loot.pending and entry(r)['status']=='verified_looted'
            assert not r.c.hunt.credited_kills
            await r.choose('target_enemy')
            assert r.c.hunt.pending['family']=='target'
        finally:await r.close()
    asyncio.run(run())


def test_completed_click_gets_verification_after_selection_deadline(tmp_path,point):
    async def run():
        r=LootRig(tmp_path);r.backend.mouse_button=lambda *a:r.backend.events.append(('mouse',*a))
        try:
            await r.start();enable(r);r.death();await r.loot(None)
            entry(r)['spent']=19.
            await r.loot('corpse_select_0')
            assert entry(r)['awaiting_result'] and not entry(r)['failures']
            entry(r)['spent']=21.
            await r.loot('loot_verified')
            assert 'corpse_select_0' not in r.sage.calls[-1]['options']
            assert r.c.loot.pending
            await r.loot('loot_verified')
            assert entry(r)['status']=='verified_looted'
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('pending_result',[False,True])
def test_time_limit_retires_without_an_extra_provider_vote(tmp_path,point,pending_result):
    async def run():
        r=LootRig(tmp_path);r.backend.mouse_button=lambda *a:r.backend.events.append(('mouse',*a))
        try:
            await r.start();enable(r);r.death()
            await r.loot('corpse_select_0' if pending_result else None)
            entry(r)['spent']=30. if pending_result else 20.
            calls=len(r.sage.calls);inputs=list(r.backend.events)
            await tick(r)
            assert not r.c.loot.pending and entry(r)['status']=='skipped_unverified'
            assert len(r.sage.calls)==calls and r.backend.events==inputs
            assert not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('which',['provider','guard'])
def test_deadline_rechecked_before_input(tmp_path,point,monkeypatch,which):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();enable(r);r.death();await r.loot(None)
            if which=='provider':
                async def late():entry(r)['spent']=20.
                r.sage.hook=late
            else:
                original=loot._guard
                async def guard(*a,**kw):
                    check=await original(*a,**kw)
                    async def late(f):
                        result=await check(f);entry(r)['spent']=20.;return result
                    return late
                monkeypatch.setattr(loot,'_guard',guard)
                async def noop():pass
                r.sage.hook=noop
            result=await r.loot('corpse_select_0')
            assert result.status!='dispatched' and all(e==('release',) for e in r.backend.events)
            r.sage.hook=None;await tick(r)
            assert not r.c.loot.pending
        finally:await r.close()
    asyncio.run(run())


def test_partial_click_at_expiry_stops_and_preserves_loot(tmp_path,point):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();enable(r);r.death();await r.loot(None)
            def partial(*args):
                entry(r)['spent']=31.
                raise RuntimeError('offline unknown click')
            r.backend.mouse_button=partial
            async def noop():pass
            r.sage.hook=noop
            await r.loot('corpse_select_0')
            assert r.c.stopped and r.c.loot.pending and entry(r)['status']=='pending'
            assert not budget.retire_expired(r.c,r.c.loot,r.world.capture())
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interruption',['focus','healing','combat','reload'])
def test_spent_allowance_survives_interruption_without_charging_other_work(tmp_path,interruption):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();enable(r);r.death();await r.loot(None)
            entry(r).update(spent=19.,failures=1);budget.save(r.c)
            if interruption=='focus':r.c.pause_focus();r.c.resume_focus()
            elif interruption=='reload':del r.c.loot_budget_state;del r.c.loot;del r.c.loot_state
            elif interruption=='healing':r.c.loot.data['resume_after_recovery']=True
            else:r.c.loot.data['interrupted']=True
            r.c.hunt.active_seconds+=600
            await r.loot('loot_remaining')
            assert 19.<=entry(r)['spent']<20. and entry(r)['failures']==1
            entry(r)['spent']=20.
            await tick(r)
            assert not r.c.loot.pending
        finally:await r.close()
    asyncio.run(run())


def test_focus_loss_stops_inflight_budget_clock(tmp_path,monkeypatch):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();enable(r);r.death();await r.loot(None)
            budget.suspend(r.c)
            clock=[100.];monkeypatch.setattr(budget,'monotonic',lambda:clock[0])
            budget.begin(r.c,r.c.loot);clock[0]=105.;r.c.pause_focus()
            charged=entry(r)['spent'];clock[0]=1000.;budget.suspend(r.c)
            assert entry(r)['spent']==charged and charged>=5.
        finally:await r.close()
    asyncio.run(run())


def test_time_between_fast_loot_decisions_counts_toward_deadline(tmp_path,monkeypatch):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();enable(r);r.death()
            clock=[100.];monkeypatch.setattr(budget,'monotonic',lambda:clock[0])
            await r.loot(None)
            clock[0]+=20.
            calls=len(r.sage.calls);await tick(r)
            assert not r.c.loot.pending and len(r.sage.calls)==calls
        finally:await r.close()
    asyncio.run(run())


def test_suspected_postcast_loot_expires_back_to_retained_combat_debt(tmp_path):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();enable(r)
            await r.choose('attack_mob_level_1')
            r.world.name=''
            await r.choose('cannot_assess');await r.choose('cannot_assess')
            await r.choose('inspect_recent_corpse')
            await r.loot(None)
            obligation=deepcopy(r.c.hunt.cast_obligation)
            assert obligation and r.c.loot.pending
            entry(r)['spent']=30.;await tick(r)
            assert not r.c.loot.pending and r.c.hunt.cast_obligation==obligation
            assert r.c.hunt.compact_stage=='recovery'
            await r.choose(None)
            assert 'inspect_recent_corpse' not in r.sage.calls[-1]['options']
            assert not any(x.startswith('corpse_select') for x in r.sage.calls[-1]['options'])
            assert 'target_enemy' in r.sage.calls[-1]['options']
            assert not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_selection_failures_apply_only_to_active_source(tmp_path,monkeypatch,point):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();enable(r);r.death();r.c.hunt.loot_request['encounter_id']=1
            await r.loot(None);first=entry(r)
            r.death('next');r.c.hunt.loot_request['encounter_id']=2
            monkeypatch.setattr(loot,'_corpse_point_continuity',lambda *a:{'approved':False})
            async def noop():pass
            r.sage.hook=noop
            for _ in range(2):await r.loot('corpse_select_0')
            second=entry(r)
            assert first['failures']==2 and second['failures']==0 and second['spent']==0
            r.sage.hook=None;await tick(r)
            assert first['status']=='skipped_unverified' and second['status']=='pending'
            assert r.c.loot.pending
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('outcome',['skip','success'])
def test_f_or_confirmation_for_one_source_cannot_verify_the_next(tmp_path,point,outcome):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();enable(r);r.death();r.c.hunt.loot_request['encounter_id']=1
            await r.loot(None);first=entry(r)
            r.death('next');r.c.hunt.loot_request['encounter_id']=2
            await r.loot('loot_remaining');await r.loot('loot_selected_corpse')
            assert first['confirmed_dead'] and not entry(r)['confirmed_dead']
            await r.loot('interact_target')
            assert r.c.loot.verification_available
            if outcome=='skip':first['spent']=30.;await tick(r)
            else:await r.loot('loot_verified');await r.loot('loot_verified')
            assert r.c.loot.pending and not r.c.loot.verification_available
            assert r.c.loot.data['attempts']==0 and entry(r)['status']=='pending'
            await r.loot(None)
            assert 'loot_verified' not in r.sage.calls[-1]['options']
        finally:await r.close()
    asyncio.run(run())


def test_expired_loot_still_yields_to_current_attacker(tmp_path):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();enable(r);r.death();await r.loot(None)
            entry(r)['spent']=30.;combat_portrait(r)
            await r.loot('under_attack')
            assert r.c.loot.pending and r.c.loot.data['interrupted'] and entry(r)['status']=='pending'
        finally:await r.close()
    asyncio.run(run())


def test_retired_source_cannot_reopen_from_new_frame_or_cast_in_same_encounter(tmp_path):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();enable(r);r.death();r.c.hunt.loot_request['encounter_id']=7
            await r.loot(None);entry(r)['spent']=20.;await tick(r)
            r.death('fresh-view');r.c.hunt.loot_request['encounter_id']=7
            assert not budget.request_allowed(r.c,r.c.hunt.loot_request)
            await tick(r)
            assert not r.c.loot.pending and not r.c.hunt.loot_request
            assert len(budget.state(r.c)['sources'])==1
        finally:await r.close()
    asyncio.run(run())


def test_new_same_name_corpse_has_own_budget_and_does_not_refresh_old_source(tmp_path):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();enable(r);r.death();r.c.hunt.loot_request['encounter_id']=1
            await r.loot(None);old=entry(r);old.update(spent=20.,failures=2)
            r.death('next');r.c.hunt.loot_request['encounter_id']=2
            await tick(r)
            assert old['status']=='skipped_unverified' and r.c.loot.pending
            assert entry(r)['status']=='pending' and entry(r)['spent']<1.
            assert len(r.c.loot.data['death_sources'])==1
            assert r.c.loot.data['death_sources'][0]['encounter_id']==2
            assert not budget.handoff_pending(r.c)
            await r.loot('loot_remaining')
        finally:await r.close()
    asyncio.run(run())


def test_retirement_closes_real_loot_panel_without_global_inventory_suppression(tmp_path):
    async def run():
        r=InventoryRig(tmp_path)
        try:
            await r.start();enable(r);r.death();await r.loot(None)
            entry(r)['spent']=20.;await tick(r);r.panel=True
            await r.choose('close_visible_ui');assert not r.panel
            await r.choose('world_normal_confirmed');await r.choose('target_enemy')
            from sage_wow.agent.grind_inventory import policy,effective_loot_enabled
            assert policy(r.c) is None and effective_loot_enabled(r.c)
            assert not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('method',['click','f'])
def test_queued_source_preserves_current_result_and_prompt_scope(tmp_path,point,method):
    async def run():
        r=LootRig(tmp_path);r.backend.mouse_button=lambda *a:r.backend.events.append(('mouse',*a))
        try:
            await r.start();enable(r);r.death();r.c.hunt.loot_request['encounter_id']=1
            if method=='click':await r.loot('corpse_select_0')
            else:
                await r.loot('loot_remaining');await r.loot('loot_selected_corpse');await r.loot('interact_target')
            await r.loot('loot_verified')
            fields=('attempts','confirmations','click_loot_authority','selected_corpse','stage')
            before={k:deepcopy(r.c.loot.data.get(k)) for k in fields}
            r.death('next');r.c.hunt.loot_request['encounter_id']=2
            loot._sweep(r.c)
            assert {k:r.c.loot.data.get(k) for k in fields}==before
            assert entry(r)['spent']==0 and entry(r)['failures']==0
            await r.loot('loot_verified')
            call=r.sage.calls[-1]
            assert 'current bound corpse is emptied' in call['instructions']
            assert 'CURRENT bound corpse' in call['options']['loot_verified'].description
            assert 'all recent corpses' not in call['instructions']
            assert r.c.loot.pending and len(r.c.loot.data['death_sources'])==1
            assert r.c.loot.data['death_sources'][0]['encounter_id']==2
            assert not r.c.loot.verification_available
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('task',['level','observation'])
def test_nonloot_early_return_suspends_existing_clock(tmp_path,monkeypatch,task):
    async def run():
        from types import SimpleNamespace
        from sage_wow.agent.cycle import CycleResult
        r=LootRig(tmp_path);clock=[100.]
        monkeypatch.setattr(budget,'monotonic',lambda:clock[0])
        try:
            await r.start();enable(r);r.death();await r.loot(None)
            clock[0]+=5.
            if task=='level':
                monkeypatch.setattr(r.c.level,'due',lambda now:False)
                await r.c.check_level(r.world.capture())
            else:
                async def reconcile(frame):
                    clock[0]+=100.
                    return CycleResult('grind_reobserve')
                r.c.observation_transaction={'restoration_needed':True}
                r.c.observation=SimpleNamespace(reconcile=reconcile)
                await r.c._process(r.world.capture())
            clock[0]+=100.
            key=next(iter(budget.state(r.c)['sources']))
            assert budget.spent(r.c,key)==5.
            assert r.c.loot.pending and entry(r)['status']=='pending'
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('bad',[{'character':'Other'},{'goal_level':5},{'selection_failures':True},
    {'selection_failures':0},{'selection_seconds':float('nan')},{'total_seconds':19.},{'total_seconds':61.}])
def test_policy_must_match_character_goal_and_limits(tmp_path,bad):
    async def run():
        r=LootRig(tmp_path)
        try:
            enable(r);r.c.profile.values['grind_only']['optional_loot_budget'].update(bad)
            with pytest.raises(ValueError,match='optional_loot_budget'):settings(r.c.profile)
        finally:await r.close()
    asyncio.run(run())
