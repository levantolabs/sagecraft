"""Keep declaration-only visual replanning from absorbing the travel loop."""
import asyncio
from copy import deepcopy

import pytest

from test_grind_travel_acceptance import TravelRig


async def visual_loop(r):
    await r.travel()
    h=r.c.hunt
    h.plan['area'].update(coordinate=None,zone_reference=None)
    h.action_failures['retained-combat-method']=2
    await r.action('change_destination')
    basis=deepcopy(h.destination_change_basis)
    await r.choose('explore_visible')
    return basis


@pytest.mark.parametrize('clean',[False,True])
def test_same_visible_replanning_yields_existing_guarded_movement(tmp_path,clean):
    async def run():
        r=TravelRig(tmp_path,clean=clean)
        try:
            basis=await visual_loop(r);h=r.c.hunt
            result=await r.choose(None)
            assert result.status!='dispatched'
            assert 'change_destination' not in r.sage.calls[-1]['options']
            assert {'detour_backward','turn_left','urgent_state'}<=r.sage.calls[-1]['options'].keys()
            assert 'change_destination=unchanged_replan' in r.sage.calls[-1]['prompt']
            assert h.phase=='travel' and h.destination_change_basis==basis
            await r.action('detour_backward')
            r.position=[9.6,10.]
            await r.action('change_destination')
            assert h.destination_change_basis['travel_receipt_id']!=basis['travel_receipt_id']
            assert h.action_failures['retained-combat-method']==2 and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['frame_only','new_visual_id','hud_inputs','target_receipt','recovery_roundtrip','focus_epoch'])
def test_nontravel_changes_do_not_renew_same_declaration(tmp_path,change):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            basis=await visual_loop(r);h=r.c.hunt
            if change=='new_visual_id':h.plan['area_id']='visual_new';h.plan['area']['id']='visual_new'
            elif change=='hud_inputs':
                before=r.c.cycle.input_generation
                await r.choose(None)  # Real fake-backend HUD hide/restore receipts.
                assert r.c.cycle.input_generation>before
            elif change=='target_receipt':h.last_gameplay_receipt_id='completed-tab'
            elif change=='recovery_roundtrip':h.phase='recover';h.phase='travel'
            elif change=='focus_epoch':r.c.cycle.invalidate('offline-focus-resume')
            prior_calls=len(r.sage.calls)
            await r.choose(None)
            assert len(r.sage.calls)>prior_calls
            assert 'change_destination' not in r.sage.calls[-1]['options']
            assert h.destination_change_basis==basis
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['position','destination'])
def test_changed_destination_or_position_permits_reconsideration(tmp_path,change):
    async def run():
        r=TravelRig(tmp_path)
        try:
            await visual_loop(r)
            if change=='position':r.position=[9.,10.]
            else:r.c.hunt.plan['area'].update(coordinate=[20.,10.],zone_reference=r.zone)
            await r.action('change_destination')
            assert r.c.hunt.phase=='choose_area'
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['basis','destination','travel_receipt'])
def test_late_changed_basis_rejects_before_replanning(tmp_path,change):
    async def run():
        r=TravelRig(tmp_path)
        try:
            await r.travel();h=r.c.hunt
            original=r.sage.decide_image_choice
            async def mutate(*args,**kwargs):
                result=await original(*args,**kwargs)
                if change=='basis':h.destination_change_basis={'changed':'late'}
                elif change=='destination':h.plan['area']['coordinate']=[30.,10.]
                else:h.last_completed_action={'receipt_id':'late-receipt'}
                return result
            r.sage.decide_image_choice=mutate
            r.sage.answers.append('change_destination')
            r.c.wait_until=0
            result=await r.c.process(r.world.capture())
            assert result.status!='dispatched' and h.phase=='travel'
            assert not h.planning_requested
        finally:await r.close()
    asyncio.run(run())
