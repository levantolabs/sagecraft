"""Movement questions retain purpose; correction menus preserve actual authority."""
import asyncio
import hashlib
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image

from sage_wow.agent.grind_combat import correction_action_state
from sage_wow.agent.grind_motion_evidence import motion_context, motion_evidence_image
from sage_wow.agent.grind_only import RetainedGrindSourceChanged
from test_grind_committed_combat import scenario, add_current_observer
from test_grind_product_spec import Rig


def healthy_observer(r):
    add_current_observer(r)
    original=r.c.target_proposal
    async def observe(frame):
        target=await original(frame)
        return {**target,'hud':{'frame_id':frame.frame_id,'player_health':1.,'health_confidence':1.,
            'target_health':1.,'target_health_confidence':1.,'player_mana':1.}}
    r.c.target_proposal=observe


async def range_error(r):
    healthy_observer(r)
    await r.choose('attack_mob_level_1')
    r.world.error='Out of range'
    await r.choose('position_error')


@scenario
async def test_unknown_forward_then_different_correction_preserves_debt_and_can_earn_retry(r):
    await range_error(r)
    h=r.c.hunt;owner=h.combat_history_key
    for _ in range(2):
        await r.choose('forward')
        await r.choose('cannot_assess');await r.choose('cannot_assess')
    debt=h.motion_key('forward','cast_correction',owner)
    assert h.motion_count('forward','cast_correction',owner)==2
    await r.choose('turn_right')
    call=r.sage.calls[-1]
    assert {'turn_left','turn_right','reject_selected_target','change_search_strategy'}<=call['options'].keys()
    assert not {'forward','attack_mob_level_1','cannot_assess','reinspect_selected_frame'} & call['options'].keys()
    assert 'last linked cast reported range' in call['prompt']
    assert "Currently unavailable: ['attack', 'forward']" in call['prompt']
    assert 'fixed HUD portrait does not show its direction' in call['prompt']
    assert 'Which offered action' in call['instructions']
    await r.choose('motion_useful')
    assert 'Assess one completed movement: turn_right' in r.sage.calls[-1]['prompt']
    assert 'purpose cast_correction' in r.sage.calls[-1]['prompt']
    assert 'spell is already in range' in r.sage.calls[-1]['prompt']
    assert 'selected portrait' not in r.sage.calls[-1]['instructions']
    r.world.error=''
    await r.choose('attack_mob_level_1')
    assert len(r.casts())==2 and h.unresolved_motion[debt]==2
    assert not h.credited_kills


@scenario
async def test_no_useful_correction_does_not_grant_cast(r):
    await range_error(r)
    await r.choose('forward');await r.choose('motion_no_useful_effect')
    await r.choose(None)
    assert 'attack_mob_level_1' not in r.sage.calls[-1]['options']
    assert r.c.hunt.cast_obligation and not r.c.hunt.retry_credit and len(r.casts())==1


@scenario
async def test_all_corrections_and_clear_exhausted_handoff_preserves_history(r):
    await range_error(r)
    h=r.c.hunt
    for action in ('forward','turn_left','turn_right','backward','strafe_left','strafe_right'):
        h.action_failures[h.motion_key(action,'cast_correction',h.combat_history_key)]=2
    h.action_failures[h.action_key('escape','clear')]=2
    before=deepcopy((h.target_history,h.action_failures,h.cast_error,h.cast_obligation))
    physical=r.physical_keys();calls=len(r.sage.calls)
    result=await r.c.process(r.world.capture())
    assert result.status=='grind_reobserve'
    assert h.phase=='choose_area' and h.planning_requested
    assert len(r.sage.calls)==calls and r.physical_keys()==physical
    assert (h.target_history,h.action_failures,h.cast_error,h.cast_obligation)==before
    assert not h.credited_kills and not h.retry_credit


@pytest.mark.parametrize('change', ['pending','mixed','probe','epoch','continuity','encounter','owner',
    'source_owner','source_scope','source_receipt','source_target','source_continuity',
    'partial','unknown_input','health','health_frame','target','threat','healing','mana','ended'])
