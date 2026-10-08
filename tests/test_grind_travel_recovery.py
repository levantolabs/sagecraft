"""Travel2 recovery acceptance, entirely synthetic under the network/native guards."""
import asyncio
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from PIL import Image

from sage_wow.agent import cycle, grind_only, grind_observation, grind_search
from sage_wow.agent.grind_search import measure
from sage_wow.grind_runner import GrindRunner
from sage_wow.models import Event
from sage_wow.perception.ocr import TextObservation
from sage_wow.platform.macos.capture import CaptureError, ForegroundCaptureInterrupted
from test_grind_travel_acceptance import TravelRig, travel_scenario
from test_grind_focus import attached, eventually
from test_grind_only import cleanup, Frames, Freeze, Sage, Backend, profile
from sage_wow.control.executor import GateSnapshot, SafeExecutor
from sage_wow.platform.macos.geometry import Rect


def events(r,kind):
    return [json.loads(row[0]) for row in r.store.connection.execute(
        'SELECT payload_json FROM events WHERE event_type=? ORDER BY rowid',(kind,))]


def reading(r, names, outside=()):
    frame=r.world.capture()
    row=lambda text,x,y:TextObservation(text,.99,{'x':x,'y':y,'width':100,'height':15})
    rows=[row(name,260,5) for name in names]+[TextObservation(name,.99,{'x':320,'y':30,'width':100,'height':5}) for name in outside]
    rows.append(TextObservation('10.0, 10.0',.99,{'x':405,'y':40,'width':75,'height':15}))
    def crop(path):
        return [row(name,0,0) for name in names] if path.name=='zone.png' else []
    return frame,measure(r.c.profile,frame,rows,crop)


def test_zone_normalization_caption_provenance_conflict_and_true_transition(tmp_path):
    async def run():
        r=TravelRig(tmp_path)
        try:
            await r.travel()
            frame,m=reading(r,['  * Offline   VALLEY','offline valley'],['Different Place'])
            r.c.hunt.observe_zone(m,r.c.cycle.session_epoch,0)
            assert m['zone_normalized_keys']==['offline valley'] and m['zone_raw_proposals']==['* Offline   VALLEY','offline valley']
            assert r.c.hunt.current_geometry(m)['available']
            assert any(row['text']=='Different Place' and not row['caption_member'] for row in m['zone_observations'])
            assert all(not row['independent_vote'] for row in m['zone_observations'] if row['extraction']=='caption_resample')
            _,conflict=reading(r,['Offline Valley','Different Place'])
            r.c.hunt.observe_zone(conflict,r.c.cycle.session_epoch,0)
            assert conflict['zone_resolution']=='conflict' and not r.c.hunt.current_geometry(conflict)['available']
            r.c.hunt.action_responses={'forward':{'historical':'sentinel'}};r.c.hunt.probe_series={'historical':'sentinel'}
            _,transition=reading(r,['Different Place'])
            r.c.hunt.observe_zone(transition,r.c.cycle.session_epoch,0)
            assert transition['zone_resolution']=='transition' and r.c.hunt.established_zone['key']=='different place'
            assert not r.c.hunt.current_geometry(transition)['available']
            assert not r.c.hunt.action_responses and r.c.hunt.probe_series is None
        finally:await r.close()
    asyncio.run(run())


@travel_scenario
async def test_catalog_does_not_establish_zone_and_weak_read_keeps_history_and_debt(r):
    r.c.hunt.established_zone=None
    _,m=reading(r,[])
    r.c.hunt.observe_zone(m,r.c.cycle.session_epoch,0)
    assert r.c.hunt.established_zone is None and not r.c.hunt.current_geometry(m)['available']
    await r.action('probe_forward');await r.action('probe_forward');await r.action('turn_left')
    history=list(r.c.hunt.recent_moves);debt=dict(r.c.hunt.travel_failures);established=dict(r.c.hunt.established_zone)
    _,weak=reading(r,[]);r.c.hunt.observe_zone(weak,r.c.cycle.session_epoch,r.c.cycle.input_generation)
    assert weak['zone_resolution']=='weak_historical_only' and not r.c.hunt.current_geometry(weak)['available']
    assert r.c.hunt.recent_moves==history and r.c.hunt.travel_failures==debt and r.c.hunt.established_zone==established


