"""Source sensing completion and one-use acquisition under real fake controller."""
import asyncio
from copy import deepcopy
import time

import pytest
from PIL import Image, ImageDraw

from test_grind_target_inspection_contract import ContractRig
from test_grind_exact_band_observation import start_band
from test_active_attacker_recovery import native_fact
from sage_wow.agent import grind_inspection


class SourceRig(ContractRig):
    def __init__(self, directory):
        self.observer_hook = None
        super().__init__(directory)

    async def inspect(self, path, **kwargs):
        result = await super().inspect(path, **kwargs)
        if self.observer_hook:await self.observer_hook(self, path)
        return result


async def empty_source(r):
    await start_band(r, 4)
    r.c.config['target_bands_by_player_level'][4]=[1,4]
    r.c.hunt.target_bands_by_player_level[4]=[1,4]
    r.world.name='';r.world.target_level=None;r.c.hunt.compact_stage='reinspect'
    r.answers.update(selected_hud='absent',name='other_or_unknown',level='unknown',target_kind='unknown',life_state='unknown')
    assert (await r.observe_target()).status=='grind_reobserve'
    return deepcopy(r.c.target_inspection_episode)


async def tab(r):
    r.c.hunt.compact_stage='acquire';r.c.hunt.encounter_ended=True;r.c.hunt.selected_presence=False
    r.c.observer_last_at=time.time()
    result=await r.choose('target_enemy')
    assert result.status=='dispatched' and result.receipt['possible_input']
    return result


async def selected(r, *, numeral='1', expect_observer=True):
    r.world.scenery_patches=[];r.world.name='Rockjaw Trogg';r.world.target_level=1
    r.answers.update(selected_hud='present',name='other_or_unknown',level=numeral,target_kind='creature',life_state='alive')
    r.c.observer_last_at=0
    return await r.choose() if expect_observer else await r.choose(None)


def test_source_absence_completes_once_across_animated_empty_backgrounds(tmp_path):
    async def run():
        r=SourceRig(tmp_path/'empty')
        try:
            episode=await empty_source(r)
            assert episode['disposition']=='source_resolved_absent' and episode['attempts']==1
            for color in ('#504030','#405030','#263746'):
                r.world.scenery_patches=[((200,80,490,240),color)]
                r.c.observer_last_at=0  # Do not let the five-second throttle hide a loop.
                await r.choose(None)
                assert r.c.target_inspection_episode['id']==episode['id']
            assert len(r.wire)==1 and len(r.events('grind_target_inspection_source_resolved'))==1
            assert not r.events('grind_target_inspection_exhausted') and not r.physical()
            assert r.events('grind_target_observation_rejected')[0]['source_question_completed']
            assert not r.c.hunt.credited_kills and not r.c.hunt.progress_facts
        finally:await r.close()
    asyncio.run(run())


def test_real_tab_missing_ocr_then_observer_numeral_ordinary_guarded_attack(tmp_path):
    async def run():
        r=SourceRig(tmp_path/'tab')
        try:
            episode=await empty_source(r)
            r.c.hunt.action_failures['retained-motion']=2
            receipt=await tab(r)
            result=await selected(r)
            assert result.status=='grind_reobserve', (r.c.target_inspection_episode, r.c.hunt.pending, r.c.hunt.target_continuity)
            pending=r.c.hunt.pending
            assert pending['receipt']['receipt_id']==receipt.receipt['receipt_id'] and not pending['outcome_consumed']
            fresh=r.c.target_inspection_episode
            assert fresh['id']!=episode['id'] and fresh['attempts']==1
            assert fresh['sensing_proof']['receipt_id']==receipt.receipt['receipt_id']
            assert not [x for x in r.physical() if x[0]=='text']
            cast=await r.choose('attack_mob_level_1')
            assert cast.status=='dispatched' and cast.receipt['possible_input']
            assert r.events('dispatch_guard_checked')[-1]['approved']
            assert r.c.hunt.action_failures['retained-motion']==2
            assert not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind',['raw_name','raw_level','live_bar','dead_bar','visual_name'])
