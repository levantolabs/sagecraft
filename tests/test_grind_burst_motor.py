"""Offline motor and current HUD tests: fake input, capture, OCR, no network."""
import asyncio
import hashlib
import socket
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from sage_wow.agent import grind_perception as perception
from sage_wow.agent.scene import Scene
from sage_wow.control.executor import SafeExecutor, ExecutionRejected
from test_executor import FakeBackend, valid_gate


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('SAGE_WOW_DISABLE_LIVE_INPUT','1')
    monkeypatch.setenv('SAGE_WOW_DISABLE_LIVE_CAPTURE','1')
    monkeypatch.setattr(socket.socket,'connect',lambda *args:pytest.fail('unintended network'))


def layout():
    return {'version':1,'id':'offline','provenance':'synthetic fixture',
        'image_width':400,'image_height':300,'regions':{
        'target_overlay':[200,60,380,140],'target_frame':[200,60,380,140],
        'target_health':[220,115,320,125],'player_frame':[10,200,140,260],
        'player_health':[20,240,120,250]}}


def frame(tmp_path,name='source',health=.8,target=.7):
    image=Image.new('RGB',(400,300),'#101010');draw=ImageDraw.Draw(image)
    draw.rectangle((20,240,20+int(health*100)-1,249),fill='#20d020')
    if target:draw.rectangle((220,115,220+int(target*100)-1,124),fill='#20d020')
    path=tmp_path/(name+'.png');image.save(path)
    return SimpleNamespace(frame_id=name,image_path=str(path),width=400,height=300,
        captured_at=datetime.now(timezone.utc).isoformat(),source='fake-window')


def row(text,x=0,y=0,width=100,height=15):
    return SimpleNamespace(text=text,confidence=.99,bounds={'x':x,'y':y,'width':width,'height':height})


def cache_controller(source):
    observation={'selected_hud':'present','name':'Young Wolf','level':1,
                 'target_kind':'creature','life_state':'alive'}
    envelope={'observation':observation,'frame_id':source.frame_id,'scope_id':'scope',
        'captured_at':source.captured_at,'image_sha256':'a'*64,
        'backend':'sage','configured_model':'levanto-sage'}
    return SimpleNamespace(profile=SimpleNamespace(values={'calibration':{'ui_layout':layout()}}),
        config={'target_name_box':[205,65,360,85],'target_names':['Young Wolf']},
        cycle=SimpleNamespace(input_generation=0,session_epoch='scope'),
        target_inspection_episode=None,
        hunt=SimpleNamespace(target_band=lambda level:(1,2),target_continuity=0,approach=None,encounter=0),
        level=SimpleNamespace(last_confirmed_level=1),target_observation={
        'result':envelope,'source':source,'generation':0,'submitted_sha256':'a'*64,
        'source_sha256':hashlib.sha256(open(source.image_path,'rb').read()).hexdigest()})


def target():
    return {'name':None,'levels':[],'self_target':False,'invalid_text':False,
        'box':(200,60,380,140),'badge':(205,90,215,100)}


def test_current_bar_reading_tracks_decreasing_hp_with_confidence(tmp_path):
    scene=perception.read_current_bars(frame(tmp_path,health=.35,target=.22),ui_layout=layout())
    assert scene.health==.35 and scene.health_confidence==1
    assert scene.target_health==.22 and scene.target_state=='alive'
    assert scene.health_evidence['frame_id']=='source'


def test_focused_hud_ocr_only_one_pass_and_dead_beats_green_pixels(tmp_path):
    calls=[]
    def ocr(path):
        calls.append(path)
        return [row('Young Wolf'),row('Dead',y=40),row('Out of range',y=260)]
    scene=perception.read_hud_scene(frame(tmp_path),ui_layout=layout(),ocr=ocr)
    assert len(calls)==1 and scene.target_state=='dead'
    assert scene.target_name=='Young Wolf' and scene.error=='Out of range'


