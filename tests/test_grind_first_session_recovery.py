"""First-session active entry and exhausted survival; real controller with fake IO."""
import asyncio
from dataclasses import replace

import pytest

from sage_wow.agent.grind_ui import active_text_entry
from sage_wow.agent.grind_perception import read_hud_scene
from test_grind_survival_guard import SurvivalRig
from test_grind_trial35_recovery import entry_world, row


def entry(r, label, *, opened=True):
    state=entry_world(r,opened=opened)
    previous=r.c.ocr
    def ocr(path):
        return [replace(item,text=label,bounds={**item.bounds,'width':120 if len(label)>5 else 25})
                if item.text=='Say:' else item for item in previous(path)]
    r.c.ocr=ocr
    return state


@pytest.mark.parametrize('label',['Say:', 'Say', 'Say: /cast [harm,nodead] Smite',
    'Tell', 'Tell:', 'Tell Friend: /cast [@player] Lesser Heal'])
def test_entry_closes_before_critical_health_heal_and_preserves_target(tmp_path,label):
    async def run():
        r=SurvivalRig(tmp_path)
        try:
            await r.start();r.enable_resources();r.world.target_level=1
            state=entry(r,label)
            await r.choose('close_visible_ui')
            assert not state['text'] and not state['open'] and state['escapes']==1
            assert r.world.name=='Young Wolf' and not r.c.heal_pending
            assert r.c.hunt.pending['family']=='ui' and not r.c.hunt.failures['recovery']
            await r.choose('world_normal_confirmed')
            assert not r.c.hunt.failures['clear'] and not r.c.hunt.credited_kills
            await r.choose('heal_self')
            assert state['text']==[{'value':'/cast [@player] Lesser Heal','entry_open':True}]
            assert r.world.name=='Young Wolf' and r.c.heal_pending
            assert not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('label',['Say: occupied', 'Say', 'Tell Friend: occupied'])
def test_entry_appearing_during_heal_decision_rejects_before_input(tmp_path,label):
    async def run():
        r=SurvivalRig(tmp_path)
        try:
            await r.start();r.enable_resources();state=entry(r,label,opened=False)
            async def appear():state['open']=True
            r.sage.hook=appear
            result=await r.choose('heal_self')
            assert result.status=='dispatch_guard_rejected' and not state['text']
            assert not r.c.heal_pending and not r.c.hunt.failures['recovery']
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('label',['Say', 'Say: occupied', 'Tell Friend: occupied'])
def test_shared_full_and_focused_entry_fact_and_negative_controls(tmp_path,label):
    async def run():
        r=SurvivalRig(tmp_path)
        try:
            state=entry(r,label);frame=r.world.capture();rows=r.c.ocr(frame.image_path)
            observed=active_text_entry(frame,rows)
            assert observed and observed['bounds']==next(x.bounds for x in rows if x.text==label)
            scene=read_hud_scene(frame,ui_layout=r.c.profile.calibration['ui_layout'],ocr=r.c.ocr)
            assert scene.active_text_entry
            for bad in [row('Someone says: hello'),row('Friend whispers: hi'),row('Tell me about this'),
                        row('Say',y=150),replace(row('Say'),confidence=.6)]:
                assert active_text_entry(frame,[bad]) is None
            state['open']=False;clean=r.world.capture()
            assert active_text_entry(clean,[row(label)]) is None
        finally:await r.close()
    asyncio.run(run())


def test_two_actual_failed_heals_keep_urgent_recovery_through_failed_escape_and_ui(tmp_path,monkeypatch):
    async def run():
        r=SurvivalRig(tmp_path)
        try:
            from test_grind_run02_settlement import virtual_clock
            clock=virtual_clock(r,monkeypatch)
            await r.start();r.enable_resources();r.world.target_level=1
            for _ in range(2):
                await r.choose('heal_self');r.c.wait_until=0
                clock['offset']+=6.1  # A real bounded observation, not the first early frame.
                assert (await r.c.process(r.world.capture())).status=='grind_reobserve'
            assert r.c.hunt.failures['recovery']==2
            debt=dict(r.c.hunt.action_failures)
            await r.choose('backward')
            assert r.c.hunt.phase=='recover'
            assert not {'heal_self','recovered_resume','attack_mob_level_1'} & r.sage.calls[-1]['options'].keys()
            await r.choose('motion_no_useful_effect')
            assert r.c.hunt.failures['recovery']==2
            motion_debt=dict(r.c.hunt.action_failures)
            state=entry(r,'Tell Friend: occupied')
            await r.choose('close_visible_ui');await r.choose('world_normal_confirmed')
            await r.choose(None)
            assert r.c.hunt.phase=='recover'
            assert not {'heal_self','recovered_resume','attack_mob_level_1'} & r.sage.calls[-1]['options'].keys()
            assert all(r.c.hunt.action_failures.get(k)==v for k,v in motion_debt.items())
            assert all(r.c.hunt.action_failures.get(k)==v for k,v in debt.items())
            assert len([x for x in r.casts() if x[1]=='/cast [@player] Lesser Heal'])==2
            r.health=.8
            await r.choose('recovered_resume')
            assert r.c.hunt.phase!='recover' and r.c.hunt.failures['recovery']==2
        finally:await r.close()
    asyncio.run(run())