def test_envelope_accepted_absence_does_not_complete_contradictory_source(tmp_path,monkeypatch,kind):
    async def run():
        r=SourceRig(tmp_path/kind)
        try:
            await start_band(r,4)
            r.world.name='';r.world.target_level=None;r.c.hunt.compact_stage='reinspect'
            r.answers.update(selected_hud='absent',name='other_or_unknown',level='unknown',target_kind='unknown',life_state='unknown')
            if kind in {'raw_name','raw_level'}:
                r.world.name='Rockjaw Trogg';r.world.target_level=1
                r.drop_name=kind=='raw_level';r.drop_level=kind!='raw_level'
            if kind in {'live_bar','dead_bar'}:
                r.world.scenery_patches=[((240,125,380,135),'green' if kind=='live_bar' else '#301010')]
            if kind=='dead_bar':
                from sage_wow.agent import grind_resources
                original=grind_resources.hud_resources
                monkeypatch.setattr(grind_resources,'hud_resources',lambda c,f:
                    {**original(c,f),'target_health':0.,'target_health_confidence':1.})
            if kind=='visual_name':
                base=r.c.target_proposal
                async def visual(frame):return {**await base(frame),'eligibility':'unknown','visual_observation':{'selected_hud':'unknown','name':'Rockjaw Trogg'}}
                r.c.target_proposal=visual
            await r.observe_target()
            assert r.events('grind_target_observer_result')[-1]['accepted']
            assert not r.events('grind_target_inspection_source_resolved')
            assert not r.physical()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation',['hash','epoch','generation','pending','selection','geometry','input_unknown','world','threat'])
def test_awaited_absent_response_cannot_finish_superseded_source_task(tmp_path,mutation):
    async def run():
        r=SourceRig(tmp_path/mutation)
        try:
            async def hook(r,path):
                if mutation=='hash':
                    with Image.open(r.c.target_inspection_episode['source_image']) as im:
                        image=im.copy();ImageDraw.Draw(image).point((210,100),fill='red');image.save(r.c.target_inspection_episode['source_image'])
                elif mutation=='epoch':r.c.cycle.session_epoch='new-epoch'
                elif mutation=='generation':r.c.cycle._input_generation+=1
                elif mutation=='pending':
                    r.c.hunt.install('target_enemy','target',r.world.capture(),deepcopy(r.c.cycle.last_receipt),{},target={'name':None,'levels':[]})
                    r.replacement_pending=r.c.hunt.pending
                elif mutation=='selection':r.c.hunt.selection_revision+=1
                elif mutation=='geometry':r.c.profile.values['calibration']['ui_layout']['regions']['target_health']=[1,1,2,2]
                elif mutation=='input_unknown':r.c.hunt.input_effect_unverified=True
                elif mutation=='world':r.c.require_world=True
                else:r.c.hunt.active_threat={'replacement':True}
            r.observer_hook=hook
            await empty_source(r)
            assert not r.events('grind_target_inspection_source_resolved')
            assert not r.events('grind_target_observer_result')[-1]['accepted']
            if mutation=='pending':
                assert r.c.hunt.pending is r.replacement_pending and not r.c.hunt.pending['outcome_consumed']
                assert not r.events('grind_action_outcome') and not r.c.hunt.unassessed
            assert not r.physical()
        finally:await r.close()
    asyncio.run(run())


def test_source_age_can_expire_after_parser_acceptance_without_finishing_question(tmp_path,monkeypatch):
    async def run():
        r=SourceRig(tmp_path/'source-age')
        try:
            async def hook(r,path):
                # Parser permits 30 seconds; this task's fresh-source scope
                # can be tighter, so parser acceptance is insufficient.
                r.c.cycle._execution_scope.get().max_frame_age_seconds=5
                now=time.time()
                monkeypatch.setattr(time,'time',lambda:now+11)
            r.observer_hook=hook
            await empty_source(r)
            assert not r.events('grind_target_inspection_source_resolved')
            assert not r.events('grind_target_observer_result')[-1]['accepted']
            assert not r.physical()
        finally:await r.close()
    asyncio.run(run())