def test_identity_survives_damage_but_alive_state_is_fresh_and_dead_is_sticky(tmp_path):
    source=frame(tmp_path);controller=cache_controller(source)
    damaged=frame(tmp_path,'damage',target=.2)
    enriched=perception.enrich(controller,damaged,target())
    assert enriched['name']=='Young Wolf' and enriched['eligibility']=='eligible'
    assert enriched['visual_observation']['life_state']=='alive'
    dead_scene=perception.read_current_bars(damaged,ui_layout=layout());dead_scene.target_dead=True
    enriched=perception.enrich(controller,damaged,target(),dead_scene)
    assert enriched['eligibility']=='ineligible'
    enriched=perception.enrich(controller,frame(tmp_path,'green-noise'),target())
    assert enriched['visual_observation']['life_state']=='dead'
    assert enriched['invalid_text']


def test_unreadable_current_life_does_not_reuse_cached_alive(tmp_path):
    source=frame(tmp_path);controller=cache_controller(source)
    enriched=perception.enrich(controller,frame(tmp_path,'empty-bar',target=0),target())
    assert enriched['name']=='Young Wolf'
    assert enriched['visual_observation']['life_state']=='unknown'
    assert enriched['eligibility']=='unknown'


@pytest.mark.parametrize('health,continues',[(.8,True),(.4,True),(.35,True),(.3,True),(.29,False)])
def test_burst_only_stops_for_critical_hp_not_normal_damage(health,continues):
    scene=Scene('frame',target_name='Young Wolf',target_alive=True,health=health,health_confidence=1)
    assert perception.burst_verdict(scene,'Young Wolf')['continue']==continues


@pytest.mark.parametrize('change',[{'target_dead':True},{'target_alive':None},
    {'target_name':'Rabbit'},{'error':'Out of range'},{'health':None},{'health_confidence':.2}])
def test_burst_fresh_guard_stops_death_change_error_or_unknown(change):
    scene=Scene('frame',target_name='Young Wolf',target_alive=True,health=.8,health_confidence=1)
    for name,value in change.items():setattr(scene,name,value)
    assert not perception.burst_verdict(scene,'Young Wolf')['continue']


def test_allowlisted_self_heal_preserves_target_and_has_input_receipt():
    backend=FakeBackend();executor=SafeExecutor(backend,valid_gate,heartbeat_timeout=3)
    async def run():
        executor.arm()
        try:return await executor.execute({'type':'cast_self_heal','spell':'Lesser Heal'})
        finally:await executor.stop()
    result=asyncio.run(run())
    assert [e[1] for e in backend.events if e[0]=='text']==['/cast [@player] Lesser Heal']
    assert result['preserves_selected_target'] and result['dispatched']
    assert any(e[0]=='key' for e in backend.events)


@pytest.mark.parametrize('spell',['Heal','Lesser Heal\n/cleartarget',None])
def test_self_heal_rejects_other_spell_before_input(spell):
    backend=FakeBackend();executor=SafeExecutor(backend,valid_gate)
    async def run():
        executor.arm()
        try:
            with pytest.raises(ExecutionRejected):await executor.execute({'type':'cast_self_heal','spell':spell})
        finally:await executor.stop()
    asyncio.run(run());assert backend.events==[]


def test_three_calibrated_pulses_and_clean_cancellation_have_settled_receipts():
    backend=FakeBackend();pacing=[]
    async def guard(binding,index):return {'continue':index<2,'frame_id':str(index),'reason':'target died'}
    async def sleep(seconds):pacing.append(seconds)
    executor=SafeExecutor(backend,valid_gate,heartbeat_timeout=3,batch_guard=guard,batch_sleep=sleep)
    async def run():
        executor.arm()
        try:return await executor.execute({'type':'cast_burst','spell':'Smite','count':3,
            'interval_seconds':2.2,'expected_target_name':'Young Wolf'})
        finally:await executor.stop()
    result=asyncio.run(run())
    assert pacing==[2.2,2.2]
    assert result['completed'] and result['cancelled'] and not result['burst_completed']
    assert result['remaining_steps']==1 and len(result['completed_steps'])==2
    assert [step['guard_frame_id'] for step in result['completed_steps']]==['0','1']
    assert all(step['submitted_at_monotonic']>0 for step in result['completed_steps'])


