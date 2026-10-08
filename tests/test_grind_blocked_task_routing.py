"""Live-run regression: global reassessment must precede work forbidden while blocked."""
import asyncio
from copy import deepcopy
import time

import pytest
from PIL import Image, ImageDraw
from test_grind_loot_integration import LootRig


def low_health(r):
    original=r.world.capture
    def capture():
        frame=original()
        with Image.open(frame.image_path) as source:image=source.convert('RGB')
        draw=ImageDraw.Draw(image)
        draw.rectangle((40,30,140,40),fill='black')
        draw.rectangle((40,30,65,40),fill='green')
        image.save(frame.image_path)
        return frame
    r.world.capture=capture;r.c.capture=capture


@pytest.mark.parametrize('task', ['loot', 'resources'])
@pytest.mark.parametrize('reason', ['blocked_timing', 'blocked_provider'])
def test_recovered_provider_reassesses_before_resuming_pending_task(tmp_path, task, reason):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();r.c.hunt.phase='search'
            if task=='loot':
                r.c.config['loot_enabled']=True;r.death()
                await r.choose('loot_remaining')
                retained=deepcopy(r.c.loot.data)
            else:
                r.c.config.update(encounter_resources=True,preserve_target_heal=True)
                low_health(r)
                await r.choose('resources_unclear')
                r.c.resource_defer_until=0
            if reason=='blocked_timing':
                # Timeout entry/backoff is covered by the timing suite; this
                # replay begins with its retained four-failure blocked state.
                r.c.timing_failures=4
            r.c.enter_blocked(reason,r.c.last_signature)
            r.c.hunt.blocked.update(next_observation_at=0,assessment_due_at=0)
            before=(r.physical_keys(),r.casts())
            await r.choose('blocked_changed_assessment')
            assert r.c.hunt.blocked is None and (r.physical_keys(),r.casts())==before
            assert not r.c.hunt.credited_kills
            if task=='loot':
                assert r.c.loot.pending
                assert r.c.loot.data['death_sources']==retained['death_sources']
                assert r.c.loot.data['attempts']==retained['attempts']
                await r.choose('loot_inspect')
            else:
                await r.choose('heal_self')
                assert r.c.heal_pending and r.c.hunt.pending['purpose']=='heal_preserve'
            assert r.c.timing_failures==0
        finally:await r.close()
    asyncio.run(run())


def test_pending_loot_cannot_bypass_blocked_observation_cadence(tmp_path):
    async def run():
        r=LootRig(tmp_path)
        try:
            await r.start();r.c.hunt.phase='search';r.c.config['loot_enabled']=True;r.death()
            await r.choose('loot_remaining')
            r.c.enter_blocked('blocked_timing',r.c.last_signature)
            r.c.wait_until=0
            r.c.hunt.blocked['next_observation_at']=time.time()+15
            before=len(r.sage.calls);events=(r.physical_keys(),r.casts())
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_blocked'
            assert len(r.sage.calls)==before and (r.physical_keys(),r.casts())==events
            r.c.hunt.blocked.update(next_observation_at=0,assessment_due_at=0)
            await r.choose('blocked_still_unresolved')
            assert r.c.hunt.blocked and r.c.loot.pending
            assert (r.physical_keys(),r.casts())==events and not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())
