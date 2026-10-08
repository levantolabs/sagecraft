"""Owned HUD transitions: delayed rendering and bounded recovery, all fake IO."""
import asyncio
from dataclasses import replace

import pytest
from PIL import Image

from sage_wow.agent.grind_observation import HUD_RECONCILIATION_LIMIT
from test_grind_hud_no_effect import failed_hide,ignore_chords
from test_grind_travel_acceptance import TravelRig
from test_grind_travel_recovery import events


@pytest.mark.parametrize('phase',['hide','restore','both'])
def test_delayed_rendering_is_observed_before_clean_world_gameplay(tmp_path,phase):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();key=r.backend.key;capture=r.c.capture;pending=[]
            def delayed(code,down):
                key(code,down)
                if code==6 and not down and ((r.toggle_count==1 and phase in {'hide','both'})
                    or (r.toggle_count==2 and phase in {'restore','both'})):
                    pending[:]=[r.hud,2];r.hud=not r.hud
            def sample():
                if pending:
                    pending[1]-=1
                    if pending[1]<0:r.hud=pending.pop(0);pending.clear()
                return capture()
            r.backend.key=delayed;r.c.capture=sample
            await r.action('probe_forward')
            t=r.c.observation_transaction
            assert t['status']=='restored_verified' and not t['restoration_needed']
            assert len(t['hidden_witnesses'])==2 and r.hud and r.toggle_count==2
            assert all(view[1] for view in r.trace if view[0]=='provider')
            if phase in {'hide','both'}:assert len(t['hide_samples'])>=4
            if phase in {'restore','both'}:assert len(t['restore_samples'])>=3
            assert r.controls['forward']['keycode'] in r.physical_keys()
        finally:await r.close()
    asyncio.run(run())