def test_fresh_burst_guards_reuse_only_identity_while_life_and_hp_change(tmp_path):
    source=frame(tmp_path);controller=cache_controller(source)
    controller.config['target_level_box']=[205,90,215,100]
    controller.hunt.selection_revision=2
    controller.revision=1;controller.current=lambda:True
    captures=iter([frame(tmp_path,'guard0'),frame(tmp_path,'guard1',health=.35,target=.2),
                   frame(tmp_path,'guard2',health=.35,target=.2)])
    controller.capture=lambda:next(captures)
    calls=[]
    def ocr(path):
        calls.append(str(path))
        return [row('Dead')] if len(calls)==3 else []
    controller.ocr=ocr;events=[];controller.event=lambda name,payload:events.append((name,payload))
    binding={'expected_target_name':'Young Wolf'}
    async def run():
        first=await perception.observe_burst(controller,binding,0)
        controller.cycle.input_generation=5
        second=await perception.observe_burst(controller,binding,1)
        third=await perception.observe_burst(controller,binding,2)
        return first,second,third
    first,second,third=asyncio.run(run())
    assert first['continue'] and second['continue']
    assert second['player_health_pixel_estimate']==.35
    assert second['target_health_pixel_estimate']==.2
    assert second['observed_target_name']=='Young Wolf'
    assert not third['continue'] and third['observed_target_state']=='dead'
    assert len(events)==3 and len(calls)==3


def test_cached_identity_carries_only_exact_completed_native_target_preserving_receipt(tmp_path):
    source=frame(tmp_path);controller=cache_controller(source)
    controller.hunt.selection_revision=2;controller.target_observation['selection_revision']=2
    command='/cast [harm,nodead] Smite'
    steps=[{'kind':kind,'status':'completed','details':{'length':len(command)} if kind=='text' else {'keycode':36}}
           for kind in ['key_down','key_up','text','key_down','key_up']]
    receipt={'selected_binding':{'type':'cast_guarded','spell':'Smite'},
        'completed':True,'dispatch_unknown':False,'error':None,'session_epoch':'scope',
        'generation_before':0,'generation_after':5,'input_steps':steps,
        'execution':{'kind':'cast_guarded','command':command}}
    controller.cycle.last_receipt=receipt;controller.cycle.input_generation=5
    assert perception.carry_target_observation(controller,receipt)
    assert controller.target_observation['generation']==5
    controller.target_observation['generation']=0
    receipt['input_steps'][2]['status']='failed_effect_unknown'
    assert not perception.carry_target_observation(controller,receipt)
    assert controller.target_observation['generation']==0


@pytest.mark.parametrize('mutation',['scope','generation','selection','binding','native_key','source'])
def test_cached_identity_cannot_cross_unreconciled_authority_or_source(tmp_path,mutation):
    source=frame(tmp_path);controller=cache_controller(source)
    controller.hunt.selection_revision=2;controller.target_observation['selection_revision']=2
    command='/cast [@player] Lesser Heal'
    steps=[{'kind':kind,'status':'completed','details':{'length':len(command)} if kind=='text' else {'keycode':36}}
           for kind in ['key_down','key_up','text','key_down','key_up']]
    receipt={'selected_binding':{'type':'cast_self_heal','spell':'Lesser Heal'},
        'completed':True,'session_epoch':'scope','generation_before':0,'generation_after':5,
        'input_steps':steps,'execution':{'kind':'cast_self_heal','command':command}}
    controller.cycle.last_receipt=receipt;controller.cycle.input_generation=5
    if mutation=='scope':receipt['session_epoch']='stale'
    elif mutation=='generation':receipt['generation_before']=1
    elif mutation=='selection':controller.hunt.selection_revision=3
    elif mutation=='binding':receipt['selected_binding']['type']='target_and_cast'
    elif mutation=='native_key':receipt['input_steps'][0]['details']['keycode']=48
    elif mutation=='source':Image.new('RGB',(400,300),'white').save(source.image_path)
    assert not perception.carry_target_observation(controller,receipt)


