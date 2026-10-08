"""Live-run regression: stalled optional loot offers an honest exit, never timeout credit."""
import asyncio
from copy import deepcopy
import time

import pytest

from sage_wow.agent.grind_loot import PASSIVE_LOOT_SECONDS, loot_route_pending
from sage_wow.agent.looting import LootSweep
from test_grind_loot_integration import LootRig, scenario, combat_portrait


def elapsed(r, seconds):
    h = r.c.hunt
    h.active_seconds = r.c.loot.data['passive_clock']['active_at'] + seconds
    h.active_tick = None


async def clicked(r):
    r.backend.mouse_button = lambda *args: r.backend.events.append(('mouse', *args))
    await r.loot('corpse_ground_1')


@scenario
async def test_exact_boundary_replaces_passive_aliases_with_honest_exit(r):
    await r.loot('loot_remaining')
    sources = deepcopy(r.c.loot.data['death_sources'])
    elapsed(r, PASSIVE_LOOT_SECONDS - .001)
    await r.loot('loot_inspect')
    assert 'loot_unavailable' not in r.sage.calls[-1]['options']
    await r.loot('loot_remaining')
    elapsed(r, PASSIVE_LOOT_SECONDS)
    await r.loot('loot_unavailable')
    options = r.sage.calls[-1]['options']
    assert not {'loot_remaining', 'loot_inspect', 'loot_uncertain'} & options.keys()
    assert {'loot_search', 'corpse_ground_1'} <= options.keys()
    assert 'UNVERIFIED' in r.sage.calls[-1]['instructions']
    assert r.c.loot.data['outcome'] == 'unverified_after_passive_stall'
    assert r.c.loot.data['search_steps'] == r.c.loot.data['attempts'] == 0
    assert r.c.loot.data['death_sources'] == sources
    assert not loot_route_pending(r.c) and r.c.hunt.compact_stage == 'acquire'
    assert not r.c.hunt.credited_kills and not r.physical_keys()
    assert r.store.load_checkpoint('grind_loot')['loot_sweep']['outcome'] == 'unverified_after_passive_stall'


@scenario
async def test_search_at_153_seconds_preserves_original_recovery_deadline(r):
    await clicked(r)
    clock = deepcopy(r.c.loot.data['passive_clock'])
    elapsed(r, 153.6)
    await r.loot('loot_inspect')
    await r.loot('loot_search')
    assert 'loot_unavailable' not in r.sage.calls[-1]['options']
    assert r.c.loot.data['search_steps'] == 1
    guards = [e['payload'] for e in r.store.recent(30) if e['event_type'] == 'dispatch_guard_checked']
    assert guards[-1]['chosen'] == 'loot_search' and guards[-1]['approved']
    elapsed(r, 179.999)
    await r.loot('loot_remaining')
    assert 'loot_unavailable' not in r.sage.calls[-1]['options']
    assert r.c.loot.pending
    assert r.c.loot.data['passive_clock'] == clock
    elapsed(r, 180)
    await r.loot('loot_unavailable')
    assert r.c.loot.data['outcome'] == 'unverified_after_passive_stall'


@scenario
async def test_isolated_positives_do_not_reset_passive_clock(r):
    await clicked(r)
    stamp = deepcopy(r.c.loot.data['passive_clock'])
    for seconds in (60, 110, 160):
        elapsed(r, seconds)
        await r.loot('loot_verified')
        assert r.c.loot.data['confirmations'] == 1
        await r.loot('loot_inspect')
        await r.loot('loot_remaining')
        assert r.c.loot.data['confirmations'] == 0
    assert r.c.loot.data['passive_clock'] == stamp
    elapsed(r, 180)
    await r.loot('loot_unavailable')
    assert r.c.loot.data['outcome'] == 'unverified_after_passive_stall'
    assert not r.c.hunt.credited_kills


@scenario
async def test_consecutive_positive_click_verification_survives_deadline(r):
    await clicked(r)
    elapsed(r, 179.9)
    await r.loot('loot_verified')
    assert r.c.loot.pending and r.c.loot.data['confirmations'] == 1
    elapsed(r, 180)
    await r.loot('loot_verified')
    assert 'loot_unavailable' in r.sage.calls[-1]['options']
    assert r.c.loot.data['outcome'] == 'verified_looted'
    assert r.c.loot.data['attempts'] == 0 and not r.c.loot.pending
    assert not r.c.hunt.credited_kills


@scenario
async def test_expired_recovery_still_allows_fresh_guarded_retry(r):
    await r.loot(None)
    clock = deepcopy(r.c.loot.data['passive_clock'])
    elapsed(r, 180)
    await clicked(r)
    assert 'loot_unavailable' in r.sage.calls[-1]['options']
    guards = [e['payload'] for e in r.store.recent(30) if e['event_type'] == 'dispatch_guard_checked']
    assert guards[-1]['chosen'] == 'corpse_ground_1' and guards[-1]['approved']
    assert r.c.loot.pending and r.c.loot.data['stage'] == 'actions'
    await r.loot('loot_selected_corpse')
    assert 'loot_unavailable' in r.sage.calls[-1]['options']
    await r.loot('interact_target')
    assert 'loot_unavailable' in r.sage.calls[-1]['options']
    assert r.c.loot.data['attempts'] == 1 and r.c.loot.pending
    assert r.c.loot.data['passive_clock'] == clock
    await r.loot('loot_verified')
    assert r.c.loot.pending
    await r.loot('loot_verified')
    assert r.c.loot.data['outcome'] == 'verified_looted'
    assert not r.c.hunt.credited_kills


