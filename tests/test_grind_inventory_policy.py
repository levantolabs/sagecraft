"""Goal-scoped full-bag recovery with fake capture, input and Sage only."""
import asyncio
from datetime import datetime, timezone

from sage_wow.perception.ocr import TextObservation
from test_grind_loot_integration import LootRig


def seed(character='Test Player', goal=3):
    return {'state':'suppressed_inventory_full','character':character,'goal_level':goal,
            'evidence':{'source':'user_report','reason':'inventory_full',
                        'observed_at':datetime.now(timezone.utc).isoformat(),
                        'detail':'User confirms bags full and requests hunting without looting for this goal.'}}


class InventoryRig(LootRig):
    def __init__(self, path):
        super().__init__(path)
        self.panel = False
        self.error = None
        self.error_y = 60
        self.error_confidence = .99
        ocr, key = self.c.ocr, self.backend.key
        def rows(path):
            out = ocr(path)
            if self.panel:
                out += [TextObservation('Items',.99,{'x':165,'y':160,'width':35,'height':10}),
                        TextObservation('Broken Fang',.99,{'x':145,'y':178,'width':80,'height':12})]
            if self.error:
                out.append(TextObservation(self.error,self.error_confidence,
                           {'x':110,'y':self.error_y,'width':170,'height':12}))
            return out
        def press(code, down):
            key(code, down)
            if code == self.controls['escape']['keycode'] and not down: self.panel = False
        self.c.ocr = rows
        self.backend.key = press

    def enable(self, *, confirmed=True):
        self.c.config.update(loot_enabled=True, skip_loot_when_inventory_full=True)
        if confirmed: self.c.config['inventory_full_seed'] = seed()


def test_confirmed_full_closes_loot_panel_then_resumes_hunting(tmp_path):
    async def run():
        r = InventoryRig(tmp_path)
        try:
            await r.start(); r.enable(); r.death(); r.panel=True
            result = await r.choose('close_visible_ui')
            assert result.status == 'dispatched' and not r.panel
            assert 3 not in r.physical_keys()  # No F interaction.
            await r.choose('world_normal_confirmed')
            await r.choose('target_enemy')
            assert not r.c.hunt.loot_request and not r.c.hunt.credited_kills
            assert not {'loot_remaining','inspect_recent_corpse'} & r.sage.calls[-1]['options'].keys()
        finally: await r.close()
    asyncio.run(run())


import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import pytest
from sage_wow.agent.grind_inventory import (observe_capacity, policy, effective_loot_enabled,
    retire_loot, augment_ui, loot_panel)
from sage_wow.agent.grind_search import ui_evidence
from sage_wow.agent.grind_only import settings
from sage_wow.agent.grind_loot import loot_route_pending, process_loot


def exercise(fn):
    def test(tmp_path):
        async def run():
            r=InventoryRig(tmp_path)
            try:
                await r.start()
                await fn(r)
            finally:await r.close()
        asyncio.run(run())
    return test


@exercise
async def test_screen_capacity_error_retires_collection_without_loot_credit(r):
    r.enable(confirmed=False);r.death();r.error='Inventory is full.';r.panel=True
    await r.choose('close_visible_ui')
    proof=policy(r.c)['evidence']
    assert proof['source']=='current_screen_error' and proof['rows'][0]['text']==r.error
    assert proof['frame_id'] and proof['image_sha256']
    assert not loot_route_pending(r.c) and not r.c.hunt.credited_kills
    assert r.c.loot.data['outcome']=='skipped_inventory_full_not_verified_looted'
    assert r.c.loot.data['confirmations']==0 and not r.c.loot.verification_available
    assert 3 not in r.physical_keys()