def test_guard_archives_fresh_frame_before_reusing_identity(tmp_path):
    from sage_wow.models import Frame
    source=frame(tmp_path);controller=cache_controller(source)
    controller.config['target_level_box']=[205,90,215,100]
    controller.hunt.selection_revision=2;controller.revision=1;controller.current=lambda:True
    captured=frame(tmp_path,'guard');captured=Frame(**vars(captured))
    controller.capture=lambda:captured;controller.ocr=lambda path:[]
    retained=[]
    def archive(frame,scope,generation):
        retained.append((frame.frame_id,scope,generation))
        path=tmp_path/'immutable.png';path.write_bytes(open(frame.image_path,'rb').read())
        return path
    controller.archive=SimpleNamespace(frame=archive);controller.event=lambda *args:None
    observation=asyncio.run(perception.observe_burst(controller,{'expected_target_name':'Young Wolf'},0))
    assert observation['continue']
    assert retained==[('guard','scope',0)]
    assert observation['image_path']==str(tmp_path/'immutable.png')
    assert controller._cast_burst_identity['source'].image_path==observation['image_path']


@pytest.mark.parametrize('failure',['timeout','exception','malformed'])
@pytest.mark.parametrize('failure_index',[0,1])
def test_guard_failure_cleanly_cancels_without_unreconciling_completed_native_input(tmp_path,failure,failure_index):
    from sage_wow.agent.cycle import DecisionCycle
    from sage_wow.models import Frame
    from sage_wow.storage import EventStore
    backend=FakeBackend()
    async def guard(binding,index):
        if index!=failure_index:return {'continue':True,'frame_id':'first'}
        if failure=='timeout':await asyncio.sleep(10)
        if failure=='exception':raise RuntimeError('offline observation failure')
        return {'continue':'not a bool'}
    async def sleep(seconds):pass
    executor=SafeExecutor(backend,valid_gate,heartbeat_timeout=3,batch_guard=guard,batch_sleep=sleep)
    executor.CAST_BURST_GUARD_MAX_SECONDS=.02
    store=EventStore(tmp_path/'receipts.sqlite3')
    # The continuation path takes fixed prior authorization and never calls Sage.
    cycle=DecisionCycle(SimpleNamespace(),executor,store)
    async def run():
        executor.arm()
        try:
            return await cycle.execute_authorized({'type':'cast_burst','spell':'Smite','count':3,
                'interval_seconds':2.2,'expected_target_name':'Young Wolf'},
                frame=Frame.create('offline',400,300),chosen_option='burst',
                authorization_id='prior-sage',request_id='offline-request',candidate_set_version='fixture')
        finally:await executor.stop()
    try:result=asyncio.run(run())
    finally:store.close()
    assert result.status=='dispatched'
    assert result.receipt['completed'] and not result.receipt['dispatch_unknown']
    assert result.receipt['error'] is None
    assert result.execution['cancelled'] and not result.execution['burst_completed']
    assert result.execution['guard_observations'][-1]['guard_failure']==(
        'timeout' if failure=='timeout' else 'RuntimeError' if failure=='exception' else 'malformed_decision')
    assert len(result.execution['completed_steps'])==failure_index
    assert len(result.receipt['input_steps'])==5*failure_index
    assert all(step['status']=='completed' for step in result.receipt['input_steps'])
    assert result.receipt['generation_after']==5*failure_index
    assert len([event for event in backend.events if event[0]=='text'])==failure_index


def test_guard_failure_cannot_hide_focus_loss():
    from sage_wow.control.executor import GateSnapshot
    backend=FakeBackend();base=valid_gate();active={'yes':True}
    def gate():return GateSnapshot(base.window_id,base.calibrated_window_id,active['yes'],
        base.bounds,base.calibrated_bounds,True,True)
    async def guard(binding,index):
        active['yes']=False
        raise RuntimeError('observation failed with changed foreground')
    executor=SafeExecutor(backend,gate,heartbeat_timeout=3,batch_guard=guard)
    async def run():
        executor.arm()
        try:
            with pytest.raises(ExecutionRejected,match='lost focus'):
                await executor.execute({'type':'cast_burst','spell':'Smite','count':3,
                    'expected_target_name':'Young Wolf'})
        finally:await executor.stop()
    asyncio.run(run());assert not backend.events


