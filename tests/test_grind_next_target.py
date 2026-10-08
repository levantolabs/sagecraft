"""Cycle past unsuitable creatures through fake input and fresh HUD guards."""
import asyncio
from copy import deepcopy

import pytest

from test_grind_hunting_progression import configure
from test_grind_progression_scouting import PolicyRig


async def setup(tmp_path):
    r=PolicyRig(tmp_path)
    await r.start_policy(4,soft=True);configure(r)
    r.c.config['target_names']=['Burly Rockjaw Trogg','Rockjaw Trogg']
    r.c.config['target_opening_cast']=True
    r.world.name='Ragged Young Wolf';r.world.target_level=1
    return r


def test_cycle_preserves_selection_until_tab_then_requires_fresh_attack_choice(tmp_path):
    async def run():
        r=await setup(tmp_path)
        try:
            h=r.c.hunt;before=deepcopy(h.rejected_signatures)
            await r.choose('select_next_target')
            assert r.physical_keys()==[r.controls['target_enemy']['keycode']]
            assert h.pending['family']=='target' and h.pending['action']=='target_enemy'
            assert h.rejected_signatures==before and not r.casts()
            assert not getattr(r.c,'target_opener',None) and not h.credited_kills
            # The result may still be the same unsuitable creature; no cast.
            await r.choose(None)
            assert not any(n.startswith('attack_mob_level_') for n in r.sage.calls[-1]['options'])
            assert not r.casts()
            r.world.name='Rockjaw Trogg';r.world.target_level=2
            await r.choose('attack_mob_level_2')
            assert r.casts() and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('continuity',['current','stale','missing'])
def test_retained_observer_fact_requires_current_continuity_frame(tmp_path,continuity):
    async def run():
        r=await setup(tmp_path)
        try:
            original=r.c.target_proposal
            async def retained(frame):
                target=await original(frame)
                provenance={**target['visual_provenance'],'frame_id':'original-observer-frame'}
                if continuity!='missing':
                    provenance['continuity_frame_id']=frame.frame_id if continuity=='current' else 'old-continuity-frame'
                return {**target,'visual_provenance':provenance}
            r.c.target_proposal=retained
            await r.choose('select_next_target' if continuity=='current' else None)
            assert ('select_next_target' in r.sage.calls[-1]['options'])==(continuity=='current')
            assert bool(r.physical_keys())==(continuity=='current')
            assert not r.casts()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['primary','player','dead','empty','unknown_life','current_threat','ongoing_fight'])
def test_cycle_does_not_replace_existing_selected_work(tmp_path,fault):
    async def run():
        r=await setup(tmp_path)
        try:
            h=r.c.hunt
            if fault=='primary':r.world.name='Rockjaw Trogg';r.world.target_level=2
            if fault=='player':r.kind='player'
            if fault=='dead':r.life='dead';r.world.health='black'
            if fault=='empty':r.world.name='';r.world.target_level=None
            if fault=='unknown_life':r.life='unknown'
            if fault=='current_threat':
                from sage_wow.agent.grind_encounter_recovery import capture_scope
                import time
                h.active_threat={'kind':'current_incoming_attack','scope':capture_scope(r.c,r.world.capture()),'occurred_at':time.time()}
            if fault=='ongoing_fight':h.encounter_ended=False;h.last_target='ragged young wolf'
            await r.choose(None)
            assert 'select_next_target' not in r.sage.calls[-1]['options']
            assert not r.physical_keys() and not r.casts()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault',['selection','focus','input_unknown','cooldown','policy'])
def test_cycle_rechecks_current_authority_after_sage(tmp_path,fault):
    async def run():
        r=await setup(tmp_path)
        try:
            async def mutate():
                h=r.c.hunt
                if fault=='selection':r.world.name='Rockjaw Trogg'
                if fault=='focus':r.c.pause_focus()
                if fault=='input_unknown':h.input_effect_unverified=True
                if fault=='cooldown':h.failures['target']=2;h.last_completed_target_active_at=h.active_seconds
                if fault=='policy':h.hunting_progression_by_player_level[4]['primary_targets'].append('Ragged Young Wolf')
            r.sage.hook=mutate
            result=await r.choose('select_next_target')
            assert 'select_next_target' in r.sage.calls[-1]['options']
            assert result.status!='dispatched' and not r.physical_keys() and not r.casts()
        finally:await r.close()
    asyncio.run(run())