def test_current_attributable_attacker_without_tab_gets_sensing_only(tmp_path):
    async def run():
        r=SourceRig(tmp_path/'threat')
        try:
            episode=await empty_source(r)
            r.c.combat_log_facts=[native_fact(event='SWING_DAMAGE',own=False)]
            assert (await selected(r)).status=='grind_reobserve'
            new=r.c.target_inspection_episode
            assert new['id']!=episode['id'] and new['sensing_proof']['kind']=='current_threat_selection'
            assert r.c.hunt.pending is None and not r.physical()
            assert not r.c.hunt.approach and not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_blocking_ui_owns_current_threat_and_does_not_consume_absent_edge(tmp_path):
    async def run():
        from test_grind_ui_closure_recovery import attach_menu
        r=SourceRig(tmp_path/'ui-threat')
        try:
            episode=await empty_source(r)
            visible=attach_menu(r)
            r.world.name='Rockjaw Trogg';r.world.target_level=1
            r.c.combat_log_facts=[native_fact(event='SWING_DAMAGE',own=False)]
            r.c.observer_last_at=0
            await r.choose(None)
            assert 'close_visible_ui' in r.sage.calls[-1]['options']
            assert len(r.wire)==1 and r.c.target_inspection_episode['id']==episode['id']
            assert not r.c.target_inspection_episode['source_absence']['consumed_by']
            assert not r.physical()
            # A positive already observed behind UI is not a later fresh edge.
            visible[0]=False;r.c.require_world=False;r.c.hunt.phase='search'
            await r.choose(None)
            assert len(r.wire)==1 and r.c.target_inspection_episode['id']==episode['id']
            assert not r.c.target_inspection_episode['source_absence']['consumed_by']
        finally:await r.close()
    asyncio.run(run())


def test_disposed_exhausted_task_current_attacker_keeps_stronger_route_without_refill(tmp_path,monkeypatch):
    async def run():
        from test_grind_selected_task_exit import disposed
        r=SourceRig(tmp_path/'disposed-threat')
        try:
            _,intent=await disposed(r,monkeypatch)
            episode=deepcopy(r.c.target_inspection_episode)
            r.c.combat_log_facts=[native_fact(event='SWING_DAMAGE',own=False)]
            r.c.observer_last_at=0
            await r.choose(None)
            assert r.c.hunt.active_threat and r.c.hunt.phase!='travel'
            assert r.c.target_inspection_episode['id']==episode['id']
            assert r.c.target_inspection_episode['attempts']==2 and len(r.wire)==2
            assert not any(key.startswith('attack_') for key in r.sage.calls[-1]['options'])
            assert 'recover_now' in r.sage.calls[-1]['options']
            assert 'explore_visible' not in r.sage.calls[-1]['options']
            assert r.c.hunt.strategy_required['selected_exit'] is intent
            assert not r.physical()
        finally:await r.close()
    asyncio.run(run())


def test_measured_low_health_owns_positive_threat_without_spending_sensing_edge(tmp_path):
    async def run():
        r=SourceRig(tmp_path/'health-threat')
        try:
            episode=await empty_source(r)
            r.world.name='Rockjaw Trogg';r.world.target_level=1
            r.world.scenery_patches=[((40,30,140,40),'#301010'),((40,30,60,40),'green')]
            r.c.config.update(encounter_resources=True,preserve_target_heal=True)
            r.c.combat_log_facts=[native_fact(event='SWING_DAMAGE',own=False)]
            r.c.observer_last_at=0
            await r.choose(None)
            assert r.c.current_hud['player_health']<=r.c.config['heal_health_fraction']
            assert 'heal_self' in r.sage.calls[-1]['options']
            assert r.c.target_inspection_episode['id']==episode['id'] and len(r.wire)==1
            assert not r.c.target_inspection_episode['source_absence']['consumed_by']
            assert not r.physical()
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind',['historical','own','unattributed','unknown_input'])
def test_no_tab_unproved_threat_cannot_renew_source_absence(tmp_path,kind):
    async def run():
        r=SourceRig(tmp_path/kind)
        try:
            episode=await empty_source(r)
            fact=native_fact(event='SWING_DAMAGE',own=False)
            if kind=='historical':fact['occurred_at']=time.time()-60
            elif kind=='own':fact.update(own_source=True,incoming_to_player=False)
            elif kind=='unattributed':fact['source_guid']='unknown'
            else:r.c.hunt.input_effect_unverified=True
            r.c.combat_log_facts=[fact]
            await selected(r,expect_observer=False)
            assert r.c.target_inspection_episode['id']==episode['id'] and len(r.wire)==1
            assert not r.c.target_inspection_episode['source_absence']['consumed_by']
            assert not r.physical()
        finally:await r.close()
    asyncio.run(run())


