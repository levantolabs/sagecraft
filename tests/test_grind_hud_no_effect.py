"""Owned failed HUD hide: synthetic images/receipts, no capture or native input."""
import asyncio
from dataclasses import replace
from pathlib import Path
import time

import pytest
from PIL import Image
from sage_wow.agent.grind_observation import visible_player_proof
from sage_wow.perception.ocr import TextObservation
from test_grind_travel_acceptance import TravelRig
from test_grind_travel_recovery import events,TravelClock
from test_direct_grind import notice_rows


def row(text,x=40,y=5,w=90,h=12,confidence=.99):
    return TextObservation(text,confidence,{'x':x,'y':y,'width':w,'height':h})


@pytest.mark.parametrize('case,expected',[
    ('exact',True),('case_space',True),('split',True),('missing',False),('outside',False),
    ('low_confidence',False),('duplicate',False),('conflict',False),('punctuation',False),
    ('same_line',False),('separated',False),('intervening',False),('surname_nested',True),('wrong_surname',False)])
def test_positive_visible_proof_is_exact_contained_adjacent_and_unambiguous(tmp_path,case,expected):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            rows=[row('Test Player')]
            if case=='case_space':rows=[row('  TEST   player  ')]
            elif case=='split':rows=[row('Test',y=5,h=10),row('Player',y=17,h=10)]
            elif case=='missing':rows=[]
            elif case=='outside':rows=[row('Test Player',x=160)]
            elif case=='low_confidence':rows=[row('Test Player',confidence=.74)]
            elif case=='duplicate':rows+=[row('Test Player',y=25)]
            elif case=='conflict':rows+=[row('Test Wrong',y=25)]
            elif case=='punctuation':rows=[row('Test Player!')]
            elif case=='same_line':rows=[row('Test',x=10),row('Player',x=70)]
            elif case=='separated':rows=[row('Test',y=5),row('Player',y=50)]
            elif case=='intervening':rows=[row('Test',y=5,h=10),row('1',y=16,h=1),row('Player',y=18,h=10)]
            elif case in {'surname_nested','wrong_surname'}:
                r.c.profile.values['character'].update(name='Test',surname='Player')
                rows=[row('Test',y=5,h=10),row('Player' if case=='surname_nested' else 'Wrong',y=17,h=10)]
            proof=visible_player_proof(r.c,r.world.capture(),rows)
            assert proof['verified']==expected
            if expected:assert proof['matching_locations']==1 and proof['source_sha256'] and proof['rows']
        finally:await r.close()
    asyncio.run(run())


def ignore_chords(r):
    original=r.backend.key
    def ignore(code,down):
        original(code,down)
        if code==6 and not down:r.hud=True
    r.backend.key=ignore
    return original


def add_notice(r):
    visible=[True];ocr=r.c.ocr
    r.c.ocr=lambda path:ocr(path)+(notice_rows() if visible[0] else [])
    r.backend.mouse_move=lambda *args:r.backend.events.append(('move',*args))
    def button(x,y,code,down):
        r.backend.events.append(('mouse',x,y,code,down))
        if not down:visible[0]=False
    r.backend.mouse_button=button
    return visible


async def failed_hide(r):
    calls=len(r.sage.calls)
    result=await r.c.process(r.world.capture())
    assert result.status=='grind_blocked' and r.toggle_count>=1
    t=r.c.observation_transaction
    assert t['hide_receipt']['completed'] and not t['hide_verified'] and t['restoration_needed']
    assert not t.get('restore_receipt') and len(r.sage.calls)==calls
    return t


async def retire_no_effect(r):
    calls=len(r.sage.calls)
    t=r.c.observation_transaction
    result=await r.c.process(r.world.capture())
    assert result.status=='grind_reobserve' and not t['restoration_needed']
    assert t['status']=='hide_no_effect_visible' and t['no_effect_charged'] and not t['hide_verified']
    assert len(t['no_effect_samples'])==2 and len({sample['frame_id'] for sample in t['no_effect_samples']})==2
    assert not t.get('restore_receipt') and not t.get('correction_receipt') and len(r.sage.calls)==calls
    return t


