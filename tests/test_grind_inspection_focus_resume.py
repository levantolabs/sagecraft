"""Real controller focus recovery with fake vision and native input disabled."""
import asyncio
from copy import deepcopy

import pytest

from test_grind_target_inspection_contract import ContractRig
from test_grind_exact_band_observation import start_band
from sage_wow.agent import grind_inspection as inspection


async def unresolved(r):
    await start_band(r)
    r.answers['level'] = 'unknown'
    assert (await r.observe_target()).status == 'grind_reobserve'
    assert r.c.target_inspection_episode['attempts'] == 1
    return deepcopy(r.c.target_inspection_episode)


async def refocus(r):
    r.c.pause_focus('clean_focus_lost')
    assert r.c.resume_focus()
    result = await r.choose('world_normal_confirmed')
    assert result.status == 'dispatched' and not result.receipt['possible_input']
    return result


def test_clean_focus_reuses_remaining_sensing_then_fresh_guarded_combat(tmp_path):
    async def run():
        r = ContractRig(tmp_path/'resume')
        try:
            old = await unresolved(r)
            r.c.hunt.action_failures['retained_motion'] = 2
            await refocus(r)
            resumed = r.c.target_inspection_episode
            assert resumed['id'] == old['id'] and resumed['attempts'] == 1
            assert resumed['started_active_at'] == old['started_active_at']
            assert resumed['source_scope']['session_epoch'] == r.c.cycle.session_epoch
            assert not resumed.get('invalidated_scope_by')
            assert not r.physical()
            r.answers['level'] = '2'
            assert (await r.observe_target()).status == 'grind_reobserve'
            assert len(r.wire) == 2
            cast = await r.choose('attack_mob_level_2')
            assert cast.status == 'dispatched' and cast.receipt['possible_input']
            assert r.events('dispatch_guard_checked')[-1]['approved']
            assert r.c.hunt.action_failures['retained_motion'] == 2
            assert not r.c.hunt.credited_kills
        finally:
            await r.close()
    asyncio.run(run())


def test_unused_new_selection_ledger_survives_unknown_cast_focus_pause(tmp_path):
    async def run():
        from test_grind_inspection_source_handoff import SourceRig, empty_source, tab, selected
        r = SourceRig(tmp_path/'unused')
        try:
            await empty_source(r)
            r.c.config['committed_combat'] = True
            await r.choose('no_selected_frame')
            await tab(r)
            r.drop_level = False
            await selected(r, expect_observer=False)
            old = deepcopy(r.c.target_inspection_episode)
            assert old['attempts'] == 0 and old['started_active_at'] is None
            cast = await r.choose('attack_mob_level_1')
            assert cast.status == 'dispatched' and cast.receipt['possible_input']
            await refocus(r)
            assert r.c.hunt.cast_obligation
            assert r.c.target_inspection_episode['id'] == old['id']
            assert r.c.target_inspection_episode['attempts'] == 0
            assert r.c.target_inspection_episode['started_active_at'] is None
            await r.choose('cannot_assess')
            await r.choose('cannot_assess')
            reinspect = await r.choose('reinspect_selected_frame')
            assert reinspect.status == 'dispatched' and not reinspect.receipt['possible_input']
            assert (await r.observe_target()).status == 'grind_reobserve'
            cast_again = await r.choose('attack_mob_level_1')
            assert cast_again.status == 'dispatched' and cast_again.receipt['possible_input']
            assert not r.c.hunt.credited_kills
            assert r.c.hunt.unassessed[-1]['receipt']['receipt_id'] == cast.receipt['receipt_id']
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation', ['spent', 'expired', 'configuration', 'damaged_source', 'unverified'])
def test_refocus_does_not_renew_or_bypass_sensing_limits(tmp_path, mutation):
    async def run():
        r = ContractRig(tmp_path/mutation)
        try:
            old = await unresolved(r)
            e = r.c.target_inspection_episode
            if mutation == 'spent': e['attempts'] = inspection.MAX_ATTEMPTS
            if mutation == 'expired': r.c.hunt.active_seconds = e['started_active_at'] + 61
            if mutation == 'configuration': r.c.config['target_level_box'] = [1, 2, 3, 4]
            if mutation == 'damaged_source':
                from pathlib import Path
                Path(e['source_image']).write_bytes(b'corrupted retained source')
            if mutation == 'unverified': r.c.hunt.input_effect_unverified = True
            await refocus(r)
            r.c.observer_last_at = 0
            frame = r.world.capture()
            target = await r.c.target_proposal(frame)
            assert inspection.availability(r.c, frame, target)['state'] != 'runnable'
            assert r.c.target_inspection_episode['id'] == old['id']
            assert not r.physical() and len(r.wire) == 1
        finally:
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation', ['configuration', 'input_generation', 'frame_source'])
def test_change_after_pause_cannot_rebind_inspection(tmp_path, mutation):
    async def run():
        r = ContractRig(tmp_path/mutation)
        try:
            old = await unresolved(r)
            r.c.pause_focus('clean_focus_lost')
            assert r.c.target_inspection_focus_resume
            if mutation == 'configuration': r.c.config['target_level_box'] = [1, 2, 3, 4]
            if mutation == 'input_generation': r.c.cycle._input_generation += 1
            assert r.c.resume_focus()
            frame = r.world.capture()
            if mutation == 'frame_source':
                from dataclasses import replace
                frame = replace(frame, source='different-source')
            await r.choose('world_normal_confirmed', frame=frame)
            assert r.c.target_inspection_episode['source_scope'] == old['source_scope']
            assert not r.events('grind_target_inspection_focus_rebound')
            assert not r.physical()
        finally:
            await r.close()
    asyncio.run(run())