def test_hidden_transition_after_initial_window_recovers_once_without_gameplay(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();key=ignore_chords(r);t=await failed_hide(r)
            initial=list(t['hide_samples']);r.backend.key=key;r.hud=False
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_reobserve' and r.hud and r.toggle_count==2
            assert t['status']=='restoration_reconciled' and not t['restoration_needed']
            assert t['hide_samples']==initial and t['hidden_witnesses'] and t['delayed_hide_samples']
            assert t['correction_attempted'] and t['correction_receipt']['completed']
            assert not r.sage.calls and not r.casts()
            assert r.physical_keys()==[58,6,58,6]
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('case',['blank','missing_identity'])
def test_first_hide_requires_positive_visible_source(tmp_path,case):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel()
            if case=='blank':r.hud=False
            else:
                ocr=r.c.ocr;r.c.ocr=lambda path:[row for row in ocr(path) if row.text!='Test Player']
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_blocked' and r.toggle_count==0 and not r.sage.calls
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('case',['black_screen','changed_world','remaining_minimap','same_time','focus','stop','generation'])
def test_delayed_hidden_witness_cannot_override_ambiguous_pixels_or_lost_authority(tmp_path,case):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();key=ignore_chords(r);t=await failed_hide(r)
            r.backend.key=key;r.hud=False;capture=r.c.capture
            def modified():
                frame=capture()
                if case in {'black_screen','changed_world','remaining_minimap'}:
                    with Image.open(frame.image_path) as im:
                        if case=='black_screen':im.paste('black',(0,0,500,300))
                        elif case=='changed_world':im.paste('#eeffff',(100,100,400,250))
                        else:im.paste('#4477bb',(400,0,500,80))
                        im.save(frame.image_path)
                return frame
            frame=modified();r.c.capture=modified
            if case=='same_time':r.c.capture=lambda:replace(capture(),captured_at=frame.captured_at)
            if case in {'focus','stop','generation'}:
                loop=asyncio.get_running_loop()
                def interrupt():
                    fresh=capture()
                    if case=='focus':
                        gate=r.executor.inspect_gate();r.executor.inspect_gate=lambda:replace(gate,foreground=False)
                    elif case=='stop':loop.call_soon_threadsafe(r.c.stop,'operator_stop')
                    else:r.c.cycle._input_generation+=1
                    return fresh
                r.c.capture=interrupt
            result=await r.c.process(frame)
            assert result.status in {'grind_blocked','grind_stopped'}
            assert t['restoration_needed'] and not t.get('correction_receipt')
            assert r.toggle_count==1 and not r.sage.calls and not r.casts()
        finally:await r.close()
    asyncio.run(run())


def test_unresolved_restoration_budget_survives_fresh_frames_and_focus_cycles(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();ignore_chords(r);t=await failed_hide(r)
            # A visibly different world cannot establish the hidden witness.
            for attempt in range(HUD_RECONCILIATION_LIMIT):
                r.c.pause_focus();r.c.resume_focus();r.hud=False
                frame=r.world.capture()
                with Image.open(frame.image_path) as im:im.paste('black',(0,0,500,300));im.save(frame.image_path)
                result=await r.c.process(frame)
                assert t['reconciliation_attempts']==attempt+1
            assert result.status=='grind_stopped' and r.c.reason=='hud_restoration_unresolved'
            assert t['restoration_needed'] and t['status']=='restoration_terminal_unresolved'
            assert len(events(r,'grind_hud_restoration_exhausted'))==1
            assert r.toggle_count==1 and not r.pressed and not r.sage.calls
        finally:await r.close()
    asyncio.run(run())


def test_each_slow_ocr_pass_cannot_evade_terminal_budget_by_expiring_its_lease(tmp_path,monkeypatch):
    from test_grind_travel_recovery import TravelClock
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();ignore_chords(r);t=await failed_hide(r)
            clock=TravelClock(monkeypatch,r);r.hud=False;rows=r.c.rows
            async def slow(frame):
                result=await rows(frame);clock.offset+=11;return result
            r.c.rows=slow
            for attempt in range(HUD_RECONCILIATION_LIMIT):
                result=await r.c.process(r.world.capture())
                assert t['reconciliation_attempts']==attempt+1
            assert result.status=='grind_stopped' and r.c.reason=='hud_restoration_unresolved'
            assert t['restoration_needed'] and r.toggle_count==1 and not r.sage.calls
        finally:await r.close()
    asyncio.run(run())


def test_recent_visible_capture_from_before_restore_cannot_clear_obligation(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();capture=r.c.capture
            def cached():
                frame=capture()
                if r.toggle_count==2:
                    # Distinct ID and valid geometry, but pixels carry a time
                    # before the completed restore command.
                    frame=replace(frame,captured_at=r.c.observation_transaction['hidden_captured_at'])
                return frame
            r.c.capture=cached
            with pytest.raises(Exception,match='predates completed input'):
                await r.c.process(r.world.capture())
            t=r.c.observation_transaction
            assert t['restoration_needed'] and t['status']=='observation_unresolved'
            assert r.toggle_count==2 and not r.sage.calls and not r.casts()
        finally:await r.close()
    asyncio.run(run())


def test_delayed_correction_rendering_is_settled_without_repeated_toggle(tmp_path):
    async def run():
        r=TravelRig(tmp_path,clean=True)
        try:
            await r.travel();key=ignore_chords(r);t=await failed_hide(r)
            r.hud=False;capture=r.c.capture;pending=[]
            def delayed(code,down):
                key(code,down)
                if code==6 and not down and r.toggle_count==2:r.hud=False;pending[:]=[2]
            def sample():
                if pending:
                    pending[0]-=1
                    if pending[0]<0:r.hud=True;pending.clear()
                return capture()
            r.backend.key=delayed;r.c.capture=sample
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_reobserve' and not t['restoration_needed']
            assert len(t['correction_samples'])>=3 and r.toggle_count==2 and r.hud
            assert not r.sage.calls
        finally:await r.close()
    asyncio.run(run())
