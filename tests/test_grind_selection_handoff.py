"""Current selection tasks retain history without inheriting its authority."""
import asyncio
from copy import deepcopy

import pytest

from test_grind_progression_scouting import PolicyRig
from test_grind_hunting_progression import configure
from test_grind_startup_acquisition import fresh_start
from test_grind_travel_recovery import events


@pytest.mark.parametrize('stage',['acquire','recovery','planning'])
def test_historical_presence_does_not_require_absence_before_idle_scouting(tmp_path,stage):
    async def run():
        r=PolicyRig(tmp_path)
        try:
            h=await fresh_start(r)
            h.selected_presence=True
            h.progression_pending=False;h.compact_stage=stage
            h.phase='choose_area' if stage=='planning' else 'search'
            h.planning_requested=stage=='planning'
            h.action_failures['old-combat-method']=2
            debt=deepcopy((h.action_failures,h.target_history,h.confirmed_target_absences))
            r.sage.hook=lambda: assert_historical_presence(h)
            await r.choose('choose_area:coldridge_southeast_troggs' if stage=='planning' else 'target_enemy')
            assert events(r,'grind_compact_question')[-1]['concrete_action_menu']
            assert 'cannot_assess' not in r.sage.calls[-1]['options']
            assert h.selected_presence is (True if stage=='planning' else None)
            assert (h.action_failures,h.target_history,h.confirmed_target_absences)==debt
            assert not r.casts() and not h.credited_kills
        finally:await r.close()
    asyncio.run(run())


async def assert_historical_presence(h):
    assert h.selected_presence is True and not h.confirmed_target_absences


@pytest.mark.parametrize('outcome',['target_cleared','clear_failed'])
@pytest.mark.parametrize('exhausted',[False,True])
def test_prior_death_cannot_replace_pending_rejected_selection_feedback(tmp_path,outcome,exhausted):
    async def run():
        r=PolicyRig(tmp_path)
        try:
            await r.start_policy(soft=True);configure(r)
            r.world.name='Rockjaw Trogg';r.world.target_level=2
            await r.choose('attack_mob_level_2')
            r.world.health='black';r.life='dead'
            await r.choose('dead_credited')
            h=r.c.hunt;assert h.target_dead_observed and h.encounter_ended
            owner=h.combat_history_key;history=deepcopy(h.target_history[owner]);kills=deepcopy(h.credited_kills)
            r.world.name='';r.world.target_level=None
            await r.choose('target_enemy')
            r.world.name='Ragged Young Wolf';r.world.target_level=1;r.world.health='green';r.life='alive'
            await r.choose('reject_selected_target')
            assert h.pending['family']=='clear' and h.target_dead_observed
            clear=deepcopy(h.pending);before=len(r.physical_keys());panels=[]
            if exhausted:
                from sage_wow.agent.grind_inspection import _episode
                # Isolated exhausted-state fixture; a pending clear admits no observer.
                r.c.target_inspection_episode=_episode(r.c,r.world.capture(),{'name':r.world.name})
                r.c.target_inspection_episode.update(attempts=2,exhausted=True)
            original=r.c.prepare_image
            async def prepare(*args,**kwargs):
                panels.append(kwargs)
                return await original(*args,**kwargs)
            r.c.prepare_image=prepare
            if outcome=='target_cleared':r.world.name='';r.world.target_level=None
            await r.choose(outcome)
            assert events(r,'grind_compact_question')[-1]['stage']=='clear'
            call=r.sage.calls[-1]
            assert call['instructions']=='Did the rejected selection clear?'
            assert 'CURRENT selected-target' in call['prompt']
            assert 'Recent official combat log facts' not in call['prompt']
            assert 'inspect_recent_corpse' not in call['options']
            assert 'change_search_strategy' not in call['options']
            assert panels[-1]['history']==((clear['source_image'],clear['source_hash'],
                'BEFORE CLEAR — HISTORICAL REJECTED SELECTION',36),)
            assert not panels[-1]['omit_target']
            assert h.pending is None and len(r.physical_keys())==before
            assert h.credited_kills==kills and h.target_history[owner]==history
            assert h.outcomes[-1]['outcome']==outcome
            assert h.selected_presence is (False if outcome=='target_cleared' else None)
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['focus','generation'])
def test_pending_clear_still_requires_current_receipt_authority(tmp_path,change):
    from test_grind_product_spec import Rig
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();await r.choose('reject_selected_target')
            h=r.c.hunt;h.target_dead_observed=True
            r.world.name=''
            async def interrupt():
                if change=='focus':r.c.pause_focus()
                else:r.c.cycle._input_generation+=1
            r.sage.hook=interrupt
            keys=list(r.physical_keys());r.sage.answers.append('target_cleared');r.c.wait_until=0
            result=await r.c.process(r.world.capture())
            assert result.status!='dispatched' and r.physical_keys()==keys
            assert not any(x.get('outcome')=='target_cleared' for x in h.outcomes)
        finally:await r.close()
    asyncio.run(run())


def test_actual_partial_clear_stops_before_feedback_or_acquisition(tmp_path):
    from test_grind_product_spec import Rig
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start()
            def fail(*args):raise RuntimeError('offline partial Escape dispatch')
            r.backend.key=fail
            r.sage.answers.append('reject_selected_target');r.c.wait_until=0
            await r.c.process(r.world.capture())
            assert r.c.stopped and r.c.reason=='partial_or_unknown_grind_input'
            assert not r.casts() and not any(x.get('outcome')=='target_cleared' for x in r.c.hunt.outcomes)
        finally:await r.close()
    asyncio.run(run())
