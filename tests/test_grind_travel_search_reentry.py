"""Live-run regression: observed travel must permit fresh search without erasing history."""
import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from test_grind_travel_acceptance import TravelRig


async def exhausted_search(r):
    await r.travel(destination=(28.5,74.2))
    r.position=[28.8,74.9]
    # These cases exercise voluntary search reentry before destination arrival.
    # Automatic arrival/acquisition is covered separately.
    r.c.hunt.catalog['offline_patch']={**r.c.hunt.plan['area'],'coordinate':[28.5,70.]}
    await r.action('begin_hunt')
    for _ in range(2):
        await r.choose('target_enemy');await r.choose('no_selected_frame')
    for action in ('forward','turn_left','turn_right'):
        for _ in range(2):
            await r.choose(action);await r.choose('motion_no_useful_effect')
    h=r.c.hunt
    prior={'failures':deepcopy(h.action_failures),'outcomes':deepcopy(h.outcomes),
        'revision':h.search_revision,'motion_key':h.motion_key('forward','search')}
    assert h.failures['target']==2 and h.motion_count('forward','search')==2
    # Exhausted local actions hand off to destination planning without asking
    # Sage to approve a singleton observation-only action.
    calls=len(r.sage.calls)
    result=await r.choose(None)
    assert result.status=='grind_reobserve' and len(r.sage.calls)==calls
    r.sage.answers.clear()
    await r.choose('choose_area:offline_patch')
    return prior


@pytest.mark.parametrize('clean',[False,True])
def test_displacement_then_small_step_reenters_search(tmp_path,clean):
    async def run():
        r=TravelRig(tmp_path,clean=clean)
        try:
            prior=await exhausted_search(r);h=r.c.hunt
            # Exact live-run endpoint sequence: one resolved translation, then
            # a smaller final step whose individual delta is below precision.
            await r.action('probe_forward');r.position=[28.7,74.7]
            await r.action('advance_forward');r.position=[28.6,74.6]
            assert h.search_revision==prior['revision']  # Input is not credit.
            await r.action('begin_hunt')
            assert h.last_completed_action['status']=='unresolved_precision'
            assert h.search_revision==prior['revision']+1 and h.failures['target']==0
            assert h.motion_count('forward','search')==0
            assert all(h.action_failures[k]==v for k,v in prior['failures'].items())
            assert h.outcomes[:len(prior['outcomes'])]==prior['outcomes']
            evidence=h.search_changes[-1]['evidence']
            assert evidence['position_before']==[28.8,74.9]
            assert evidence['position_after']==[28.6,74.6]
            assert len(evidence['travel_receipts'])==2
            assert not h.planning_requested and h.phase=='search'
            assert h.strategy_required is None
            await r.choose('target_enemy')
            assert {'target_enemy','forward','turn_left','turn_right'}<=r.sage.calls[-1]['options'].keys()
            r.world.name='Young Wolf';await r.choose('attack_mob_level_1')
            assert len(r.casts())==1 and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('case',['declaration','turn','unresolved','unknown','returned','wrong_epoch','wrong_receipt','changed_source'])
def test_no_search_renewal_without_current_changed_travel_endpoint(tmp_path,case):
    async def run():
        r=TravelRig(tmp_path)
        try:
            prior=await exhausted_search(r);h=r.c.hunt
            if case=='turn':await r.action('turn_left')
            elif case!='declaration':
                await r.action('probe_forward')
                if case=='unresolved':r.position=[28.7,74.8]
                elif case=='unknown':r.position=None
                else:
                    r.position=[28.7,74.7]
                    await r.action('advance_forward')
                    if case=='returned':r.position=[28.8,74.9]
                    elif case=='wrong_epoch':h.recent_moves[-1]['session_epoch']='retired-epoch'
                    elif case=='wrong_receipt':h.pending['previous_gameplay_receipt_id']='unrelated-input'
                    elif case=='changed_source':Path(h.recent_moves[-1]['source_image']).write_bytes(b'changed retained source')
            await r.choose(None)
            assert 'begin_hunt' not in r.sage.calls[-1]['options']
            assert h.phase=='travel' and h.strategy_required
            assert h.search_revision==prior['revision'] and h.failures['target']==2
            assert all(h.action_failures[k]==v for k,v in prior['failures'].items())
            assert {'change_destination','urgent_state'}<=r.sage.calls[-1]['options'].keys()
        finally:await r.close()
    asyncio.run(run())


