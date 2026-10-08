"""Historical cast diagnostics must not become another encounter's question."""
from copy import deepcopy
from dataclasses import replace

import pytest

from sage_wow.agent.grind_search import HuntState
from sage_wow.models import Frame
from test_grind_committed_combat import scenario, add_precise_resource_observer


def reviewed_casts():
    h=HuntState({},'offline-review')
    frame=Frame.create('offline-window',500,300)
    target={'name':'Ragged Young Wolf','levels':[1],
        'hud':{'frame_id':frame.frame_id,'target_health':1.,'target_health_confidence':1.,'player_mana':1.},
        'visual_observation':{'selected_hud':'present','life_state':'alive'}}
    history=h.approach_for(target);history['combat_failures']=2
    h.failures['combat']=2;h.cast_obligation='retained unassessed combat debt'
    for generation in (1,2):
        pending={'family':'combat','target':dict(target),'target_history_key':history['key'],
            'source_frame_id':'before','measurement':{'combat_hud':{**target['hud'],'frame_id':'before'}},
            'receipt':{'receipt_id':f'cast-{generation}','completed':True,'possible_input':True,
                'session_epoch':'epoch','generation_before':generation-1,'generation_after':generation}}
        review=h.review_cast_observations(target,pending,frame,linked=True,fresh=True,
            session_epoch='epoch',input_generation=generation)
    assert review['unchanged_resource_casts']==2
    return h,target,pending,frame


@pytest.mark.parametrize('change',[
    'different_target','absent','dead','empty_health','new_life','completed_life',
    'stale_hud','stale_frame','epoch','generation','window','geometry',
    'target_pending','partial_input','unlinked','wrong_receipt_epoch','wrong_receipt_generation',
    'wrong_source_hud','unknown_life'])
def test_inactive_review_is_retained_without_erasing_combat_debt(change):
    h,target,pending,frame=reviewed_casts()
    saved=deepcopy(h.cast_review);kwargs={'linked':True,'fresh':True,'session_epoch':'epoch','input_generation':2}
    if change=='different_target':target['name']='Burly Rockjaw Trogg'
    elif change=='absent':target['visual_observation']['selected_hud']='absent'
    elif change=='dead':target['visual_observation']['life_state']='dead'
    elif change=='empty_health':target['hud']['target_health']=0
    elif change=='new_life':h.target_lives['ragged young wolf']=1
    elif change=='completed_life':h.target_history[pending['target_history_key']]['completed']=True
    elif change=='stale_hud':target['hud']['frame_id']='older-frame'
    elif change=='stale_frame':kwargs['fresh']=False
    elif change=='epoch':kwargs['session_epoch']='new-epoch'
    elif change=='generation':kwargs['input_generation']=3
    elif change=='window':frame=replace(frame,source='other-window')
    elif change=='geometry':frame=replace(frame,width=600)
    elif change=='target_pending':pending['family']='target'
    elif change=='partial_input':pending['receipt']['completed']=False
    elif change=='unlinked':kwargs['linked']=False
    elif change=='wrong_receipt_epoch':pending['receipt']['session_epoch']='other-epoch'
    elif change=='wrong_receipt_generation':pending['receipt']['generation_after']=3
    elif change=='wrong_source_hud':pending['measurement']['combat_hud']['frame_id']='unrelated-frame'
    elif change=='unknown_life':
        target['hud']['target_health']=None;target['visual_observation']['life_state']='unknown'
    assert h.review_cast_observations(target,pending,frame,**kwargs) is None
    assert h.cast_review==saved
    assert h.failures['combat']==2 and h.cast_obligation=='retained unassessed combat debt'
    assert h.target_history[pending['target_history_key']]['combat_failures']==2


@pytest.mark.parametrize('pending_present',[True,False])
def test_current_same_living_review_remains_active_without_double_counting(pending_present):
    h,target,pending,frame=reviewed_casts()
    assert h.review_cast_observations(target,pending if pending_present else None,frame,
        linked=True,fresh=True,session_epoch='epoch',input_generation=2) is h.cast_review
    assert h.cast_review['unchanged_resource_casts']==2
    assert h.cast_review['reviewed_receipts']==['cast-1','cast-2']


def test_current_typed_alive_observation_can_support_unreadable_health():
    h,target,pending,frame=reviewed_casts()
    target['hud']['target_health']=None
    assert h.review_cast_observations(target,pending,frame,linked=True,fresh=True,
        session_epoch='epoch',input_generation=2) is h.cast_review


def test_late_health_drop_retires_already_reviewed_receipt_without_claiming_damage():
    h,target,pending,frame=reviewed_casts()
    target['hud']['target_health']=.65
    assert h.review_cast_observations(target,pending,frame,linked=True,fresh=True,
        session_epoch='epoch',input_generation=2) is None
    assert h.cast_review['unchanged_resource_casts']==2
    assert h.cast_review['retired_by']['frame_id']==frame.frame_id
    assert not h.cast_review['damage_known'] and not h.credited_kills
    assert h.failures['combat']==2 and h.cast_obligation=='retained unassessed combat debt'


@scenario
async def test_changed_target_menu_drops_old_wolf_diagnostic_and_keeps_feedback(r):
    add_precise_resource_observer(r,[1.])
    await r.choose('attack_mob_level_1');await r.choose('attack_mob_level_1')
    await r.choose(None)
    assert r.c.hunt.cast_review['unchanged_resource_casts']==2
    saved=deepcopy(r.c.hunt.cast_review)
    r.world.name='Burly Rockjaw Trogg'
    await r.choose(None)
    options=r.sage.calls[-1]['options']
    assert not {'reinspect_cast_problem','approach_for_range_check','stand_for_cast'} & options.keys()
    assert 'Completed cast inputs repeatedly left' not in r.sage.calls[-1]['prompt']
    assert 'cannot_assess' not in options  # Real null remains honest unresolved feedback.
    assert r.c.hunt.cast_review==saved and len(r.casts())==2


@scenario
async def test_late_health_drop_restores_normal_same_target_menu(r):
    health=[1.];add_precise_resource_observer(r,health)
    await r.choose('attack_mob_level_1');await r.choose('attack_mob_level_1')
    await r.choose(None)
    health[0]=.65
    await r.choose(None)
    options=r.sage.calls[-1]['options']
    assert 'attack_mob_level_1' in options
    assert not {'reinspect_cast_problem','approach_for_range_check','stand_for_cast'} & options.keys()
    assert r.c.hunt.cast_review['retired_by'] and not r.c.hunt.credited_kills