def test_deadline_during_native_command_remains_execution_failure(tmp_path):
    from sage_wow.agent.cycle import DecisionCycle
    from sage_wow.models import Frame
    from sage_wow.storage import EventStore
    backend=FakeBackend()
    async def guard(binding,index):return {'continue':True}
    executor=SafeExecutor(backend,valid_gate,heartbeat_timeout=3,batch_guard=guard)
    executor.CAST_BURST_MAX_SECONDS=.6
    async def partial_command(command):
        await executor._keypress({'keycode':36,'hold_seconds':.05})
        await asyncio.sleep(10)
    executor._submit_command=partial_command
    store=EventStore(tmp_path/'partial-receipts.sqlite3')
    cycle=DecisionCycle(SimpleNamespace(),executor,store)
    async def run():
        executor.arm()
        try:return await cycle.execute_authorized({'type':'cast_burst','spell':'Smite','count':3,
            'expected_target_name':'Young Wolf'},frame=Frame.create('offline',400,300),
            chosen_option='burst',authorization_id='prior-sage',request_id='offline-request',candidate_set_version='fixture')
        finally:await executor.stop()
    try:result=asyncio.run(run())
    finally:store.close()
    assert result.status=='execution_failed' and result.receipt['possible_input']
    assert not result.receipt['completed'] and result.receipt['error']
    assert 'during command submission' in result.receipt['error']


@pytest.mark.parametrize('text,kind',[('Out of range','range'),('Target is not in front of you','facing'),
    ('You must be standing to do that','standing'),('Not enough mana','mana'),
    ('Another action is in progress','cooldown')])
def test_fixed_error_region_reports_cast_failure_text_and_kind(tmp_path,text,kind):
    def ocr(path):return [row('Young Wolf'),row(text,y=260)]
    scene=perception.read_hud_scene(frame(tmp_path),ui_layout=layout(),ocr=ocr)
    assert scene.error==text and scene.error_cues==[{'kind':kind,'text':text}]
    assert not perception.burst_verdict(scene,'Young Wolf')['continue']


def test_early_post_cast_error_stops_remaining_pulses_and_native_receipt_is_reconciled(tmp_path):
    from sage_wow.agent.cycle import DecisionCycle
    from sage_wow.models import Frame
    from sage_wow.storage import EventStore
    backend=FakeBackend();phases=[]
    async def guard(binding,index):return {'continue':True,'frame_id':f'pre{index}'}
    async def after(binding,index,phase):
        phases.append((index,phase))
        return {'continue':False,'reason':'visible input error: Out of range',
            'observed_error_cues':[{'kind':'range','text':'Out of range'}],
            'frame_id':'early-error','image_path':'retained.png'}
    async def sleep(seconds):pass
    executor=SafeExecutor(backend,valid_gate,heartbeat_timeout=3,batch_guard=guard,
        batch_post_guard=after,batch_sleep=sleep)
    store=EventStore(tmp_path/'post-error.sqlite3');cycle=DecisionCycle(SimpleNamespace(),executor,store)
    async def run():
        executor.arm()
        try:return await cycle.execute_authorized({'type':'cast_burst','spell':'Smite','count':3,
            'interval_seconds':2.2,'expected_target_name':'Young Wolf','start_attack':True},
            frame=Frame.create('offline',400,300),chosen_option='burst',authorization_id='prior-sage',
            request_id='offline-request',candidate_set_version='fixture')
        finally:await executor.stop()
    try:result=asyncio.run(run())
    finally:store.close()
    assert [e[1] for e in backend.events if e[0]=='text']==['/startattack [harm,nodead]','/cast [harm,nodead] Smite']
    assert result.receipt['completed'] and not result.receipt['dispatch_unknown']
    assert len(result.receipt['input_steps'])==10 and result.receipt['generation_after']==10
    assert result.execution['cancelled'] and len(result.execution['completed_steps'])==1
    assert phases==[(0,'early_post_cast')]
    assert result.execution['post_cast_observations'][0]['observed_error_cues'][0]['kind']=='range'


