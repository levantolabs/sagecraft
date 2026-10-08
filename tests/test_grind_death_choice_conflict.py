"""Own living HUD evidence must constrain semantic player-death claims."""
import asyncio
from pathlib import Path

import pytest
from PIL import Image

from sage_wow.agent.cycle import ActionCandidate
from sage_wow.agent.grind_resources import hud_resources
from test_grind_only import setup, cleanup


ALIVE=Path(__file__).parent/'fixtures/loot_translucent_hud/player_alive.png'


def living_frame(c,frames):
    regions=c.profile.values['calibration']['ui_layout']['regions']
    regions['player_health']=[40,30,285,58]
    regions['player_frame']=[0,0,300,90]
    frame=frames.capture()
    with Image.open(frame.image_path) as raw:image=raw.convert('RGB')
    with Image.open(ALIVE) as bar:image.paste(bar,(40,30))
    image.save(frame.image_path)
    assert hud_resources(c,frame)['player_health']>0
    return frame


@pytest.mark.parametrize('death_option',['dead_or_unrecoverable','player_dead'])
def test_current_living_bar_withholds_death_even_with_stale_empty_cached_hud(tmp_path,death_option):
    async def scenario():
        c,f,s,b,store,e=setup(tmp_path,['cannot_assess']);e.arm()
        try:
            frame=living_frame(c,f)
            c.current_hud={'frame_id':'old-frame','player_health':0,'health_confidence':1}
            options=[ActionCandidate(death_option,'Own player is dead',{'type':'observe_only'}),
                     ActionCandidate('player_alive','Own living state',{'type':'observe_only'}),
                     ActionCandidate('cannot_assess','Uncertain; observe',{'type':'observe_only'})]
            result=await c.decide(frame,frame.image_path,'Classify the own player, not a corpse.',
                'Observe only.',options,level_read=death_option=='player_dead')
            assert result.status=='dispatched' and not c.stopped
            assert death_option not in s.calls[-1]['options']
            assert 'cannot_assess' in s.calls[-1]['options']
            assert not [event for event in b.events if event[0] in {'key','text'}]
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_combat_feedback_with_corpse_text_keeps_living_player_in_session(tmp_path):
    from test_grind_survival_guard import SurvivalRig
    async def scenario():
        r=SurvivalRig(tmp_path)
        try:
            r.health=1
            await r.start()
            r.c.config['committed_combat']=True
            await r.choose('attack_mob_level_1')
            casts=list(r.casts())
            r.world.error='Corpse'
            await r.choose('cannot_assess')
            assert 'dead_or_unrecoverable' not in r.sage.calls[-1]['options']
            assert not r.c.stopped and r.casts()==casts
            assert r.c.hunt.pending is None or r.c.hunt.pending['purpose']=='cast'
        finally:await r.close()
    asyncio.run(scenario())


def test_explicit_own_death_modal_still_stops_with_positive_health_pixels(tmp_path):
    from test_grind_survival_guard import SurvivalRig
    async def scenario():
        r=SurvivalRig(tmp_path)
        try:
            r.health=1;r.ui='death'
            frame=r.world.capture()
            assert hud_resources(r.c,frame)['player_health']>0
            result=await r.c.process(frame)
            assert result.status=='grind_stopped' and r.c.reason=='own_death_modal_observed'
            assert not r.sage.calls and not r.physical_keys() and not r.casts()
        finally:await r.close()
    asyncio.run(scenario())


def test_stale_living_cache_does_not_hide_death_with_unreadable_current_bar(tmp_path):
    async def scenario():
        c,f,s,b,store,e=setup(tmp_path,['cannot_assess']);e.arm()
        try:
            frame=f.capture()
            c.current_hud={'frame_id':'old-frame','player_health':1,'health_confidence':1}
            options=[ActionCandidate('player_dead','Own death',{'type':'observe_only'}),
                     ActionCandidate('cannot_assess','Uncertain',{'type':'observe_only'})]
            assert hud_resources(c,frame)['player_health'] is None
            await c.decide(frame,frame.image_path,'Read own state.','Observe only.',options)
            assert 'player_dead' in s.calls[-1]['options'] and not c.stopped
        finally:await cleanup(e,store)
    asyncio.run(scenario())