def test_correction_menu_scope_excludes_unrelated_or_emergency_states(tmp_path,change):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.c.config['committed_combat']=True
            await range_error(r)
            frame=r.world.capture();target=await r.c.target_proposal(frame)
            h=r.c.hunt;pending=None;kwargs={}
            assert correction_action_state(r.c,frame,target,None,'inspect')
            if change=='pending':pending={'family':'motion'}
            elif change in {'mixed','probe'}:kwargs[change]={'evidence':'retained'}
            elif change=='epoch':h.cast_error['assessment_source']['receipt']['session_epoch']='old'
            elif change=='continuity':h.target_continuity+=1
            elif change=='encounter':h.encounter+=1
            elif change=='owner':h.combat_history_key='another'
            elif change=='source_owner':h.cast_error['assessment_source']['target_history_key']='another'
            elif change=='source_scope':h.cast_error['assessment_source']['source_scope']['source']='another-window'
            elif change=='source_receipt':h.cast_error['assessment_source']['receipt']['receipt_id']='another-cast'
            elif change=='source_target':h.cast_error['assessment_source']['target']['name']='Other target'
            elif change=='source_continuity':h.cast_error['assessment_source']['target_continuity']=-1
            elif change=='partial':h.cast_error['assessment_source']['receipt']['completed']=False
            elif change=='unknown_input':h.input_effect_unverified=True
            elif change=='health':target['hud']['player_health']=.1
            elif change=='health_frame':target['hud']['frame_id']='old'
            elif change=='target':target['visual_observation']['life_state']='unknown'
            elif change=='threat':h.active_threat={'current':True}
            elif change=='healing':r.c.heal_pending={'receipt':'pending'}
            elif change=='mana':h.no_mana=True
            elif change=='ended':h.encounter_ended=True
            assert not correction_action_state(r.c,frame,target,pending,'inspect',**kwargs)
        finally:await r.close()
    asyncio.run(run())


@scenario
async def test_numeric_dropout_does_not_change_historical_correction_owner(r):
    r.world.target_level=1
    await range_error(r)
    h=r.c.hunt;frame=r.world.capture();target=await r.c.target_proposal(frame)
    revision=h.selection_revision;continuity=h.target_continuity
    h.selection_seen({**target,'levels':[]})
    h.selection_seen(target)
    assert h.selection_revision>revision and h.target_continuity==continuity
    assert correction_action_state(r.c,frame,target,None,'inspect')
    await r.choose('turn_left')
    assert 'Which offered action' in r.sage.calls[-1]['instructions']
    assert h.cast_obligation and not h.retry_credit and len(r.casts())==1


@pytest.mark.parametrize('purpose,phrase', [('standing','stand our player up'),('search','changed search sector'),
    ('approach','closer'),('cast_correction','closer')])
def test_motion_context_describes_the_actual_task(purpose,phrase):
    pending={'action':'forward','purpose':purpose,'receipt':{'selected_binding':{'hold_seconds':.08}}}
    context=motion_context(pending,{'name':'Any eligible creature'})
    assert phrase in context and 'duration 0.08 seconds' in context
    assert 'BEFORE' in context and 'CURRENT' in context


def test_movement_comparison_orders_worlds_at_equal_scale_and_detects_mutation(tmp_path):
    before=tmp_path/'before.png';after=tmp_path/'after.png';output=tmp_path/'pair.png'
    Image.new('RGB',(500,300),'red').save(before);Image.new('RGB',(500,300),'blue').save(after)
    digest=hashlib.sha256(before.read_bytes()).hexdigest()
    args=dict(history=((str(before),digest,'old',32),),player_hud=(0,0,120,80))
    frame=SimpleNamespace(image_path=str(after))
    motion_evidence_image(frame,(200,80,490,240),output,**args)
    with Image.open(output) as image:
        assert image.width==500 and image.height<900
        assert image.getpixel((250,178))==(255,0,0)
        assert image.getpixel((250,506))==(0,0,255)
    Image.new('RGB',(500,300),'green').save(before)
    with pytest.raises(RetainedGrindSourceChanged):motion_evidence_image(frame,(200,80,490,240),output,**args)
    assert not output.exists()