def test_all_pulses_have_early_observations_and_last_pulse_has_settled_observation():
    backend=FakeBackend();phases=[];sleeps=[]
    async def guard(binding,index):return {'continue':True,'frame_id':f'pre{index}'}
    async def after(binding,index,phase):
        phases.append((index,phase));return {'continue':True,'frame_id':phase+str(index)}
    async def sleep(seconds):sleeps.append(seconds)
    executor=SafeExecutor(backend,valid_gate,heartbeat_timeout=3,batch_guard=guard,
        batch_post_guard=after,batch_sleep=sleep)
    async def run():
        executor.arm()
        try:return await executor.execute({'type':'cast_burst','spell':'Smite','count':3,
            'interval_seconds':2.2,'expected_target_name':'Young Wolf','start_attack':True})
        finally:await executor.stop()
    result=asyncio.run(run())
    assert phases==[(0,'early_post_cast'),(1,'early_post_cast'),(2,'early_post_cast'),(2,'settled_post_cast')]
    assert len(result['post_cast_observations'])==4
    assert len(result['opening_commands'])==1 and result['burst_completed']
    assert len(sleeps)==6 and sleeps[::2]==[.35]*3
    assert [e[1] for e in backend.events if e[0]=='text']==['/startattack [harm,nodead]']+['/cast [harm,nodead] Smite']*3


def test_opening_autoattack_input_failure_still_stops_without_smite():
    backend=FakeBackend()
    def fail_text(value):
        backend.events.append(('text',value));raise RuntimeError('native text failure')
    backend.text=fail_text
    async def guard(binding,index):return {'continue':True}
    executor=SafeExecutor(backend,valid_gate,heartbeat_timeout=3,batch_guard=guard)
    async def run():
        executor.arm()
        try:
            with pytest.raises(RuntimeError) as failure:
                await executor.execute({'type':'cast_burst','spell':'Smite','count':3,
                    'expected_target_name':'Young Wolf','start_attack':True})
            assert any(step['status']=='failed_effect_unknown' for step in failure.value.input_steps)
        finally:await executor.stop()
    asyncio.run(run())
    assert [e[1] for e in backend.events if e[0]=='text']==['/startattack [harm,nodead]']


def test_carried_identity_accepts_reconciled_autoattack_plus_opening_smite(tmp_path):
    source=frame(tmp_path);controller=cache_controller(source)
    controller.hunt.selection_revision=1;controller.target_observation['selection_revision']=1
    commands=['/startattack [harm,nodead]','/cast [harm,nodead] Smite']
    steps=[{'kind':kind,'status':'completed','details':{'length':len(command)} if kind=='text' else {'keycode':36}}
        for command in commands for kind in ['key_down','key_up','text','key_down','key_up']]
    receipt={'selected_binding':{'type':'cast_guarded','spell':'Smite','start_attack':True},
        'completed':True,'session_epoch':'scope','generation_before':0,'generation_after':10,
        'input_steps':steps,'execution':{'kind':'cast_guarded','command':commands[1],
            'opening_commands':[{'command':commands[0],'dispatched':True}]}}
    controller.cycle.last_receipt=receipt;controller.cycle.input_generation=10
    assert perception.carry_target_observation(controller,receipt)
    controller.target_observation['generation']=0
    receipt['execution']['opening_commands'][0]['command']='/targetenemy'
    assert not perception.carry_target_observation(controller,receipt)


def test_single_authorized_cast_probe_uses_early_and_settled_facts():
    backend=FakeBackend();phases=[]
    async def guard(binding,index):return {'continue':True,'frame_id':'pre'}
    async def after(binding,index,phase):
        phases.append(phase);return {'continue':True,'frame_id':phase}
    async def sleep(seconds):pass
    executor=SafeExecutor(backend,valid_gate,heartbeat_timeout=3,batch_guard=guard,
        batch_post_guard=after,batch_sleep=sleep)
    async def run():
        executor.arm()
        try:return await executor.execute({'type':'cast_guarded','spell':'Smite','expected_target_name':'Young Wolf'})
        finally:await executor.stop()
    result=asyncio.run(run())
    assert result['kind']=='cast_guarded' and result['requested_count']==1
    assert phases==['early_post_cast','settled_post_cast']
    assert [e[1] for e in backend.events if e[0]=='text']==['/cast [harm,nodead] Smite']