@scenario
async def test_completed_search_cannot_restart_expired_recovery_clock(r):
    await r.loot(None)
    clock = deepcopy(r.c.loot.data['passive_clock'])
    elapsed(r, 180)
    before = r.c.hunt.active_seconds
    r.c.hunt.active_tick = time.time() - 2
    await r.loot('loot_search')
    assert r.c.hunt.active_seconds >= before + 2
    assert r.c.loot.data['passive_clock'] == clock
    await r.loot(None)
    assert 'loot_unavailable' in r.sage.calls[-1]['options']


@scenario
async def test_pause_wall_time_does_not_advance_loot_deadline(r):
    await r.loot(None)
    elapsed(r, 179)
    # Focus pause clears active_tick; the first resumed tick excludes wall time.
    r.c.hunt.tick(time.time() + 600)
    await r.loot(None)
    assert 'loot_unavailable' not in r.sage.calls[-1]['options']
    assert r.c.loot.pending


@scenario
async def test_combat_recovery_and_new_obligation_start_fresh_passive_intervals(r):
    combat_portrait(r)
    await r.loot('under_attack')
    elapsed(r, 600)
    r.c.hunt.encounter_ended = True
    await r.loot('loot_remaining')
    assert 'loot_unavailable' not in r.sage.calls[-1]['options']
    elapsed(r, 180)
    await r.loot('recover_now')
    r.c.hunt.active_seconds += 600
    await r.loot('loot_remaining')
    assert 'loot_unavailable' not in r.sage.calls[-1]['options']
    elapsed(r, 180)
    r.death('second-obligation')
    await r.loot('loot_remaining')
    assert 'loot_unavailable' not in r.sage.calls[-1]['options']
    assert len(r.c.loot.data['death_sources']) == 2


@scenario
async def test_same_session_reload_retains_clock_but_new_epoch_restarts_it(r):
    await r.loot(None)
    clock = deepcopy(r.c.loot.data['passive_clock'])
    elapsed(r, 180)
    del r.c.loot
    del r.c.loot_state
    await r.loot(None)
    assert 'loot_unavailable' in r.sage.calls[-1]['options']
    assert r.c.loot.data['passive_clock'] == clock
    r.c.cycle.session_epoch = 'fresh-session'
    await r.loot('loot_remaining')
    assert 'loot_unavailable' not in r.sage.calls[-1]['options']


@pytest.mark.parametrize('mutation', ['source', 'new_request', 'action', 'clock', 'epoch', 'generation', 'unknown_input'])
def test_unverified_exit_revalidates_after_provider_await(tmp_path, mutation):
    async def run():
        r = LootRig(tmp_path)
        try:
            await r.start();r.death();await r.loot(None)
            elapsed(r, 180)
            async def change():
                sweep = r.c.loot.data
                if mutation == 'source':sweep['death_sources'][0]['receipt_id'] = 'different-cast'
                elif mutation == 'new_request':r.death('later-death')
                elif mutation == 'action':sweep['last_action'] = {'receipt_id': 'later-input'}
                elif mutation == 'clock':sweep['passive_clock']['active_at'] = r.c.hunt.active_seconds
                elif mutation == 'epoch':r.c.cycle.session_epoch = 'new-epoch'
                elif mutation == 'generation':r.c.cycle.input_generation += 1
                elif mutation == 'unknown_input':r.c.hunt.input_effect_unverified = True
            r.sage.hook = change
            result = await r.loot('loot_unavailable')
            assert result.status != 'dispatched'
            assert r.c.loot.pending and not r.c.loot.data.get('outcome')
            assert not r.physical_keys() and not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


@scenario
async def test_unverified_exit_rechecks_even_after_observation_receipt(r):
    await r.loot(None);elapsed(r, 180)
    original = r.c.decide
    async def late(*args, **kwargs):
        result = await original(*args, **kwargs)
        r.c.loot.data['death_sources'][0]['receipt_id'] = 'changed-after-receipt'
        return result
    r.c.decide = late
    await r.loot('loot_unavailable')
    assert r.c.loot.pending and not r.c.loot.data.get('outcome')
    assert any(e['event_type'] == 'grind_loot_unverified_exit_rejected' for e in r.store.recent(30))


@scenario
async def test_partial_retry_does_not_refresh_clock_or_drop_pending_loot(r):
    await r.loot(None);elapsed(r, 180)
    clock = deepcopy(r.c.loot.data['passive_clock'])
    original = r.backend.key
    def fail(code, down):
        if code == r.controls['turn_left']['keycode'] and down:
            raise RuntimeError('offline partial search input')
        original(code, down)
    r.backend.key = fail
    async def no_op():pass
    r.sage.hook = no_op
    result = await r.loot('loot_search')
    assert result.status != 'dispatched' and r.c.stopped
    assert r.c.loot.pending and r.c.loot.data['passive_clock'] == clock
    assert r.c.loot.data['search_steps'] == 0


def test_generic_sweep_does_not_infer_timeout_without_grind_authority():
    sweep = LootSweep({})
    sweep.suspect_kill('unknown-cast')
    assert 'loot_unavailable' not in {x.option for x in sweep.candidates()}
    sweep.observe('loot_unavailable', 'unsupported')
    assert sweep.pending and not sweep.data.get('outcome')