def test_near_equal_hide_two_visible_samples_retire_with_no_toggle_and_next_notice_is_handled(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();ignore_chords(r)
            t=await failed_hide(r)
            assert t['source_visible_proof']['verified'] and max(t['hide_difference'].values())<=2
            visible=add_notice(r)
            await retire_no_effect(r)
            assert r.toggle_count==1 and r.c.hud_no_effect_count==1
            assert r.c.hunt.blocked is None and not r.casts() and r.physical_keys()==[58,6]
            # Reprocessing its incoming sample cannot reuse the failed decision.
            assert t['hud_no_effect_count']==1
            r.sage.answers.append('acknowledge_notice');r.c.wait_until=0
            result=await r.c.process(r.world.capture())
            assert result.status=='dispatched' and r.c.hunt.phase=='ui_recover'
            assert r.toggle_count==1 and len(r.sage.calls)==1 and not visible[0]
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('case',['one_region','delayed_hidden','no_second','same_sample',
    'focus','partial','generation','corrupt','geometry','same_time'])
def test_no_effect_branch_cannot_clear_ambiguous_unowned_or_unsettled_evidence(tmp_path,case):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();ignore_chords(r)
            t=await failed_hide(r);before=r.toggle_count
            frame=r.world.capture()
            if case=='one_region':
                with Image.open(frame.image_path) as im:im.paste('#000000',(400,0,500,80));im.save(frame.image_path)
            elif case=='delayed_hidden':
                capture=r.c.capture
                def late():r.hud=False;return capture()
                r.c.capture=late
            elif case=='no_second':
                capture=r.c.capture
                gate=r.executor.inspect_gate();foreground=[True]
                r.executor.inspect_gate=lambda:replace(gate,foreground=foreground[0])
                def lose_focus():foreground[0]=False;return capture()
                r.c.capture=lose_focus
            elif case=='same_sample':r.c.capture=lambda:frame
            elif case=='same_time':r.c.capture=lambda:replace(r.world.capture(),captured_at=frame.captured_at)
            elif case=='focus':
                gate=r.executor.inspect_gate();r.executor.inspect_gate=lambda:replace(gate,foreground=False)
            elif case=='partial':t['hide_receipt']['dispatch_unknown']=True
            elif case=='generation':r.c.cycle._input_generation+=1
            elif case=='corrupt':Path(t['source_frame']['image_path']).write_bytes(b'corrupt')
            elif case=='geometry':frame=replace(frame,source='wrong-window')
            if case in {'corrupt','geometry','same_sample'}:
                with pytest.raises(Exception):await r.c.process(frame)
            else:
                result=await r.c.process(frame)
                assert result.status==('grind_stopped' if case in {'partial','generation'} else 'grind_blocked')
            assert t['restoration_needed'] and r.c.hud_no_effect_count==0
            if case=='no_second':assert len(t['no_effect_samples'])==1
            assert r.toggle_count==before and not r.sage.calls and not r.casts()
        finally:await r.close()
    asyncio.run(run())


def test_two_no_effect_attempts_keep_session_debt_across_unblock_phase_focus_and_frames(tmp_path,monkeypatch):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            # Isolate HUD reassessment from the independent scheduled level check.
            r.c.level.interval_seconds=300
            r.c.config['level_check_seconds']=300
            await r.travel();ignore_chords(r);deadline=r.c.deadline;clock=TravelClock(monkeypatch,r)
            first=await failed_hide(r);await retire_no_effect(r)
            r.c.pause_focus();r.c.resume_focus();r.c.wait_until=0
            await r.choose('world_normal_confirmed')
            second=await failed_hide(r);assert second['transaction_id']!=first['transaction_id']
            await retire_no_effect(r)
            assert r.c.hud_no_effect_count==2 and r.toggle_count==2 and r.c.deadline==deadline
            assert r.c.hunt.blocked['reason']=='blocked_hud_capture_unavailable'
            count=len(r.sage.calls);clock.offset=61;r.c.wait_until=0
            await r.choose('blocked_changed_assessment')
            assert not r.c.hunt.blocked and r.c.hud_no_effect_count==2
            r.c.hunt.phase='search';r.world.name='Arbitrary Selected Unit'
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_blocked' and len(r.sage.calls)==count+1
            assert r.toggle_count==2 and not r.casts() and r.physical_keys()==[58,6,58,6]
            assert await r.c.observation.capture(r.world.capture()) is None
            assert r.toggle_count==2 and r.c.hud_no_effect_count==2
        finally:await r.close()
    asyncio.run(run())


def test_retry_requires_current_visible_proof_after_delayed_actual_hide(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();original=ignore_chords(r)
            await failed_hide(r);await retire_no_effect(r)
            r.backend.key=original;r.hud=False
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_blocked' and r.toggle_count==1 and not r.sage.calls
            assert not r.c.observation_transaction['hide_verified'] and r.c.hud_no_effect_count==1
            assert events(r,'grind_hud_retry_visibility_unresolved')
            r.hud=True;r.c.hunt.blocked=None;r.c.wait_until=0
            await r.action('probe_forward')
            assert r.toggle_count==3 and r.c.hud_no_effect_count==0 and r.c.observation_transaction['status']=='restored_verified'
        finally:await r.close()
    asyncio.run(run())


def test_verified_later_restoration_resets_streak_but_failed_restore_receipt_does_not(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();original=ignore_chords(r)
            await failed_hide(r);await retire_no_effect(r)
            def fail_restore(code,down):
                original(code,down)
                if code==6 and not down and r.toggle_count==3:r.hud=False
            r.backend.key=fail_restore
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_blocked' and r.c.hud_no_effect_count==1
            t=r.c.observation_transaction
            assert t['hide_verified'] and t['restore_receipt']['completed'] and t['restoration_needed']
            r.hud=True
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_reobserve' and r.c.hud_no_effect_count==0 and not t['restoration_needed']
            assert t['status']=='restoration_reconciled' and r.toggle_count==3
        finally:await r.close()
    asyncio.run(run())


def test_stop_or_original_deadline_dominates_no_effect_reconciliation(tmp_path,monkeypatch):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();ignore_chords(r);await failed_hide(r)
            clock=TravelClock(monkeypatch,r);end=time.time()+10;r.c.deadline=end
            capture=r.c.capture
            def expires():clock.offset=11;return capture()
            r.c.capture=expires
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_blocked' and r.c.observation_transaction['restoration_needed']
            assert r.c.deadline==end and r.c.hud_no_effect_count==0 and r.toggle_count==1
            r.c.stop('operator_stop');result=await r.c.process(r.world.capture())
            assert result.status=='grind_stopped' and r.toggle_count==1 and not r.sage.calls
        finally:await r.close()
    asyncio.run(run())


def test_terminal_capability_debt_allows_guarded_notice_and_scheduled_level_only(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();ignore_chords(r)
            for _ in range(2):await failed_hide(r);await retire_no_effect(r)
            assert r.c.hud_no_effect_count==2
            calls=len(r.sage.calls);visible=add_notice(r)
            r.c.level.last_attempt_at-=300
            await r.choose('acknowledge_notice')
            assert not visible[0] and r.c.hunt.phase=='ui_recover'
            assert 'player_level_1' not in r.sage.calls[-1]['options']
            await r.choose('world_normal_confirmed')
            await r.choose('player_level_2')
            assert r.c.level.last_confirmed_level==2 and r.c.hud_no_effect_count==2
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_blocked' and len(r.sage.calls)==calls+3
            assert r.toggle_count==2 and not r.casts() and r.physical_keys()==[58,6,58,6]
            assert len([event for event in r.backend.events if event[0]=='mouse' and event[-1]])==1
        finally:await r.close()
    asyncio.run(run())


def test_repeated_same_transaction_reconciliation_cannot_double_charge_no_effect(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();ignore_chords(r);await failed_hide(r)
            t=await retire_no_effect(r)
            result=await r.c.observation.reconcile(r.world.capture())
            assert result.status=='grind_reobserve' and r.c.hud_no_effect_count==1
            assert r.c.observation_transaction is t and t['no_effect_charged']
            assert t['hud_no_effect_count']==1 and r.toggle_count==1 and not r.sage.calls
        finally:await r.close()
    asyncio.run(run())
