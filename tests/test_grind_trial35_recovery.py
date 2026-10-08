"""Current entry mode and one guarded clear pair; fake providers and motors only."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

from sage_wow.agent.grind_ui import active_text_entry
from sage_wow.agent.grind_perception import observe_burst
from sage_wow.perception.ocr import TextObservation
from test_grind_product_spec import Rig
from test_grind_committed_combat import add_precise_resource_observer


def row(text='Say:', x=14, y=259, width=25, height=12):
    return TextObservation(text,.99,{'x':x,'y':y,'width':width,'height':height})


def entry_world(r, *, opened=True, first_clear_sticks=False):
    state={'open':opened,'escapes':0,'menu_open':False,'text':[]}
    capture=r.world.capture;ocr=r.world.ocr;press=r.backend.key;text=r.backend.text
    def draw():
        frame=capture()
        if state['open']:
            with Image.open(frame.image_path) as image:
                canvas=ImageDraw.Draw(image)
                canvas.rectangle((10,257,145,273),fill='black',outline='#dddddd')
                canvas.text((14,259),'Say:',fill='white');image.save(frame.image_path)
        return frame
    def rows(path):
        if Path(path).parent.name.startswith('sage-burst-hud-'):
            result=[row('Young Wolf',5,5,180,40)]
            if state['open']:
                # Exact current crop geometry: target, centre, player, entry.
                top=(160*3+20)+(165*3+20)+(90*3+20)
                result.append(row('Say:',42,top+(259-195)*3,75,36))
            return result
        result=ocr(path)
        if state['open'] and not Path(path).parent.name.startswith('sage-target-ocr-'):
            result.append(row())
        return result
    def key(*args):
        press(*args)
        if args[-1] is not True:return
        if args[0]==36:state['open']=not state['open']
        elif args[0]==r.controls['escape']['keycode']:
            state['escapes']+=1
            if state['open']:state['open']=False
            elif first_clear_sticks and state['escapes']==1:pass
            elif r.world.name:r.world.name=''
            else:state['menu_open']=True
    def typed(value):text(value);state['text'].append({'value':value,'entry_open':state['open']})
    r.world.capture=r.c.capture=draw;r.c.ocr=rows;r.backend.key=key;r.backend.text=typed
    return state


def test_bordered_say_is_current_entry_but_passive_chat_is_not(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            state=entry_world(r);frame=r.world.capture()
            assert active_text_entry(frame,[row()])['kind']=='bordered_chat_entry'
            assert active_text_entry(frame,[row('Someone says: hello')]) is None
            assert active_text_entry(frame,[replace(row(),confidence=.6)]) is None
            assert active_text_entry(frame,[row(y=150)]) is None
            state['open']=False;clean=r.world.capture()
            assert active_text_entry(clean,[row()]) is None
        finally:await r.close()
    asyncio.run(run())


def test_active_entry_uses_ui_close_then_fresh_world_confirmation(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.world.target_level=1;state=entry_world(r)
            await r.choose('close_visible_ui')
            assert not state['open'] and r.world.name and state['escapes']==1
            assert r.c.hunt.pending['family']=='ui' and r.c.clear_continuation is None
            assert not any('attack' in name for name in r.sage.calls[-1]['options'])
            await r.choose('world_normal_confirmed')
            assert not r.c.hunt.failures['clear'] and not r.c.hunt.credited_kills
            await r.choose('reject_selected_target')
            assert state['escapes']==2 and not r.world.name
            await r.choose('target_cleared')
            assert state['escapes']==2 and not state['menu_open'] and r.c.hunt.pending is None
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('choice',['reject_selected_target','attack_mob_level_1'])
def test_entry_appearing_at_dispatch_blocks_clear_or_cast(tmp_path,choice):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.world.target_level=1;state=entry_world(r,opened=False)
            async def appeared():state['open']=True
            r.sage.hook=appeared
            result=await r.choose(choice)
            assert result.status=='dispatch_guard_rejected'
            assert not state['text'] and state['escapes']==0 and r.c.clear_continuation is None
        finally:await r.close()
    asyncio.run(run())


def test_entry_between_burst_pulses_stops_next_command(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.world.target_level=1;state=entry_world(r,opened=False)
            r.c.config.update(committed_combat=True,smite_burst_count=3,cast_wait_seconds=2.2)
            r.executor.batch_guard=lambda binding,index:observe_burst(r.c,binding,index)
            async def between(seconds):state['open']=True
            r.executor._batch_sleep=between
            result=await r.choose('attack_mob_level_1')
            assert result.receipt['completed'] and result.receipt['execution']['cancelled']
            assert len([x for x in state['text'] if x['value'].startswith('/cast ')])==1
            assert all(x['entry_open'] for x in state['text']) and not r.c.hunt.credited_kills
            await r.choose('close_visible_ui')
            assert not state['open'] and r.c.hunt.pending['family']=='ui'
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('stays',[False,True])
def test_clear_pair_is_one_attempt_and_never_escapes_an_empty_selection(tmp_path,stays):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.world.target_level=1;state=entry_world(r,opened=False,first_clear_sticks=stays)
            await r.choose('reject_selected_target');original=deepcopy(r.c.hunt.pending)
            calls=len(r.sage.calls)
            if stays:
                r.c.wait_until=0;result=await r.c.process(r.world.capture())
                assert result.status=='dispatched' and result.decision is None
                assert len(r.sage.calls)==calls and state['escapes']==2
                assert r.c.hunt.pending['clear_pair']['original']==original
                assert result.receipt['authorization_id']==original['receipt']['receipt_id']
                assert not r.c.hunt.failures['clear'] and not r.c.hunt.outcomes
            await r.choose('target_cleared')
            assert state['escapes']==(2 if stays else 1) and not state['menu_open']
            assert r.c.hunt.pending is None and not r.c.clear_continuation
            outcomes=[x for x in r.c.hunt.outcomes if x['outcome']=='target_cleared']
            assert len(outcomes)==1 and not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['generation','source','config','revision','target','ui','unknown','partial'])
def test_second_escape_requires_unchanged_owned_current_clear(tmp_path,change):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.world.target_level=1;state=entry_world(r,opened=False,first_clear_sticks=True)
            await r.choose('reject_selected_target');pending=r.c.hunt.pending
            if change=='generation':r.c.cycle._input_generation+=1
            elif change=='source':
                with Image.open(pending['source_image']) as im:
                    ImageDraw.Draw(im).point((0,0),fill='white');im.save(pending['source_image'])
            elif change=='config':r.c.config['cast_wait_seconds']+=.1
            elif change=='revision':r.c.revision+=1
            elif change=='target':r.world.name='Other Creature'
            elif change=='ui':state['open']=True
            elif change=='unknown':r.world.name=''
            else:pending['receipt']['completed']=False
            r.sage.answers.append(None);r.c.wait_until=0
            await r.c.process(r.world.capture())
            assert state['escapes']==1 and r.c.clear_continuation is None
            assert not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_cast_feedback_removes_requested_answers_and_keeps_useful_exits(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.c.config['committed_combat']=True
            add_precise_resource_observer(r,[1.])
            await r.choose('attack_mob_level_1');await r.choose('attack_mob_level_1')
            await r.choose(None)
            options=r.sage.calls[-1]['options']
            assert not {'cannot_assess','no_effect','reinspect_cast_problem','reinspect_selected_frame'} & options.keys()
            assert {'approach_for_range_check','stand_for_cast','reject_selected_target','change_search_strategy'} <= options.keys()
            assert r.c.hunt.cast_review['unchanged_resource_casts']==2
            await r.choose('change_search_strategy')
            assert r.c.hunt.planning_requested and not r.c.hunt.credited_kills
            assert not r.c.hunt.retry_credit
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('outcome',['absent','failed','unknown'])
def test_typed_clear_pair_retains_owner_and_one_final_outcome(tmp_path,monkeypatch,outcome):
    from test_grind_target_inspection_contract import ContractRig
    from test_grind_selected_task_exit import disposed, exhaust
    from sage_wow.agent.grind_relocation import typed_release
    async def run():
        r=ContractRig(tmp_path/'typed-pair')
        try:
            values,intent=await disposed(r,monkeypatch);await exhaust(r)
            await r.choose('reject_selected_target')
            original=deepcopy(r.c.hunt.pending);before=r.c.hunt.failures['clear']
            r.c.wait_until=0;second=await r.c.process(r.world.capture())
            assert second.status=='dispatched' and second.decision is None
            pending=r.c.hunt.pending
            assert pending['clear_pair']['original']==original
            assert pending['selected_task_abandon']==original['selected_task_abandon']
            assert r.c.hunt.failures['clear']==before
            if outcome!='failed':
                r.world.name='';r.world.target_level=None;values['target_health']=None
            if outcome=='absent':
                await r.choose('target_cleared')
                assert typed_release(r.c.hunt) and intent['disposition']=='closed'
            elif outcome=='failed':
                await r.choose('clear_failed')
                assert not typed_release(r.c.hunt) and intent['disposition']=='handed_off'
            else:
                await r.choose(None);await r.choose('cannot_assess')
                assert intent['disposition']=='assessment_exhausted' and not typed_release(r.c.hunt)
                assert len(intent['clear_assessments'][second.receipt['receipt_id']]['request_ids'])==2
            assert r.c.hunt.pending is None and pending['outcome_consumed']
            assert r.c.hunt.failures['clear']==before+(outcome!='absent')
            outcomes=[x for x in r.c.hunt.outcomes if x['receipt_id'] in
                {original['receipt']['receipt_id'],second.receipt['receipt_id']}]
            assert len(outcomes)==1 and not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


def test_partial_second_escape_retains_input_unknown_and_cannot_repeat(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.world.target_level=1;state=entry_world(r,opened=False,first_clear_sticks=True)
            await r.choose('reject_selected_target');original=deepcopy(r.c.hunt.pending)
            key=r.backend.key
            def partial(*args):
                key(*args)
                if args[0]==r.controls['escape']['keycode'] and args[-1] is True:
                    raise RuntimeError('offline partial second Escape')
            r.backend.key=partial;r.c.wait_until=0
            result=await r.c.process(r.world.capture())
            assert result.status=='execution_failed' and state['escapes']==2
            assert r.c.hunt.input_effect_unverified and r.c.clear_continuation is None
            assert r.c.hunt.pending['clear_pair']['original']==original
            assert not r.c.hunt.pending['receipt']['completed']
            assert not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())
