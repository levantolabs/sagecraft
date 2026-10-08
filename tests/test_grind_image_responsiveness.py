"""Real event-loop scheduling with slow fake image work and fake game input."""
import asyncio
import contextlib
import threading
import time

import pytest

from sage_wow.agent import grind_only,grind_observation,grind_motion_evidence
from test_grind_guild_invitation import attach_invitation
from test_grind_product_spec import Rig
from test_grind_travel_acceptance import TravelRig


@pytest.mark.parametrize('route',['ui_pair','combat','feedback','focused','travel','clean_travel','level'])
def test_slow_image_composition_keeps_live_supervisor_responsive(tmp_path,monkeypatch,route):
    async def run():
        travel=route in {'travel','clean_travel'}
        r=TravelRig(tmp_path,clean=route=='clean_travel') if travel else Rig(tmp_path)
        pulse=None
        try:
            if travel:await r.travel()
            else:await r.start()
            answer='attack_mob_level_1';owner=grind_only;name='composed_evidence_image'
            if route=='ui_pair':
                visible=attach_invitation(r)
                r.c.require_world=True;await r.choose('close_visible_ui')
                visible[0]=False;answer='world_normal_confirmed'
            elif route=='feedback':
                await r.choose('attack_mob_level_1');r.world.health='orange';answer='own_damaged_alive'
            elif route=='focused':
                r.c.config['turn_seconds']=.05
                await r.choose('turn_left');answer='motion_useful'
                owner,name=grind_motion_evidence,'motion_evidence_image'
            elif travel:
                answer='begin_hunt'
                owner,name=(grind_observation.CleanSnapshot,'image') if route=='clean_travel' else (grind_only,'travel_image')
            elif route=='level':
                r.c.level.last_attempt_at=0;answer='player_level_1';name='player_level_assessment_image'
            original=getattr(owner,name);threads=[]
            def slow(*args,**kwargs):
                threads.append(threading.get_ident())
                time.sleep(.35)
                return original(*args,**kwargs)
            monkeypatch.setattr(owner,name,slow)
            beats=[]
            async def heartbeat():
                while True:
                    beats.append(time.monotonic());r.executor.heartbeat()
                    await asyncio.sleep(.01)
            r.executor.heartbeat();r.executor.heartbeat_timeout=.18
            pulse=asyncio.create_task(heartbeat());await asyncio.sleep(0)
            result=await r.choose(answer)
            assert result.status=='dispatched' and r.executor.armed
            assert threads and set(threads)!={threading.get_ident()}
            assert len(beats)>=12
            assert max(b-a for a,b in zip(beats,beats[1:]))<.18
            assert r.executor.last_watchdog_trip is None
            assert any(event['payload'].get('elapsed_seconds',0)>=.35
                for event in r.store.recent(80) if event['event_type']=='grind_image_composition')
        finally:
            if pulse:
                pulse.cancel()
                with contextlib.suppress(asyncio.CancelledError):await pulse
            await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('invalidate',['pause','generation','stop','input_disarm'])
def test_authority_changes_during_image_work_never_reach_provider_or_input(tmp_path,monkeypatch,invalidate):
    async def run():
        r=Rig(tmp_path);entered=threading.Event();finish=threading.Event();task=None
        try:
            await r.start()
            original=grind_only.composed_evidence_image
            def slow(*args,**kwargs):
                entered.set()
                assert finish.wait(2)
                return original(*args,**kwargs)
            monkeypatch.setattr(grind_only,'composed_evidence_image',slow)
            calls=len(r.sage.calls);inputs=list(r.backend.events)
            r.sage.answers.append('attack_mob_level_1')
            task=asyncio.create_task(r.c.process(r.world.capture()))
            assert await asyncio.to_thread(entered.wait,1)
            if invalidate=='pause':r.c.pause_focus()
            elif invalidate=='generation':r.c.cycle._input_generation+=1
            elif invalidate=='stop':r.c.stop('operator_stop')
            else:await r.executor.stop('operator_stop')
            finish.set();result=await task
            assert result.status=='scope_rejected'
            assert len(r.sage.calls)==calls and not r.casts()
            assert not [item for item in r.backend.events[len(inputs):] if item[0] in {'key','text'}]
            assert not list(tmp_path.glob('grind-view-*'))
            if invalidate=='input_disarm':
                assert not r.executor.armed and r.backend.events[-1]==('release',)
        finally:
            finish.set()
            if task and not task.done():await task
            await r.close()
    asyncio.run(run())