@pytest.mark.parametrize('text,y,confidence',[
    ('Inventory is full',270,.99),('Player says inventory is full',60,.99),
    ('Out of range',60,.99),('You do not have permission to loot that corpse',60,.99),
    ('No target',60,.99),('There is no loot',60,.99),('Inventory is full',60,.5),
])
def test_other_errors_and_chat_are_not_capacity(tmp_path,text,y,confidence):
    async def run():
        r=InventoryRig(tmp_path)
        try:
            await r.start();r.enable(confirmed=False);r.death();r.error=text;r.error_y=y;r.error_confidence=confidence
            f=r.world.capture();await observe_capacity(r.c,f,await r.c.rows(f))
            assert policy(r.c) is None and loot_route_pending(r.c)
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('case',['no_loot','stale','wrong_identity','disabled','generation','revision','epoch','changed_image'])
def test_capacity_requires_current_scoped_loot_and_own_identity(tmp_path,case,monkeypatch):
    async def run():
        r=InventoryRig(tmp_path)
        try:
            await r.start();r.enable(confirmed=False);r.death();r.error='Inventory is full'
            f=r.world.capture();rows=await r.c.rows(f)
            if case=='no_loot':r.c.hunt.loot_request=None
            if case=='disabled':r.c.config['skip_loot_when_inventory_full']=False
            if case=='stale':f=replace(f,captured_at=(datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat())
            if case=='wrong_identity':rows=[x for x in rows if x.text!='Test Player']
            if case in {'generation','revision','epoch','changed_image'}:
                from sage_wow.agent import grind_observation
                original=grind_observation.read_player_identity
                async def changed(*args,**kwargs):
                    result=await original(*args,**kwargs)
                    if case=='generation':r.c.cycle._input_generation+=1
                    elif case=='revision':r.c.revision+=1
                    elif case=='epoch':r.c.cycle.session_epoch='other-epoch'
                    else:
                        from pathlib import Path
                        Path(f.image_path).write_bytes(Path(f.image_path).read_bytes()+b'changed')
                    return result
                monkeypatch.setattr(grind_observation,'read_player_identity',changed)
            await observe_capacity(r.c,f,rows)
            assert not getattr(r.c,'loot_policy',None)
        finally:await r.close()
    asyncio.run(run())


@exercise
async def test_closed_panel_withholds_escape_at_dispatch(r):
    r.enable();r.panel=True
    async def disappeared():r.panel=False
    r.sage.hook=disappeared
    before=list(r.physical_keys())
    result=await r.choose('close_visible_ui')
    assert result.status=='dispatch_guard_rejected' and r.physical_keys()==before


@pytest.mark.parametrize('case',['heading_only','chat','target_overlay','low_confidence','duplicate_title'])
def test_items_text_needs_a_current_local_panel(tmp_path,case):
    async def run():
        r=InventoryRig(tmp_path)
        try:
            await r.start();r.enable();r.panel=True
            f=r.world.capture();rows=await r.c.rows(f)
            if case=='heading_only':rows=[x for x in rows if x.text!='Broken Fang']
            if case=='chat':rows=[replace(x,bounds={**x.bounds,'y':270 if x.text=='Items' else 285}) if x.text in {'Items','Broken Fang'} else x for x in rows]
            if case=='target_overlay':rows=[replace(x,bounds={**x.bounds,'x':220,'y':110 if x.text=='Items' else 126}) if x.text in {'Items','Broken Fang'} else x for x in rows]
            if case=='low_confidence':rows=[replace(x,confidence=.4) if x.text=='Items' else x for x in rows]
            if case=='duplicate_title':rows.append(replace(next(x for x in rows if x.text=='Items'),bounds={'x':100,'y':110,'width':35,'height':10}))
            assert not loot_panel(r.c,f,rows)
            assert not augment_ui(r.c,f,rows,ui_evidence(r.c.profile,f,rows,{}))['positive']
        finally:await r.close()
    asyncio.run(run())


@exercise
async def test_suppression_preserves_pending_partial_input_and_failure_debt(r):
    r.enable();r.death()
    r.c.hunt.pending={'receipt':{'dispatch_unknown':True},'family':'combat'}
    r.c.hunt.input_effect_unverified=True
    r.c.hunt.blocked={'reason':'blocked_resources'}
    r.c.hunt.failures['combat']=2
    r.c.hunt.cast_obligation={'unresolved':True}
    prior=deepcopy((r.c.hunt.pending,r.c.hunt.blocked,r.c.hunt.cast_obligation))
    retire_loot(r.c)
    assert (r.c.hunt.pending,r.c.hunt.blocked,r.c.hunt.cast_obligation)==prior
    assert r.c.hunt.input_effect_unverified and r.c.hunt.failures['combat']==2
    assert not r.c.hunt.loot_request
    assert r.c.loot.data['outcome']=='skipped_inventory_full_not_verified_looted'


@exercise
async def test_future_death_and_speculative_corpse_do_not_reenter_loot(r):
    r.enable();r.c.config['committed_combat']=True
    await r.choose('attack_mob_level_1')
    r.world.name=''
    await r.choose('dead_after_selection_cleared')
    assert r.c.hunt.target_dead_observed and not r.c.hunt.loot_request
    assert 'inspect_recent_corpse' not in r.sage.calls[-1]['options']
    await r.choose('target_enemy')
    assert 'Loot collection is suspended' in r.sage.calls[-1]['prompt']
    assert not r.c.hunt.credited_kills and not loot_route_pending(r.c)


@exercise
async def test_policy_persists_as_facts_in_checkpoint_and_campaign(r):
    r.enable();progress=r.world.directory/'campaign.json'
    r.c.config['campaign_progress_path']=str(progress)
    r.c.record_campaign_progress(r.world.capture(),1,{'verified':True})
    proof=json.loads(progress.read_text())
    assert proof['loot_policy']==seed_with_time(r.c)
    assert 'receipt' not in proof['loot_policy'] and 'session_epoch' not in proof['loot_policy']
    del r.c.loot_policy;r.c.config.pop('inventory_full_seed')
    assert not effective_loot_enabled(r.c)  # checkpoint
    del r.c.loot_policy;r.store.save_checkpoint('grind_loot_policy',{})
    r.c.config['campaign_continuation']={'evidence':proof}
    assert not effective_loot_enabled(r.c)  # fresh-session continuation facts


def seed_with_time(c):return c.config['inventory_full_seed']


@pytest.mark.parametrize('change',[{'character':'Other Player'},{'goal_level':5},{'state':'full'},
    {'evidence':{'source':'chat','reason':'inventory_full'}}])
def test_mismatched_or_unattributed_seed_rejected(tmp_path,change):
    async def run():
        r=InventoryRig(tmp_path)
        try:
            cfg=r.c.profile.values['grind_only']
            cfg.update(skip_loot_when_inventory_full=True,inventory_full_seed={**seed(),**change})
            with pytest.raises(ValueError):settings(r.c.profile)
        finally:await r.close()
    asyncio.run(run())


@exercise
async def test_without_confirmation_existing_loot_behavior_is_unchanged(r):
    r.enable(confirmed=False);r.death();r.panel=True
    await r.loot('loot_remaining')
    assert effective_loot_enabled(r.c) and r.c.loot.pending
    assert policy(r.c) is None


@exercise
async def test_direct_loot_entry_cannot_reenable_suppressed_collection(r):
    r.enable();r.death()
    f=r.world.capture();rows=await r.c.rows(f)
    result=await process_loot(r.c,f,await r.c.target_proposal(f),{},rows)
    assert result.status=='grind_reobserve' and not r.sage.answers
    assert not loot_route_pending(r.c) and not r.physical_keys()


@pytest.mark.parametrize('chat',[False,True])
def test_retained_panel_geometry_and_actual_passive_chat_regression(tmp_path,chat):
    # Text/geometry transcribed from a retained live-run frame, not live capture.
    # No fixture pixels or OCR are treated as proof of full inventory.
    from types import SimpleNamespace
    from sage_wow.models import Frame
    c=SimpleNamespace(profile=SimpleNamespace(values={'calibration':{'ui_layout':{
        'version':1,'id':'retained64','provenance':'offline retained geometry',
        'image_width':1496,'image_height':967,'regions':{
        'player_frame':[52,348,446,496],'player_health':[191,405,436,433],
        'target_overlay':[145,500,480,628],'target_frame':[148,509,360,574],
        'target_health':[158,545,356,568]}}}}))
    frame=Frame.create('offline-retained-shape',1496,967)
    if chat:
        texts=[('Items',60,707,40,16),('Somebody linked a sword',40,736,160,16)]
    else:texts=[('Items',500,624,37,11),('Broken Fang',465,656,76,16)]
    rows=[TextObservation(t,.99,dict(x=x,y=y,width=w,height=h)) for t,x,y,w,h in texts]
    assert bool(loot_panel(c,frame,rows)) is not chat