@travel_scenario
async def test_tiny_step_preserves_original_direction_provenance_but_not_latest_success(r):
    await r.action('probe_forward');r.position=[10.4,10.]
    await r.action('advance_forward')
    response=dict(r.c.hunt.action_responses['forward'])
    r.position=[10.5,10.]
    await r.action('advance_forward')
    carried=r.c.hunt.action_responses['forward']
    for key in ('captured_at','frame_id','receipt_id','dx','dy','generation','source'):
        assert carried[key]==response[key]
    assert carried['latest_endpoint']==[10.5,10.] and carried['continuity_receipt_id']!=response['receipt_id']
    assert r.c.hunt.last_completed_action['status']=='unresolved_precision'
    assert r.c.hunt.last_completed_action['progress_status']=='unresolved_precision'
    assert 'unresolved_precision' in r.sage.calls[-1]['prompt']
    measurement={'zone_proposals':[r.zone],'position':r.position,'position_status':'readable_proposal'}
    now=time.time();carried['captured_at']=(datetime.now(timezone.utc)-timedelta(seconds=11)).isoformat()
    assert r.c.hunt.action_direction('forward',measurement,r.c.cycle.input_generation,r.c.cycle.session_epoch,now=now) is None
    assert r.c.hunt.action_direction('forward',measurement,r.c.cycle.input_generation,r.c.cycle.session_epoch,now=now,continuous=True)
    measurement['position']=[11.,10.]
    assert r.c.hunt.action_direction('forward',measurement,r.c.cycle.input_generation,r.c.cycle.session_epoch,continuous=True) is None
    assert not r.c.hunt.action_responses


@travel_scenario
async def test_probe_duration_spelling_unreadable_and_reselection_do_not_reset_debt(r):
    r.c.config['move_seconds']=2
    r.c.hunt.plan['area']['coordinate']=[10.8,10.]
    await r.action('probe_forward')
    assert r.c.hunt.pending['requested_duration']==1
    r.c.config['move_seconds']=.7;r.zone=' * OFFLINE   valley '
    await r.action('probe_forward')
    r.position=None
    await r.action('turn_left')
    assert not {'probe_forward','advance_forward'} & r.sage.calls[-1]['options'].keys()
    assert sum(r.c.hunt.travel_failures.values())==2
    debt=dict(r.c.hunt.travel_failures)
    r.c.hunt.choose(dict(r.c.hunt.plan['area']),r.world.capture(),'reselected',1)
    assert r.c.hunt.travel_failures==debt
    r.position=[10.,10.]
    await r.action('probe_forward')  # A completed turn is a changed evidenced approach.
    assert all(r.c.hunt.travel_failures.get(k,0)>=v for k,v in debt.items())


class TravelClock:
    def __init__(self,monkey,r):
        self.offset=0
        proxy=SimpleNamespace(time=lambda:time.time()+self.offset,monotonic=time.monotonic)
        for module in (cycle,grind_only,grind_observation,grind_search):monkey.setattr(module,'time',proxy)
        original=r.world.capture
        def capture():
            frame=original()
            return replace(frame,captured_at=datetime.fromtimestamp(time.time()+self.offset,timezone.utc).isoformat())
        r.world.capture=capture;r.c.capture=capture


@pytest.mark.parametrize('phase',['initial','ocr','provider_reserve','provider_late','guard','guard_ocr','hold'])
def test_single_source_deadline_blocks_each_expired_phase_without_gameplay(tmp_path,monkeypatch,phase):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();clock=TravelClock(monkeypatch,r);source=r.world.capture()
            requests_before=len(events(r,'sage_request_started'))
            r.sage.answers.append('probe_forward')
            if phase=='initial':source=replace(source,captured_at=(datetime.now(timezone.utc)-timedelta(seconds=13)).isoformat())
            if phase=='ocr':
                ocr=r.c.ocr
                def slow_ocr(path):
                    rows=ocr(path)
                    if path.name.startswith('acceptance-'):clock.offset=13
                    return rows
                r.c.ocr=slow_ocr
            elif phase=='provider_reserve':
                r.after_hidden=lambda:setattr(clock,'offset',9.1)
            elif phase=='provider_late':
                async def late():clock.offset=9.2
                r.sage.hook=late
            elif phase in {'guard','guard_ocr','hold'}:
                r.c.config['move_seconds']=1
                async def provider():clock.offset=8
                r.sage.hook=provider
                capture=r.c.capture
                def guard_capture():
                    if r.sage.calls:clock.offset=13 if phase=='guard' else 9 if phase=='guard_ocr' else 11.2
                    return capture()
                r.c.capture=guard_capture
                if phase=='guard_ocr':
                    ocr=r.c.ocr
                    def dispatch_ocr(path):
                        result=ocr(path)
                        if r.sage.calls:clock.offset=13
                        return result
                    r.c.ocr=dispatch_ocr
            result=await r.c.process(source)
            assert result.status=='travel_deadline_expired',(phase,result.status,result.detail)
            assert r.controls['forward']['keycode'] not in r.physical_keys() and not r.c.hunt.pending
            assert r.c.provider_failures==0 and r.c.null_answers==0
            assert events(r,'travel_decision_expired')
            if phase in {'initial','ocr','provider_reserve'}:
                assert not r.sage.calls
                assert len(events(r,'sage_request_started'))==requests_before
            if phase=='initial':assert r.toggle_count==0
            if r.c.observation_transaction:
                assert r.c.observation_transaction['travel_source_at']==pytest.approx(datetime.fromisoformat(source.captured_at).timestamp())
                assert r.c.observation_transaction['travel_deadline']==pytest.approx(datetime.fromisoformat(source.captured_at).timestamp()+12)
        finally:await r.close()
    asyncio.run(run())


