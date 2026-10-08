"""Live-run regression: missed nonoffensive selections retain history but cannot ban Tab."""
import asyncio
from copy import deepcopy

import pytest

from sage_wow.agent.grind_search import TARGET_RETRY_SECONDS
from test_grind_product_spec import Rig


async def two_misses(r):
    await r.start();r.world.name=''
    for _ in range(2):
        await r.choose('target_enemy');await r.choose('no_selected_frame')
    h=r.c.hunt
    assert h.failures['target']==2
    return h.last_completed_target_active_at,deepcopy(h.outcomes),deepcopy(h.action_failures)


def test_two_misses_allow_one_cooldown_retry_then_request_a_new_search(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            stamp,outcomes,failures=await two_misses(r);h=r.c.hunt
            h.active_seconds=stamp+TARGET_RETRY_SECONDS-1
            await r.choose(None)
            assert 'target_enemy' not in r.sage.calls[-1]['options']
            assert h.last_completed_target_active_at==stamp
            h.active_seconds=stamp+TARGET_RETRY_SECONDS+.1
            await r.choose('target_enemy')
            next_stamp=h.last_completed_target_active_at
            assert next_stamp>stamp and not h.target_selection_available()
            assert h.failures['target']==2 and h.action_failures==failures
            assert h.outcomes[:len(outcomes)]==outcomes
            assert not r.casts() and not h.credited_kills
            await r.choose('no_selected_frame')
            assert h.failures['target']==3
            await r.choose(None)
            assert 'target_enemy' not in r.sage.calls[-1]['options']
            assert 'explore_visible' in r.sage.calls[-1]['options']
            h.active_seconds=next_stamp+TARGET_RETRY_SECONDS+.1
            await r.choose('explore_visible')
            assert h.phase=='travel' and h.failures['target']==3
            assert r.physical_keys().count(r.controls['target_enemy']['keycode'])==3
            assert not r.casts() and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interruption',['input_uncertain','cooldown_changed','focus_changed'])
def test_retry_offer_revalidates_input_scope_and_cooldown_after_sage(tmp_path,interruption):
    async def run():
        r=Rig(tmp_path)
        try:
            stamp,_,_=await two_misses(r);h=r.c.hunt
            h.active_seconds=stamp+TARGET_RETRY_SECONDS+.1
            before=list(r.physical_keys())
            async def interrupted():
                if interruption=='input_uncertain':h.input_effect_unverified=True
                elif interruption=='cooldown_changed':h.last_completed_target_active_at=h.active_seconds
                else:r.c.pause_focus()
            r.sage.hook=interrupted
            result=await r.choose('target_enemy')
            assert 'target_enemy' in r.sage.calls[-1]['options']
            assert result.status!='dispatched' and r.physical_keys()==before
            assert not r.casts() and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_retry_selection_does_not_bypass_fresh_offensive_identity_guard(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            stamp,_,_=await two_misses(r);h=r.c.hunt
            h.active_seconds=stamp+TARGET_RETRY_SECONDS+.1
            await r.choose('target_enemy');r.world.name='Young Wolf'
            async def selection_changed():r.world.name='Different Wolf'
            r.sage.hook=selection_changed
            result=await r.choose('attack_mob_level_1')
            assert result.status=='dispatch_guard_rejected'
            assert not r.casts() and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_partial_tab_stops_and_cannot_refresh_completed_retry_timestamp(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            stamp,_,_=await two_misses(r);h=r.c.hunt
            h.active_seconds=stamp+TARGET_RETRY_SECONDS+.1
            original=r.backend.key
            def uncertain(code,down):
                if code==r.controls['target_enemy']['keycode'] and down:
                    raise RuntimeError('offline partial Tab dispatch')
                original(code,down)
            r.backend.key=uncertain
            r.c.wait_until=0;r.sage.answers.append('target_enemy')
            result=await r.c.process(r.world.capture())
            assert result.status!='dispatched' and r.c.stopped
            assert h.last_completed_target_active_at==stamp
            assert r.c.reason=='partial_or_unknown_grind_input'
            assert not r.casts() and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_cooldown_starts_after_sage_and_completed_input_not_request_start(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            stamp,_,_=await two_misses(r);h=r.c.hunt
            h.active_seconds=stamp+TARGET_RETRY_SECONDS+.1
            before={}
            async def slow_choice():
                before['active']=h.active_seconds
                await asyncio.sleep(.04)
            r.sage.hook=slow_choice
            await r.choose('target_enemy')
            assert h.last_completed_target_active_at>=before['active']+.035
            assert h.active_seconds==h.last_completed_target_active_at
            assert not h.target_selection_available()
        finally:await r.close()
    asyncio.run(run())
