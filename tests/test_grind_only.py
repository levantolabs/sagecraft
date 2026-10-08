"""Isolated fake grinding loop: no legacy runtime, network, capture or native input."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json

import pytest
from PIL import Image, ImageDraw
from sage_wow.agent.grind_only import GrindController, settings, same_patch, evidence_image
from sage_wow.grind_runner import GrindRunner
from sage_wow.config import Profile
from sage_wow.control.executor import GateSnapshot, SafeExecutor
from sage_wow.models import Frame
from sage_wow.perception.ocr import TextObservation
from sage_wow.platform.macos.geometry import Rect
from sage_wow.sage.client import SageDecision
from sage_wow.storage import EventStore


class Backend:
    def __init__(self):self.events=[];self.closed=False
    def key(self,*args):self.events.append(('key',*args))
    def text(self,text):self.events.append(('text',text))
    def release_all(self):self.events.append(('release',))
    def close(self):self.closed=True


class Sage:
    def __init__(self,choices=(),hook=None):self.choices=list(choices);self.calls=[];self.hook=hook;self.closed=False
    async def decide_image_choice(self,path,prompt,instructions,candidates,envelope,reasoning):
        options={c.option:c for c in candidates.options}
        self.calls.append({'options':options,'prompt':prompt,'instructions':instructions,'size':Image.open(path).size})
        if self.hook:await self.hook()
        chosen=self.choices.pop(0) if self.choices else 'target_unclear'
        return SageDecision(envelope,chosen,options[chosen].binding if chosen in options else None,.9,(),
            'offline-grind-test',0,{'ran':False},{'simulated':True},0,tuple(options))
    async def close(self):self.closed=True


def profile(tmp_path):
    regions={'player_frame':[0,0,150,90],'player_health':[40,30,140,40],
        'target_frame':[200,80,490,240],'target_health':[240,125,380,135],'target_overlay':[200,80,490,240]}
    values={'character':{'name':'Test Player'},'client':{'window_id':7},
        'calibration':{'ui_layout':{'version':1,'id':'fake','provenance':'offline operator geometry',
        'image_width':500,'image_height':300,'regions':regions}},
        'grind_only':{'enabled':True,'clean_world_observation':False,'session_id':'fresh-test','duration_seconds':10,'cast_wait_seconds':.1,
        'player_level_box':[25,55,45,80],'target_level_box':[420,170,450,200],
        'evidence_max_bytes':1024**2,'disk_reserve_bytes':1024**3},
        'controls':{'bindings':{name:{'keycode':i,'verified_from':'fake operator'} for i,name in enumerate(
        ('target_enemy','forward','backward','turn_left','turn_right','smite','escape','target_self','lesser_heal'))}}}
    path=tmp_path/'profile.yaml';path.write_text('offline fixture')
    return Profile(path,values)


class Frames:
    def __init__(self,tmp_path):self.directory=tmp_path;self.count=0;self.name='Young Wolf';self.numeric=None;self.dead=False;self.health='green';self.badge_change=None
    def capture(self):
        self.count+=1;path=self.directory/f'source-{self.count}.png'
        image=Image.new('RGB',(500,300),'#263746');draw=ImageDraw.Draw(image)
        draw.text((210,100),self.name,fill='white');draw.rectangle((240,125,380,135),fill=self.health)
        draw.text((425,175),'1',fill='white');draw.text((28,60),'1',fill='white')
        if self.badge_change=='target':draw.point((426,177),fill='red')
        if self.badge_change=='player':draw.point((28,62),fill='red')
        image.save(path);return Frame.create('offline-window-7',500,300,image_path=str(path))
    def ocr(self,path):
        row=lambda text,x,y,w,h:TextObservation(text,.99,{'x':x,'y':y,'width':w,'height':h})
        rows=[row(self.name,210,100,140,15),row('Test Player',40,5,90,15)]
        if self.numeric is not None:rows.append(row(str(self.numeric),425,175,12,18))
        if self.dead:rows.append(row('Dead',400,140,30,12))
        return rows


def setup(tmp_path,choices=(),hook=None):
    frames=Frames(tmp_path);sage=Sage(choices,hook);backend=Backend();rect=Rect(0,0,500,300)
    gate=GateSnapshot(7,7,True,rect,rect,True,True)
    executor=SafeExecutor(backend,lambda:gate,heartbeat_timeout=30,watchdog_interval=.01)
    store=EventStore(tmp_path/'unit.sqlite3');p=profile(tmp_path)
    controller=GrindController(p,store,sage,executor,capture=frames.capture,ocr=frames.ocr)
    return controller,frames,sage,backend,store,executor


async def baseline(c,f):
    if c.require_world:
        c.cycle.sage.choices.insert(0,'world_normal_confirmed')
        assert (await c.process(f.capture())).status=='dispatched'
    assert (await c.process(f.capture())).status=='dispatched'
    assert c.baseline and c.level.last_confirmed_level==1


async def cleanup(executor,store):
    await executor.stop();store.close()


def test_explicit_long_horizon_keeps_an_absolute_bounded_deadline(tmp_path):
    import time
    async def run():
        c,f,s,b,store,e=setup(tmp_path)
        try:
            c.profile.values['grind_only']['duration_seconds']=28800
            configured=settings(c.profile)
            c.config=configured;c.deadline=time.time()+configured['duration_seconds']
            deadline=c.deadline
            c.pause_focus();c.resume_focus()
            assert c.deadline==deadline and 28799<deadline-time.time()<=28800
            c.profile.values['grind_only']['duration_seconds']=28801
            with pytest.raises(ValueError,match='duration_seconds'):settings(c.profile)
            c.deadline=time.time()-1
            assert not c.current() and not c.resume_focus()
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_combined_visual_choice_one_cast_then_two_fresh_goal_level_reads(tmp_path):
    async def scenario():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','attack_mob_level_1',
            'player_level_3','player_level_3']);e.arm()
        try:
            await baseline(c,f)
            result=await c.process(f.capture())
            assert result.status=='dispatched' and result.receipt['possible_input']
            assert [event for event in b.events if event[0]=='text']==[('text','/cast [harm,nodead] Smite')]
            assert len(s.calls)==3  # Startup world proof; no separate eligibility/classifier call.
            assert 'attack_mob_level_1' in s.calls[-1]['options']  # Numeric OCR absent.
            c.level.last_attempt_at=0;c.hunt.phase='search';c.hunt.encounter_ended=True
            await c.process(f.capture())
            assert not c.success and not c.stopped
            await c.process(f.capture())
            assert c.success and c.stopped and c.reason=='goal_level_verified'
            assert c.cycle.input_generation==result.receipt['generation_after']
            assert result.receipt['generation_after']>result.receipt['generation_before']
        finally:await cleanup(e,store)
    asyncio.run(scenario())


@pytest.mark.parametrize('kind',['self','dead','out_of_band'])
def test_invalid_selected_units_offer_no_ordinary_attack(tmp_path,kind):
    async def scenario():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','reject_selected_target']);e.arm()
        try:
            await baseline(c,f)
            if kind=='self':f.name='Test Player'
            elif kind=='dead':f.dead=True
            else:f.numeric=5
            await c.process(f.capture())
            assert not any(x.startswith('attack_mob_') for x in s.calls[-1]['options'])
            if kind!='out_of_band':assert 'defend_attacker' not in s.calls[-1]['options']
            assert not any(x[0]=='text' for x in b.events)
        finally:await cleanup(e,store)
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['target','player','name'])
def test_fresh_dispatch_changed_badge_or_target_vetoes_without_input(tmp_path,change):
    async def scenario():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','attack_mob_level_1']);e.arm()
        try:
            await baseline(c,f)
            async def mutate():
                if change=='name':f.name='Other Wolf'
                else:f.badge_change=change
            s.hook=mutate
            result=await c.process(f.capture())
            assert result.status!='dispatched' and not c.stopped
            assert not any(x[0]=='text' for x in b.events)
            assert c.cycle.input_generation==0
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_successful_health_changes_allow_more_than_three_casts(tmp_path):
    async def scenario():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','attack_mob_level_1']+['damaged_alive','attack_mob_level_1']*4);e.arm()
        try:
            await baseline(c,f)
            for index,color in enumerate(('green','yellow','orange','red','black')):
                f.health=color
                if index:assert (await c.process(f.capture())).status=='dispatched'
                assert (await c.process(f.capture())).status=='dispatched'
            assert len([x for x in b.events if x[0]=='text'])==5 and not c.stopped
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_unchanged_cast_and_unclear_streak_change_menu_without_stop(tmp_path):
    async def scenario():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','attack_mob_level_1','no_effect','attack_mob_level_1','no_effect']+['cannot_assess']*3);e.arm()
        try:
            await baseline(c,f)
            for _ in range(7):await c.process(f.capture())
            assert 'attack_mob_level_1' not in s.calls[-1]['options']
            assert 'target_unclear' not in s.calls[-1]['options']
            assert {'forward','turn_left','reject_selected_target','cannot_assess','dead_or_unrecoverable'}<=s.calls[-1]['options'].keys()
            assert not c.stopped and not c.hunt.blocked and c.hunt.recovery_requested
            assert 'change_search_strategy' in s.calls[-1]['options']
            s.choices.append('reinspect_selected_frame')
            calls=len(s.calls);await c.process(f.capture());assert len(s.calls)==calls+1
            assert len([x for x in b.events if x[0]=='text'])==2
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_due_level_check_defers_during_combat_without_level_trigger_options(tmp_path):
    async def scenario():
        c,f,s,b,store,e=setup(tmp_path,['player_level_1','attack_mob_level_1','damaged_alive','attack_mob_level_1']);e.arm()
        try:
            await baseline(c,f);await c.process(f.capture());c.level.last_attempt_at=0;f.health='yellow'
            await c.process(f.capture());await c.process(f.capture())
            assert 'attack_mob_level_1' in s.calls[-1]['options']
            assert 'verify_player_level' not in s.calls[-1]['options'] and not any(x.startswith('player_level_') for x in s.calls[-1]['options'])
        finally:await cleanup(e,store)
    asyncio.run(scenario())


def test_localized_digit_stroke_is_not_diluted_and_inset_keeps_source(tmp_path):
    f=Frames(tmp_path);a=f.capture();f.badge_change='target';b=f.capture()
    assert same_patch(a.image_path,b.image_path,(420,170,450,200))
    assert not same_patch(a.image_path,b.image_path,(420,170,450,200),badge=True)
    original=Path(a.image_path).read_bytes()
    image=evidence_image(a,(200,80,490,240),tmp_path/'view.png',player_badge=(25,55,45,80))
    assert Image.open(image).width>=684
    assert Path(a.image_path).read_bytes()==original


class Freeze:
    def __init__(self,*args):self.manifest={'offline':True}
    def changed(self):return None


def test_runner_control_during_pending_inference_releases_everything(tmp_path,monkeypatch):
    from types import SimpleNamespace
    # This verifies control/heartbeat behavior; host disk pressure is unrelated.
    monkeypatch.setattr('sage_wow.grind_runner.shutil.disk_usage',
        lambda path: SimpleNamespace(free=10*1024**3))
    async def scenario():
        p=profile(tmp_path);f=Frames(tmp_path);entered=asyncio.Event();cancelled=[]
        async def block():
            entered.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:cancelled.append(True);raise
        sage=Sage(['player_level_1'],block);backend=Backend();rect=Rect(0,0,500,300)
        e=SafeExecutor(backend,lambda:GateSnapshot(7,7,True,rect,rect,True,True),heartbeat_timeout=.75,watchdog_interval=.01)
        directory=tmp_path/'run';runner=GrindRunner(p,directory,sage=sage,executor=e,capture=f.capture,ocr=f.ocr,freeze_factory=Freeze)
        task=asyncio.create_task(runner.run());await asyncio.wait_for(entered.wait(),2)
        await asyncio.sleep(.8);assert e.armed  # Independent heartbeat while Sage waits.
        (directory/'control.json').write_text(json.dumps({'command':'stop'}))
        await asyncio.wait_for(task,2)
        assert cancelled and backend.closed and sage.closed and not e.armed
        assert not any(x[0]=='text' for x in backend.events)
        assert json.loads((directory/'status.json').read_text())['state']=='STOPPED'
        assert not list((directory/'trial-evidence').glob('grind-*.png'))
    asyncio.run(scenario())


def test_existing_trial_never_opened_or_appended(tmp_path):
    async def scenario():
        p=profile(tmp_path);directory=tmp_path/'finished';directory.mkdir()
        history=directory/'events.sqlite3';history.write_bytes(b'finished immutable data')
        before=hashlib.sha256(history.read_bytes()).hexdigest()
        runner=GrindRunner(p,directory,sage=Sage(),executor=SimpleNamespace())
        with pytest.raises(ValueError,match='immutable'):await runner.run()
        assert hashlib.sha256(history.read_bytes()).hexdigest()==before
        assert sorted(x.name for x in directory.iterdir())==['events.sqlite3']
    asyncio.run(scenario())


def test_standalone_runner_does_not_construct_legacy_harness():
    import ast
    for path in ('src/sage_wow/grind_runner.py','src/sage_wow/agent/grind_only.py'):
        tree=ast.parse(Path(path).read_text())
        modules=[node.module or '' for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)]
        assert not any(m.endswith(('.runtime','.gameplay','.tactical','.controller','.runner')) for m in modules)
