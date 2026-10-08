"""Live-run regression: an absence fact before Tab must not veto a later acquisition."""
import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sage_wow.agent.grind_perception import inspect
from test_grind_acquisition_opening import enable, acquire, process, smites
from test_grind_level3_recovery import target_observer
from test_grind_product_spec import Rig


async def absent_observation(r):
    r.c.config['selected_target_observer'] = {
        'enabled': True, 'backend': 'sage', 'model': 'offline', 'timeout_seconds': 2}
    async def absent(path, **kwargs):
        result = await target_observer()(path, **kwargs)
        result['observation'] = {'selected_hud': 'absent', 'name': None,
            'level': None, 'target_kind': 'unknown', 'life_state': 'unknown'}
        return result
    r.c.target_observer = absent
    frame = r.world.capture()
    assert await inspect(r.c, frame, await r.c.raw_target_proposal(frame))
    return r.c.target_observation


def test_real_tab_after_observed_absence_reaches_single_opener_then_sage(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            enable(r); await r.start(); r.world.name = ''
            old = await absent_observation(r)
            assert (await r.c.target_proposal(r.world.capture()))['eligibility'] == 'absent'
            completed = deepcopy(r.c.target_inspection_episode)
            assert completed['disposition'] == 'source_resolved_absent'
            assert completed['attempts'] == 1 and not completed['exhausted']
            tab = await acquire(r)
            assert old['generation'] != tab['generation_after']
            calls = len(r.sage.calls)
            result = await process(r)
            assert result.status == 'dispatched' and result.decision is None
            assert result.receipt['authorization_id'] == tab['receipt_id']
            assert len(smites(r)) == 1 and len(r.sage.calls) == calls
            retired = [json.loads(row[0]) for row in r.store.connection.execute(
                "SELECT payload_json FROM events WHERE event_type='grind_target_inspection_retired'")]
            handoff = [event for event in retired
                       if event.get('reason') == 'authenticated_new_selected_sensing_task']
            assert len(handoff) == 1 and handoff[0]['id'] == completed['id']
            assert handoff[0]['sensing_proof']['receipt_id'] == tab['receipt_id']
            assert not handoff[0]['combat_allowance_renewed']
            assert r.c.target_inspection_episode is None  # Successful opener retires sensing.
            assert not r.c.target_observation
            await r.choose('damaged_alive')
            assert len(r.sage.calls) == calls + 1 and len(smites(r)) == 1
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['generation', 'scope', 'age', 'source', 'hash'])
def test_retired_observation_cannot_create_new_semantic_conflict(tmp_path, change):
    async def run():
        r = Rig(tmp_path)
        try:
            enable(r); await r.start(); r.world.name = ''
            old = await absent_observation(r)
            assert (await r.c.target_proposal(r.world.capture()))['eligibility'] == 'absent'
            completed = deepcopy(r.c.target_inspection_episode)
            r.world.name = 'Young Wolf'
            frame = r.world.capture()
            if change == 'generation': r.c.cycle._input_generation += 1
            elif change == 'scope': r.c.cycle.invalidate('offline changed scope')
            elif change == 'age': old['result']['captured_at'] = (datetime.now(timezone.utc)-timedelta(seconds=40)).isoformat()
            elif change == 'source': frame = replace(frame, source='different-offline-window')
            else: old['source_sha256'] = 'changed-original-proof'
            target = await r.c.target_proposal(frame)
            assert not target.get('observation_conflict')
            # Historical completion remains to prevent another automatic
            # empty-source episode; its retired projection supplies no fact.
            assert r.c.target_inspection_episode == completed
            assert completed['disposition'] == 'source_resolved_absent'
            assert completed['attempts'] == 1 and not completed['exhausted']
            assert r.c.target_observation is None
            assert not smites(r)  # Retiring a fact never supplies input authority.
        finally: await r.close()
    asyncio.run(run())


def test_current_conflicting_absence_with_known_creature_uses_bounded_recovery(tmp_path):
    async def run():
        r = Rig(tmp_path)
        try:
            enable(r); await r.start()
            await absent_observation(r)  # Provider contradicts this same visible HUD.
            target = await r.c.target_proposal(r.world.capture())
            assert target.get('observation_conflict')
            assert target['eligibility'] == 'unknown'
            episode = r.c.target_inspection_episode
            r.c.hunt.active_seconds += 61
            r.c.observer_last_at = 0
            await r.choose('change_search_strategy')
            assert episode['exhausted'] and r.c.hunt.planning_requested
            assert not any(k.startswith('attack_mob_level_') for k in r.sage.calls[-1]['options'])
            assert not smites(r)
        finally: await r.close()
    asyncio.run(run())
