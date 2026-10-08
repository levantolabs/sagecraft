"""Post-loot acquisition and travel reentry share evidence, not stale authority."""
import asyncio
from copy import deepcopy

import pytest

from sage_wow.agent import grind_inspection as inspection
from sage_wow.agent.grind_search import measure
from test_grind_loot_budget import enable, entry, tick
from test_grind_loot_integration import LootRig
from test_grind_retained_cast_acquisition import retained
from test_grind_travel_acceptance import TravelRig


@pytest.mark.parametrize('absences',[1,2])
def test_retired_loot_and_exhausted_inspection_do_not_invent_failed_search(tmp_path,absences):
    async def run():
        r=LootRig(tmp_path)
        try:
            h=await retained(r);enable(r);prior_casts=list(r.casts())
            recent=h.recent_combat
            h.loot_request={'frame_id':'offline-postcast-disappearance',
                'receipt_id':recent['receipt']['receipt_id'],'encounter_id':h.encounter,
                'target':deepcopy(recent['target']),'suspected':True,
                'death_observed':False,'credit_known':False}
            await r.loot(None);entry(r)['spent']=31.
            assert (await tick(r)).status=='grind_reobserve'
            assert r.c.loot.data['outcome']=='skipped_unverified'
            await r.choose('target_enemy');await r.choose('no_selected_frame')
            assert h.confirmed_target_absences==1 and not h.pending
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            # Explicit exhausted-state routing fixture, not an observer admission.
            episode=inspection._episode(r.c,frame,target)
            episode.update(attempts=2,exhausted=True)
            r.c.target_inspection_episode=episode
            assert episode['exhausted'] and not episode['name']
            debt=deepcopy((h.cast_obligation,h.action_failures,h.progress_facts))
            h.confirmed_target_absences=absences
            if absences==2:
                async def retired_while_choosing():r.c.target_inspection_episode=None
                r.sage.hook=retired_while_choosing
                result=await r.choose('change_search_strategy')
                assert result.status=='dispatched' and h.planning_requested
            else:
                await r.choose('target_enemy')
                assert 'change_search_strategy' not in r.sage.calls[-1]['options']
                assert {'target_enemy','turn_left','turn_right','forward'}==set(r.sage.calls[-1]['options'])
                assert r.c.target_inspection_episode is episode and episode['attempts']==2
                assert h.pending['family']=='target' and not h.planning_requested
            prompt=r.sage.calls[-1]['prompt']
            assert f'Earlier completed selections followed by no selected enemy: {absences}.' in prompt
            assert 'same view' not in prompt and 'found no nearby candidate' not in prompt
            assert (h.cast_obligation,h.action_failures,h.progress_facts)==debt
            assert r.casts()==prior_casts and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


async def away(r):
    await r.travel();await r.action('probe_forward');r.position=[9.6,10.]
    await r.choose(None)
    h=r.c.hunt
    assert h.forward_history and not h.pending
    return h


@pytest.mark.parametrize('turn',['turn_left','turn_right'])
def test_actual_search_turn_then_same_destination_requires_fresh_probe(tmp_path,turn):
    async def run():
        r=TravelRig(tmp_path)
        try:
            h=await away(r);r.c.config['committed_combat']=True
            old=deepcopy((h.forward_history,h.travel_failures,h.failed_direction_approaches))
            revision=h.travel_approach_revision;search=h.search_revision
            # This fixture represents genuine local hunting ownership, not a
            # recovery alias that may bypass a retained travel question.
            h.plan['phase']='search'
            h.phase='search';h.compact_stage='acquire';h.encounter_ended=True;h.selected_presence=False
            await r.choose(turn)
            assert h.pending['purpose']=='search'
            assert h.travel_approach_revision==revision+1 and h.search_revision==search
            assert (h.forward_history,h.travel_failures,h.failed_direction_approaches)==old
            h.choose(dict(h.plan['area']),r.world.capture(),'same-destination',1)
            await r.action('probe_forward')
            call=r.sage.calls[-1]
            assert 'Decision needed: after_turn' in call['prompt']
            assert 'advance_forward' not in call['options']
            assert 'Direction still current: no' in call['prompt']
            assert h.pending['travel_purpose']=='probe'
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('purpose',['travel','search','cast_correction','recovery_escape'])
@pytest.mark.parametrize('receipt_kind',['completed','no_input','partial','unknown','error'])
def test_turn_orientation_revision_needs_completed_physical_input_not_task_label(tmp_path,purpose,receipt_kind):
    async def run():
        r=TravelRig(tmp_path)
        try:
            h=await away(r);frame=r.world.capture()
            measurement=measure(r.c.profile,frame,await r.c.rows(frame),r.c.ocr)
            old=deepcopy((h.forward_history,h.travel_failures,h.failed_direction_approaches,h.search_revision,h.progress_facts))
            revision=h.travel_approach_revision
            receipt={'receipt_id':'explicit-offline-turn','completed':True,'possible_input':True,
                'generation_after':r.c.cycle.input_generation,'session_epoch':r.c.cycle.session_epoch}
            if receipt_kind=='no_input':receipt['possible_input']=False
            elif receipt_kind=='partial':receipt['completed']=False
            elif receipt_kind=='unknown':receipt['dispatch_unknown']=True
            elif receipt_kind=='error':receipt['error']='offline error'
            h.install('turn_left','motion',frame,receipt,measurement,purpose=purpose)
            assert h.travel_approach_revision==revision+(receipt_kind=='completed')
            assert (h.forward_history,h.travel_failures,h.failed_direction_approaches,h.search_revision,h.progress_facts)==old
            if receipt_kind=='completed':
                assert h.forward_decision(measurement,r.c.cycle.input_generation,r.c.cycle.session_epoch,.2)==('after_turn','probe_forward',None)
                assert not h.action_responses and h.mapping_generation is None
        finally:await r.close()
    asyncio.run(run())
