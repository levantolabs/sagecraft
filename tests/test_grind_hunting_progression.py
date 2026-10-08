"""Level-three relocation, primary pulls and unchanged combat protections."""
import asyncio
from copy import deepcopy
import time

import pytest

from sage_wow.agent.grind_only import settings
from sage_wow.agent.grind_progression import deferred, search_entry_allowed
from sage_wow.agent.grind_search import HuntState, load_catalog
from test_grind_only import profile
from test_grind_progression_scouting import PolicyRig


ENTRY = {'primary_targets': ['Small Crag Boar', 'Burly Rockjaw Trogg', 'Rockjaw Trogg'],
         'area_ids': ['coldridge_west_boars', 'coldridge_southeast_troggs']}


def configure(r):
    r.c.profile.values['grind_only']['hunting_progression_by_player_level'] = {3: deepcopy(ENTRY), 4: deepcopy(ENTRY)}
    r.c.config.update(settings(r.c.profile))
    r.c.hunt.hunting_progression_by_player_level = deepcopy(r.c.config['hunting_progression_by_player_level'])


@pytest.mark.parametrize('bad', [None, [], {True: ENTRY}, {'three': ENTRY}, {3: {}},
    {3: {**ENTRY, 'area_ids': ['missing']}}, {3: {**ENTRY, 'primary_targets': []}},
    {3: {**ENTRY, 'primary_targets': ['Small Crag Boar', 'small crag boar']}},
    {3: ENTRY, '3': ENTRY}])
def test_bad_progression_configuration_rejected(tmp_path, bad):
    p = profile(tmp_path); p.values['grind_only']['hunting_progression_by_player_level'] = bad
    with pytest.raises(ValueError): settings(p)


def test_default_levels_and_catalog_remain_hypotheses(tmp_path):
    p = profile(tmp_path); catalog = load_catalog(p); h = HuntState(catalog, 'offline')
    assert h.candidates(1)[0]['id'] == 'cold_start_historical_patch_01'
    h.hunting_progression_by_player_level = {3: deepcopy(ENTRY), 4: deepcopy(ENTRY)}
    assert [a['id'] for a in h.candidates(3)] == ENTRY['area_ids']
    assert all(not a['source']['current_population_verified'] for a in h.candidates(3))
    h.last_level = 2; h.level_changed(3); assert h.progression_pending
    h.progression_pending = False; h.level_changed(4); assert not h.progression_pending
    h.learned['wolf'] = {**catalog['cold_start_historical_patch_01'], 'id': 'wolf', 'observed_target_names': ['Ragged Young Wolf']}
    h.learned['boar'] = {**catalog['coldridge_west_boars'], 'id': 'boar', 'observed_target_names': ['Small Crag Boar']}
    assert 'wolf' not in {a['id'] for a in h.candidates(3)}
    assert 'boar' in {a['id'] for a in h.candidates(3)}


def test_fresh_level3_continuation_plans_and_travels_away_from_wolf_patch(tmp_path):
    async def run():
        r = PolicyRig(tmp_path)
        try:
            await r.start_policy(soft=True); configure(r); h = r.c.hunt
            r.zone = 'Coldridge Valley'; r.position = [28.4,74.9]
            h.last_level = None; h.level_changed(3); r.world.name = ''; r.world.target_level = None
            await r.choose('choose_area:coldridge_west_boars')
            choices = r.sage.calls[-1]['options']
            assert all('wolf' not in n for n in choices if n.startswith('choose_area:'))
            assert 'search_here' not in choices
            assert h.phase == 'travel' and h.plan['area']['coordinate'] == [22.8, 72.5]
            assert 'Small Crag Boar' in r.sage.calls[-1]['prompt']
            await r.choose(None)
            assert 'begin_hunt' not in r.sage.calls[-1]['options']
            assert 'probe_forward' in r.sage.calls[-1]['options']
            assert not r.casts() and not h.credited_kills
        finally: await r.close()
    asyncio.run(run())


def test_new_level_strategy_waits_for_ongoing_fight(tmp_path):
    async def run():
        r = PolicyRig(tmp_path)
        try:
            await r.start_policy(soft=True)
            r.world.name = 'Ragged Young Wolf'; r.world.target_level = 1
            await r.choose('attack_mob_level_1')
            configure(r); h = r.c.hunt
            h.last_level = 2; h.level_changed(3)
            await r.choose('damaged_alive')
            before = len(r.casts())
            await r.choose('attack_mob_level_1')
            assert len(r.casts()) > before and h.progression_pending
            assert h.phase == 'fight' and not h.credited_kills
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('name,mob,allowed', [('Ragged Young Wolf',1,False),
    ('Small Crag Boar',3,True), ('Burly Rockjaw Trogg',2,True), ('Rockjaw Trogg',1,True),
    ('Small Crag Boar',5,False)])