def test_positive_without_tab_or_attributed_threat_does_not_renew(tmp_path):
    async def run():
        r=SourceRig(tmp_path/'unproved')
        try:
            episode=await empty_source(r)
            await selected(r,expect_observer=False)
            assert r.c.target_inspection_episode['id']==episode['id'] and len(r.wire)==1
            assert r.c.target_inspection_episode['source_absence']['invalidated_by']
            # Useful observed search remains an independent retirement control.
            r.c.hunt.search_revision+=1
            r.c.observer_last_at=0
            await r.choose(None)
            assert len(r.wire)==2 and r.c.target_inspection_episode['id']!=episode['id']
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['hash','configuration','epoch','generation','receipt'])
def test_completed_tab_cannot_borrow_mutated_absence_provenance(tmp_path,change):
    async def run():
        r=SourceRig(tmp_path/change)
        try:
            episode=await empty_source(r)
            await tab(r)
            if change=='hash':
                with Image.open(episode['source_absence']['source_image']) as raw:
                    image=raw.copy();ImageDraw.Draw(image).point((212,103),fill='red')
                    image.save(episode['source_absence']['source_image'])
            elif change=='configuration':r.c.config['target_name_box']=[201,95,410,120]
            elif change=='epoch':r.c.cycle.session_epoch='different-epoch'
            elif change=='generation':r.c.cycle._input_generation+=1
            else:r.c.hunt.pending['receipt']['completed']=False
            await selected(r,expect_observer=False)
            assert r.c.target_inspection_episode['id']==episode['id']
            assert len(r.wire)==1 and not [x for x in r.physical() if x[0]=='text']
            assert not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_positive_tab_receipt_and_unchanged_positive_tabs_do_not_refill_new_episode(tmp_path):
    async def run():
        r=SourceRig(tmp_path/'reuse')
        try:
            await empty_source(r);await tab(r)
            r.answers['level']='unknown'
            await selected(r,numeral='unknown')
            episode=deepcopy(r.c.target_inspection_episode)
            await r.observe_target()
            assert r.c.target_inspection_episode['attempts']==2
            r.c.hunt.archive_pending('offline-existing-unknown-disposition')
            await tab(r)
            await selected(r,numeral='unknown',expect_observer=False)
            assert r.c.target_inspection_episode['id']==episode['id']
            assert r.c.target_inspection_episode['attempts']==2 and len(r.wire)==3
            assert not [x for x in r.physical() if x[0]=='text']
        finally:await r.close()
    asyncio.run(run())


def test_already_readable_first_boundary_proof_cannot_leak_to_later_unknown_selection(tmp_path):
    async def run():
        r=SourceRig(tmp_path/'atomic')
        try:
            await empty_source(r);await tab(r)
            r.drop_level=False
            await selected(r,expect_observer=False)
            first=deepcopy(r.c.target_inspection_episode)
            assert first['attempts']==0 and first['sensing_proof']['kind']=='linked_acquisition'
            r.c.hunt.archive_pending('offline-existing-disposition')
            r.world.name='Burly Rockjaw Trogg';r.world.target_level=2;r.drop_level=True
            r.answers['level']='unknown'
            await r.observe_target()
            assert r.c.target_inspection_episode['id']!=first['id']
            assert 'sensing_proof' not in r.c.target_inspection_episode
            assert len(r.wire)==2 and not [x for x in r.physical() if x[0]=='text']
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('assessment',[False,True])
def test_empty_tab_then_later_positive_requires_genuine_current_absence_reanchor(tmp_path,monkeypatch,assessment):
    async def run():
        r=SourceRig(tmp_path/str(assessment))
        try:
            episode=await empty_source(r)
            await tab(r)
            if assessment:
                r.c.observer_last_at=0
                await r.choose('no_selected_frame')
                assert r.events('grind_target_inspection_absence_reanchored')
            else:
                r.c.hunt.archive_pending('offline-unassessed-first-tab')
                # Missed assessment cannot renew; the original residual
                # allowance remains independently usable after actual Tab.
                monkeypatch.setattr(grind_inspection,'needs_boundary',lambda *args:False)
            await tab(r)
            await selected(r,expect_observer=assessment)
            assert (r.c.target_inspection_episode['id']!=episode['id']) is assessment
            assert len(r.wire)==2
            if not assessment:
                assert r.c.target_inspection_episode['attempts']==2
                assert 'sensing_proof' not in r.c.target_inspection_episode
            assert not [x for x in r.physical() if x[0]=='text']
        finally:await r.close()
    asyncio.run(run())