def test_valid_twelve_second_lease_bypasses_old_eight_and_ten_cutoffs_and_caches_frames(tmp_path,monkeypatch):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();clock=TravelClock(monkeypatch,r);counts=Counter();ocr=r.c.ocr
            def count(path):counts[str(path)]+=1;return ocr(path)
            r.c.ocr=count
            started=time.time()
            async def provider():clock.offset=8-(time.time()-started)
            r.sage.hook=provider
            capture=r.c.capture
            def fresh():
                if r.sage.calls:clock.offset=10.5-(time.time()-started)
                return capture()
            r.c.capture=fresh
            result=await r.choose('probe_forward')
            assert result.status=='dispatched'
            tx=r.c.observation_transaction
            assert counts[tx['restored_frame']['image_path']]==1
            dispatch=result.receipt['dispatch_frame_id']
            path=next(path for path,item in r.frames.items() if item['frame'].frame_id==dispatch)
            assert counts[path]==1
            allowance=events(r,'travel_provider_budget')[-1]
            assert 0<allowance['provider_allowance_seconds']<=9
            assert allowance['validation_reserve_seconds']==3
            assert allowance['deadline']==pytest.approx(tx['travel_source_at']+12)
            assert not r.pressed
        finally:await r.close()
    asyncio.run(run())


def test_mutated_cached_source_rejected_instead_of_reanalysed(tmp_path):
    async def run():
        r=TravelRig(tmp_path)
        try:
            frame=r.world.capture();r.c._analysis_cache={}
            await r.c.rows(frame)
            with Image.open(frame.image_path) as im:
                im.putpixel((0,0),(255,0,0));im.save(frame.image_path)
            with pytest.raises(ValueError,match='Immutable OCR source hash changed'):await r.c.rows(frame)
        finally:await r.close()
    asyncio.run(run())


async def unresolved_restore(r):
    await r.travel()
    original=r.backend.key
    def ignore_restore(code,down):
        original(code,down)
        if code==6 and not down and r.toggle_count==2:r.hud=False
    r.backend.key=ignore_restore
    result=await r.c.process(r.world.capture())
    assert result.status=='grind_blocked' and r.toggle_count==2 and not r.hud
    tx=r.c.observation_transaction
    assert tx['hide_verified'] and tx['restore_receipt']['completed'] and tx['restoration_needed']
    return tx