def test_primary_target_menu_and_cast_guard_keep_exact_level_limits(tmp_path, name, mob, allowed):
    async def run():
        r = PolicyRig(tmp_path)
        try:
            await r.start_policy(soft=True); configure(r)
            r.world.name = name; r.world.target_level = mob
            await r.choose(f'attack_mob_level_{mob}' if allowed else None)
            options = r.sage.calls[-1]['options']
            assert bool({n for n in options if n.startswith('attack_mob_level_')}) == allowed
            assert bool(r.casts()) == allowed
            if allowed:
                assert r.c.hunt.latest_patch['observed_target_names'] == [name]
            assert not r.c.hunt.credited_kills
        finally: await r.close()
    asyncio.run(run())


def test_nonprimary_clear_verifies_then_returns_to_region_selection(tmp_path):
    async def run():
        r = PolicyRig(tmp_path)
        try:
            await r.start_policy(soft=True); configure(r)
            r.world.name = 'Ragged Young Wolf'; r.world.target_level = 1
            await r.choose('reject_selected_target')
            assert r.c.hunt.pending['family'] == 'clear'
            assert r.c.hunt.strategy_required['reason'] == 'level_progression'
            r.world.name = ''; r.world.target_level = None
            await r.choose('target_cleared')
            await r.choose('choose_area:coldridge_southeast_troggs')
            assert r.c.hunt.phase == 'travel' and not r.casts()
        finally: await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('reason', ['ongoing_fight','current_attack','stale_attack'])
def test_strategy_preserves_current_combat_but_not_stale_threat(tmp_path, reason):
    async def run():
        r = PolicyRig(tmp_path)
        try:
            await r.start_policy(soft=True); configure(r)
            h = r.c.hunt; target = {'name': 'Ragged Young Wolf'}
            if reason == 'ongoing_fight': h.last_target = 'ragged young wolf'; h.encounter_ended = False
            else:
                from sage_wow.agent.grind_encounter_recovery import capture_scope
                frame=r.world.capture()
                h.active_threat = {'kind':'current_incoming_attack','scope':capture_scope(r.c,frame),
                    'occurred_at': time.time() - (20 if reason=='stale_attack' else 1)}
            assert deferred(r.c,target,frame if reason!='ongoing_fight' else None) == (reason=='stale_attack')
        finally: await r.close()
    asyncio.run(run())


def test_arrival_primary_sighting_and_visual_search_are_distinct(tmp_path):
    async def run():
        r = PolicyRig(tmp_path)
        try:
            await r.start_policy(soft=True); configure(r); h = r.c.hunt
            h.plan = {'area': {'coordinate': [22.8,72.5]}}
            wolf = {'name':'Ragged Young Wolf','eligibility':'eligible',
                'visual_observation':{'selected_hud':'present','life_state':'alive'}}
            far = {'available':True,'inside_radius':False}
            assert not search_entry_allowed(r.c,wolf,far,{'changed':True})
            assert search_entry_allowed(r.c,wolf,{'available':True,'inside_radius':True},None)
            assert search_entry_allowed(r.c,{**wolf,'name':'Small Crag Boar'},far,None)
            assert not search_entry_allowed(r.c,{**wolf,'name':'Small Crag Boar','eligibility':'unknown'},far,None)
            assert not search_entry_allowed(r.c,wolf,{'available':False},{'changed':True})
            h.plan['area']['coordinate'] = None
            assert search_entry_allowed(r.c,wolf,{'available':False},{'changed':True})
            assert not search_entry_allowed(r.c,wolf,{'available':False},None)
        finally: await r.close()
    asyncio.run(run())


def test_automatic_opener_cannot_pull_nonprimary_creature(tmp_path):
    from test_grind_acquisition_opening import enable, acquire, process, smites
    from test_grind_product_spec import Rig
    async def run():
        r = Rig(tmp_path)
        try:
            enable(r); await r.start(); await acquire(r)
            r.c.hunt.hunting_progression_by_player_level = {1: deepcopy(ENTRY)}
            r.sage.answers.append(None)
            await process(r)
            assert not smites(r)
            events=[e['payload'] for e in r.store.recent(50) if e['event_type']=='grind_target_opener_result']
            assert 'primary hunting strategy' in events[-1]['reason']
        finally: await r.close()
    asyncio.run(run())


def test_final_dispatch_guard_rejects_nonprimary_pull(tmp_path):
    async def run():
        r = PolicyRig(tmp_path)
        try:
            await r.start_policy(soft=True); configure(r)
            r.world.name = 'Ragged Young Wolf'; r.world.target_level = 1
            frame = r.world.capture(); target = await r.c.target_proposal(frame)
            guard = await r.c.guard(frame,target,exact_level=1)
            checked = await guard(frame)
            assert not checked.approved and 'hunting_strategy' in checked.evidence['failed_predicates']
            assert not r.casts()
        finally: await r.close()
    asyncio.run(run())