def test_old_cast_lost_empty_sensing_tab_numeral_attack_feedback_loot_and_hunt(tmp_path):
    async def run():
        r=SourceRig(tmp_path/'whole')
        try:
            await start_band(r,4)
            r.c.hunt.target_bands_by_player_level[4]=[1,4]
            r.c.config['target_bands_by_player_level'][4]=[1,4]
            await r.observe_target()
            oldcast=await r.choose('attack_mob_level_2')
            assert oldcast.status=='dispatched' and oldcast.receipt['possible_input']
            old_owner=r.c.hunt.combat_history_key
            r.world.name='';r.world.target_level=None
            await r.choose('lost')
            assert r.c.hunt.encounter_ended and r.c.hunt.pending is None
            assert r.c.hunt.unassessed[-1]['receipt']['receipt_id']==oldcast.receipt['receipt_id']
            assert r.c.hunt.cast_obligation
            r.c.hunt.compact_stage='reinspect'  # Explicit sensing question, no ownership reset.
            r.answers.update(selected_hud='absent',name='other_or_unknown',level='unknown',target_kind='unknown',life_state='unknown')
            await r.observe_target()
            episode=deepcopy(r.c.target_inspection_episode)
            r.world.scenery_patches=[((200,80,490,240),'#504030')]
            r.c.observer_last_at=0
            await r.choose(None)
            assert r.events('grind_target_observation_rejected')[-1]['source_question_completed']
            before=deepcopy(r.c.hunt.target_history[old_owner])
            # Source completion itself grants no current absence. This actual
            # offered image assessment supplies ordinary acquisition entry.
            await r.choose('no_selected_frame')
            assert r.c.hunt.compact_stage=='acquire'
            r.c.config['target_opening_cast']=True
            from sage_wow.agent.grind_acquisition import learn_creature
            assert learn_creature(r.c,{'own_source':True,'event':'PARTY_KILL','dest_guid':'Creature-offline-type',
                'dest_name':'Rockjaw Trogg','source_name':'Test Player'})
            target=await r.choose('target_enemy')
            assert target.status=='dispatched'
            await selected(r)
            assert r.c.target_opener is None
            assert [x['outcome'] for x in r.events('grind_target_opener_result')]==['returned_to_sage']
            assert len([x for x in r.physical() if x[0]=='text'])==1
            assert r.c.hunt.pending['receipt']['receipt_id']==target.receipt['receipt_id']
            assert r.c.target_inspection_episode['id']!=episode['id']
            assert r.c.hunt.target_history[old_owner]==before
            await r.choose('attack_mob_level_1')
            assert len([x for x in r.physical() if x[0]=='text'])==2
            assert r.events('dispatch_guard_checked')[-1]['approved']
            from test_grind_loot_budget import enable
            r.p.values['controls']['bindings']['interact_target']={'keycode':3,'verified_from':'offline fixture'}
            enable(r)
            r.world.health='#301010'
            await r.choose('dead')
            assert r.c.hunt.target_dead_observed and r.c.hunt.loot_request
            for answer in ('loot_remaining','loot_selected_corpse','interact_target','loot_verified','loot_verified'):
                await r.choose(answer)
            assert not r.c.loot.pending and r.c.loot.data['outcome']=='verified_looted'
            r.world.name='';r.world.target_level=None;r.c.observer_last_at=time.time()
            await r.choose('target_enemy')
            assert r.c.hunt.pending['family']=='target'
            assert not r.c.hunt.credited_kills and not r.c.success
            assert r.c.hunt.target_history[old_owner]['cast_obligation']==before['cast_obligation']
        finally:await r.close()
    asyncio.run(run())