@pytest.mark.parametrize('visibility',['visible','hidden','ambiguous','spent','no_focus','partial','cancelled','corrupted'])
def test_owned_hud_obligation_reconciliation_is_bounded_and_never_gameplay(tmp_path,visibility):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            tx=await unresolved_restore(r)
            if visibility=='visible':r.hud=True
            elif visibility=='spent':tx['correction_attempted']=True
            elif visibility=='no_focus':
                gate=r.executor.inspect_gate();r.executor.inspect_gate=lambda:replace(gate,foreground=False)
            elif visibility=='partial':
                tx['restore_receipt']['dispatch_unknown']=True;r.hud=True
            elif visibility=='corrupted':
                Path(tx['source_frame']['image_path']).write_bytes(b'corrupted')
            elif visibility=='cancelled':
                execute=r.c.cycle.execute_observation
                async def cancelled(*args,**kwargs):
                    if kwargs['chosen_option']=='grind_hud_restore_correction':raise asyncio.CancelledError()
                    return await execute(*args,**kwargs)
                r.c.cycle.execute_observation=cancelled
            frame=r.world.capture()
            if visibility=='ambiguous':
                with Image.open(frame.image_path) as im:
                    im.paste('#656565',(0,0,150,25));im.paste('#656565',(400,0,500,80));im.save(frame.image_path)
            if visibility=='cancelled':
                with pytest.raises(asyncio.CancelledError):await r.c.process(frame)
                assert tx['correction_attempted']
                assert (await r.c.process(r.world.capture())).status=='grind_blocked'
            elif visibility=='corrupted':
                with pytest.raises(Exception):await r.c.process(frame)
            else:
                result=await r.c.process(frame)
                assert result.status==('grind_reobserve' if visibility in {'visible','hidden'} else 'grind_stopped' if visibility=='partial' else 'grind_blocked')
            assert not r.sage.calls and r.controls['forward']['keycode'] not in r.physical_keys()
            assert r.toggle_count==(3 if visibility=='hidden' else 2)
            if visibility=='hidden':
                assert tx['correction_attempted'] and tx['correction_receipt']['completed'] and r.hud
                assert 'grind-travel-recovery-spec.md' in tx['correction_receipt']['authorization_reference']
            if visibility in {'visible','hidden'}:
                assert not tx['restoration_needed'] and tx['status']=='restoration_reconciled'
            if visibility in {'spent','cancelled','ambiguous'}:
                r.hud=True
                assert (await r.c.process(r.world.capture())).status=='grind_reobserve'
                assert not tx['restoration_needed'] and r.toggle_count==2
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('phase',['hidden','restored','guard'])
@pytest.mark.parametrize('error',['focus','generic','source','runtime'])
def test_clean_observation_and_dispatch_propagate_capture_errors_after_bookkeeping(tmp_path,phase,error):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();capture=r.c.capture
            failure=ForegroundCaptureInterrupted if error=='focus' else RuntimeError if error=='runtime' else CaptureError
            def failing():
                selected=(not r.hud if phase=='hidden' else r.toggle_count==2 if phase=='restored' else bool(r.sage.calls))
                if selected:
                    if error=='source':return replace(capture(),source='wrong-window')
                    raise failure('fake foreground interruption' if error=='focus' else 'fake generic capture failure')
                return capture()
            r.c.capture=failing;r.sage.answers.append('probe_forward')
            expected=ValueError if error=='source' and phase!='guard' else RuntimeError if error=='runtime' and phase!='guard' else CaptureError
            with pytest.raises(expected):
                await r.c.process(r.world.capture())
            assert r.c.observation_transaction['final_generation']==r.c.cycle.input_generation
            assert r.controls['forward']['keycode'] not in r.physical_keys() and not r.pressed
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('when',['done','cancellation'])
@pytest.mark.parametrize('poison',[False,True])
def test_pause_await_accepts_only_typed_focus_and_entire_clean_ledger(tmp_path,when,poison):
    async def run():
        runner,c,f,s,b,store,e=attached(tmp_path);e.arm()
        try:
            if poison:
                store.append(Event.create('execution_receipt',{'generation_before':0,'generation_after':1,
                    'possible_input':True,'completed':False,'dispatch_unknown':True,'input_steps':[{'status':'effect_unknown'}]}))
                c.cycle._input_generation=1
            entered=asyncio.Event()
            async def interrupted():
                entered.set()
                if when=='cancellation':
                    try:await asyncio.Event().wait()
                    except asyncio.CancelledError:pass
                raise ForegroundCaptureInterrupted('fake narrow focus interruption')
            runner.task=asyncio.create_task(interrupted());await entered.wait()
            if when=='done':await asyncio.sleep(0)
            await runner.pause()
            assert runner.state==('PLAYING' if poison else 'PAUSED_FOCUS')
            assert runner.stopping==poison and not e.armed and runner.task is None
            if poison:assert runner.reason=='partial_or_unknown_grind_input'
        finally:await cleanup(e,store)
    asyncio.run(run())