def test_third_sector_entry_is_consumed_once_and_preserves_combat_debt(tmp_path):
    async def run():
        r=TravelRig(tmp_path)
        try:
            prior=await exhausted_search(r);h=r.c.hunt
            h.tried_sectors=[1,2];h.sector=2
            h.failures['combat']=2;h.cast_obligation='retained unknown combat'
            await r.action('probe_forward');r.position=[28.7,74.7]
            await r.action('begin_hunt')
            assert h.phase=='search' and not h.planning_requested
            assert h.failures['combat']==2 and h.cast_obligation=='retained unknown combat'
            assert h.search_revision==prior['revision']+1
            # Merely choose travel again and begin here; the old record cannot
            # grant another local revision or erase newly accumulated failures.
            h.failures['target']=2
            h.choose(dict(h.plan['area']),r.world.capture(),'offline-return',1)
            await r.action('begin_hunt')
            assert h.search_revision==prior['revision']+1 and h.failures['target']==2
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('returned',[False,True])
def test_long_travel_keeps_original_sector_after_recent_history_rolls(tmp_path,returned):
    async def run():
        r=TravelRig(tmp_path)
        try:
            prior=await exhausted_search(r);h=r.c.hunt
            h.plan['area']['coordinate']=[28.8,70.]
            await r.action('probe_forward')
            origin=[28.8,74.9]
            for index in range(1,7):
                r.position=[28.8,round(74.9-index*.2,1)] if index<6 or not returned else origin[:]
                if index==6 and returned:await r.choose(None)
                else:await r.action('advance_forward' if index<6 else 'begin_hunt')
            assert len(h.recent_moves)==5
            assert h.last_completed_action['search_origin']['position']==origin
            assert h.search_revision==prior['revision']+(0 if returned else 1)
            assert h.failures['target']==(2 if returned else 0)
            if returned:assert 'begin_hunt' not in r.sage.calls[-1]['options'] and h.strategy_required
        finally:await r.close()
    asyncio.run(run())


def test_source_mutated_during_sage_choice_cannot_renew_search(tmp_path):
    async def run():
        r=TravelRig(tmp_path)
        try:
            prior=await exhausted_search(r);h=r.c.hunt
            await r.action('probe_forward');r.position=[28.7,74.7]
            async def changed():
                Path(h.last_completed_action['search_origin']['source_image']).write_bytes(b'changed during Sage await')
            r.sage.hook=changed
            result=await r.choose('begin_hunt')
            assert result.status=='precondition_failed'
            assert h.phase=='travel' and h.strategy_required
            assert h.search_revision==prior['revision'] and h.failures['target']==2
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('changed',['source','strategy'])
def test_strategy_search_reentry_rechecks_proof_after_dispatch_guard(tmp_path,changed):
    async def run():
        r=TravelRig(tmp_path)
        try:
            prior=await exhausted_search(r);h=r.c.hunt
            await r.action('probe_forward');r.position=[28.7,74.7]
            original=r.c.travel_guard
            def guard(*args,**kwargs):
                check=original(*args,**kwargs)
                async def changed_after_check(*check_args):
                    result=await check(*check_args)
                    assert result.approved
                    if changed=='source':
                        Path(h.last_completed_action['search_origin']['source_image']).write_bytes(b'changed after dispatch guard')
                    else:h.strategy_required['request_id']='different-strategy-request'
                    return result
                return changed_after_check
            r.c.travel_guard=guard
            # Keep the ordinary rig's successful-dispatch assertion disabled
            # for this intentionally rejected late-proof test.
            async def provider_hook():pass
            r.sage.hook=provider_hook
            before=list(r.physical_keys())
            result=await r.choose('begin_hunt')
            assert result.status=='dispatch_guard_rejected'
            assert h.phase=='travel' and h.strategy_required
            assert h.search_revision==prior['revision'] and h.failures['target']==2
            assert all(h.action_failures[k]==v for k,v in prior['failures'].items())
            assert r.physical_keys()==before and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_new_strategy_cannot_reuse_prior_displacement_or_its_carried_origin(tmp_path):
    async def run():
        r=TravelRig(tmp_path)
        try:
            await r.travel()
            await r.action('probe_forward');r.position=[10.4,10.]
            await r.action('change_destination')
            h=r.c.hunt
            assert h.last_completed_action['status']=='observed_displacement'
            old_receipt=h.last_completed_action['receipt_id']
            # Initial ordinary planning may inspect here. A subsequent explicit
            # strategy request must require new evidence from this position.
            await r.choose('search_here')
            # Strategy review follows attempted local acquisition; an idle
            # unsearched startup now offers concrete acquisition actions first.
            h.confirmed_target_absences=2
            await r.choose('change_search_strategy');await r.choose('explore_visible')
            await r.choose(None)
            assert 'begin_hunt' not in r.sage.calls[-1]['options']
            assert h.strategy_required and h.search_revision==0
            await r.action('detour_strafe_left');r.position=[10.5,10.]
            # Assess the unresolved first step while choosing an unconsumed
            # first-page alternative. Success spends only the chosen strafe;
            # a null here would instead consume the whole offered page.
            await r.action('detour_strafe_right')
            assert h.last_completed_action['status']=='unresolved_precision'
            assert h.last_completed_action['search_origin']['position']==[10.4,10.]
            assert 'begin_hunt' not in r.sage.calls[-1]['options']
            assert h.strategy_required and h.search_revision==0
            # A new resolved displacement can fulfill the request, preserving
            # the earlier receipt as history rather than proof for this entry.
            r.position=[10.7,10.]
            await r.action('begin_hunt')
            assert h.strategy_required is None and h.search_revision==1
            assert old_receipt not in h.search_changes[-1]['evidence']['travel_receipts']
            assert any(item['receipt_id']==old_receipt for item in h.outcomes)
        finally:await r.close()
    asyncio.run(run())