@pytest.mark.parametrize('typed',[True,False])
def test_actual_runner_done_task_focus_exception_pauses_but_generic_stops(tmp_path,typed):
    async def run():
        p=profile(tmp_path);frames=Frames(tmp_path);sage=Sage(['world_normal_confirmed','player_level_1'])
        backend=Backend();rect=Rect(0,0,500,300);good=GateSnapshot(7,7,True,rect,rect,True,True)
        executor=SafeExecutor(backend,lambda:good,heartbeat_timeout=30,watchdog_interval=.01)
        calls=0
        def capture():
            nonlocal calls
            calls+=1
            if calls==1:raise (ForegroundCaptureInterrupted if typed else CaptureError)('fake capture interruption')
            return frames.capture()
        runner=GrindRunner(p,tmp_path/'run',sage=sage,executor=executor,capture=capture,ocr=frames.ocr,freeze_factory=Freeze)
        task=asyncio.create_task(runner.run())
        try:
            if typed:
                await eventually(lambda:runner.store and bool(events(runner,'grind_paused')))
                paused=events(runner,'grind_paused')[-1]
                assert paused['state']=='PAUSED_FOCUS' and paused['input_reconciled']
                assert runner.input_reconciled() and not runner.stopping
                runner.request_stop();await asyncio.wait_for(task,2)
            else:
                with pytest.raises(CaptureError):await asyncio.wait_for(task,2)
                assert runner.state=='STOPPED' and 'CaptureError' in runner.reason
        finally:
            if not task.done():runner.request_stop();await asyncio.wait_for(task,2)
    asyncio.run(run())


def test_late_provider_that_suppresses_cancellation_cannot_spend_validation_reserve(tmp_path,monkeypatch):
    async def run():
        r=TravelRig(tmp_path)
        try:
            await r.travel();clock=TravelClock(monkeypatch,r)
            provider=r.sage.decide_image_choice
            cancelled=[]
            async def stubborn(*args,**kwargs):
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(True);clock.offset=9.5
                    return await provider(*args,**kwargs)
            r.sage.decide_image_choice=stubborn
            r.sage.answers.append('probe_forward')
            # A short real timeout drives cancellation; the synthetic clock
            # establishes that the returned answer missed D-reserve, before D.
            original=cycle.asyncio.wait_for
            async def bounded(awaitable,timeout):return await original(awaitable,timeout=.02)
            monkeypatch.setattr(cycle.asyncio,'wait_for',bounded)
            result=await r.c.process(r.world.capture())
            assert cancelled and result.status=='travel_deadline_expired'
            assert events(r,'travel_decision_expired')[-1]['phase']=='late_sage_response'
            assert not r.physical_keys() and not r.c.hunt.pending
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['telemetry','cached_digest','clean_digest'])
def test_continuity_telemetry_and_corruption_have_distinct_outcomes(tmp_path,failure):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel()
            if failure=='telemetry':
                async def changed():r.world.player_health='red'
                r.sage.hook=changed
            elif failure=='clean_digest':
                async def mutate():
                    path=r.c.observation_transaction['hidden_frame']['image_path']
                    with Image.open(path) as im:im.putpixel((0,0),(255,0,0));im.save(path)
                r.sage.hook=mutate
            else:
                rows=r.c.rows
                async def mutate_cached(frame):
                    result=await rows(frame)
                    if r.sage.calls and frame.frame_id!=r.c.observation_transaction['restored_frame_id']:
                        with Image.open(frame.image_path) as im:im.putpixel((0,0),(255,0,0));im.save(frame.image_path)
                    return result
                r.c.rows=mutate_cached
            r.sage.answers.append('probe_forward')
            if failure=='telemetry':
                result=await r.c.process(r.world.capture())
                assert result.status=='dispatch_guard_rejected' and 'telemetry changed' in result.detail
                assert not events(r,'travel_decision_expired')
            else:
                with pytest.raises(CaptureError,match='hash changed'):await r.c.process(r.world.capture())
            assert r.controls['forward']['keycode'] not in r.physical_keys()
        finally:await r.close()
    asyncio.run(run())


def test_completed_but_ineffective_hud_correction_remains_spent_until_visible(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            tx=await unresolved_restore(r)
            key=r.backend.key
            def no_correction_effect(code,down):
                key(code,down)
                if code==6 and not down and r.toggle_count==3:r.hud=False
            r.backend.key=no_correction_effect
            assert (await r.c.process(r.world.capture())).status=='grind_blocked'
            assert tx['correction_attempted'] and tx['correction_receipt']['completed']
            assert tx['restoration_needed'] and r.toggle_count==3
            assert (await r.c.process(r.world.capture())).status=='grind_blocked'
            assert r.toggle_count==3
            r.c.pause_focus();r.c.resume_focus()
            assert (await r.c.process(r.world.capture())).status=='grind_blocked'
            assert tx['correction_attempted'] and r.toggle_count==3
            r.hud=True
            assert (await r.c.process(r.world.capture())).status=='grind_reobserve'
            assert not tx['restoration_needed'] and r.toggle_count==3 and not r.sage.calls
            assert r.c.require_world  # Clearing HUD never revives old gameplay authority.
        finally:await r.close()
    asyncio.run(run())
