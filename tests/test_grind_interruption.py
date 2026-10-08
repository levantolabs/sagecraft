"""Interrupted movement reconciliation: fake native input and retained ledgers."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import time

import pytest

from sage_wow.agent.grind_interruption import released_movement
from sage_wow.models import Event
from test_grind_focus import attached, eventually
from test_grind_identity_recovery import attach_gate, events, gates
from test_grind_only import cleanup, profile


def evidence():
    receipt = dict(receipt_id='receipt', request_id='request', source_frame_id='source',
        dispatch_frame_id='dispatch', chosen_option='probe_forward', session_epoch='epoch',
        generation_before=0, generation_after=2, authorization_type='sage_decision',
        selected_binding=dict(type='keypress', keycode=1, hold_seconds=1),
        completed=False, error='CancelledError', dispatch_unknown=False, possible_input=True)
    receipt['input_steps'] = [dict(kind=kind, execution_id='receipt', details={'keycode':1},
        attempted=True, status='completed', dispatch_started_at_monotonic=start,
        dispatch_completed_at_monotonic=end) for kind,start,end in [('key_down',1,2),('key_up',6,7)]]
    guard = dict(approved=True, request_id='request', source_frame_id='source',
        dispatch_frame_id='dispatch', chosen='probe_forward', session_epoch='epoch', input_generation=0,
        evidence=dict(selected_target_required=False, predicates=dict(player_badge=True,ui_clear=True),
            failed_predicates=[]))
    recovery = {'observation':{'monotonic_at':3}}
    release = dict(execution_id='receipt', source='identity_unavailable', reason='focus_or_calibration_invalid',
        release_error=None, held_inputs=[dict(kind='key',code=1,held_seconds=.25)],
        release_started_at_monotonic=4, release_attempt_completed_at_monotonic=5)
    return receipt,recovery,guard,release


@pytest.mark.parametrize('action,key', [('forward',1),('backward',2),('turn_left',3),('turn_right',4),
    ('strafe_left',10),('strafe_right',11)])
def test_general_movement_classifier_preserves_incomplete_receipt(tmp_path,action,key):
    p=profile(tmp_path);r,recovery,g,release=evidence()
    p.values['controls']['bindings'][action]={'keycode':key,'verified_from':'fake'}
    r['selected_binding']['keycode']=key
    for step in r['input_steps']:step['details']['keycode']=key
    release['held_inputs'][0]['code']=key
    before=deepcopy(r)
    assert released_movement(p,r,recovery,g,release)==action
    assert r==before and not r['completed']


@pytest.mark.parametrize('change', ['cast','text','chord','unverified','alias','duration_nan',
    'unknown','wrong_authority','not_cancelled','missing_up','extra_step','unknown_step','wrong_key',
    'wrong_execution','guard_missing','guard_request','guard_epoch','guard_generation','guard_frame',
    'guard_target','guard_ui','guard_rejected','release_failed','release_missing','release_execution',
    'release_source','release_multiple','release_key','release_time','release_nan','release_duration',
    'identity_release_failed'])
def test_incomplete_or_unattributed_input_is_not_reconciled(tmp_path,change):
    p=profile(tmp_path);r,recovery,g,release=evidence()
    if change=='cast':r['selected_binding']['keycode']=5
    elif change in {'text','chord'}:r['selected_binding']['type']=change
    elif change=='unverified':p.values['controls']['bindings']['forward'].pop('verified_from')
    elif change=='alias':p.values['controls']['bindings']['backward']['keycode']=1
    elif change=='duration_nan':r['selected_binding']['hold_seconds']=float('nan')
    elif change=='unknown':r['dispatch_unknown']=True
    elif change=='wrong_authority':r['authorization_type']='prior_sage_continuation'
    elif change=='not_cancelled':r['error']='ExecutionRejected'
    elif change=='missing_up':r['input_steps'].pop()
    elif change=='extra_step':r['input_steps'].append(deepcopy(r['input_steps'][1]))
    elif change=='unknown_step':r['input_steps'][1]['status']='effect_unknown'
    elif change=='wrong_key':r['input_steps'][1]['details']['keycode']=2
    elif change=='wrong_execution':r['input_steps'][1]['execution_id']='other'
    elif change=='guard_missing':g={}
    elif change=='guard_request':g['request_id']='other'
    elif change=='guard_epoch':g['session_epoch']='other'
    elif change=='guard_generation':g['input_generation']=2
    elif change=='guard_frame':g['dispatch_frame_id']='other'
    elif change=='guard_target':g['evidence']['selected_target_required']=True
    elif change=='guard_ui':g['evidence']['predicates']['ui_clear']=False
    elif change=='guard_rejected':g['approved']=False
    elif change=='release_failed':release['release_error']='OSError'
    elif change=='release_missing':release={}
    elif change=='release_execution':release['execution_id']='other'
    elif change=='release_source':release['source']='focus'
    elif change=='release_multiple':release['held_inputs']*=2
    elif change=='release_key':release['held_inputs'][0]['code']=2
    elif change=='release_time':release['release_started_at_monotonic']=1
    elif change=='release_nan':release['release_started_at_monotonic']=float('nan')
    elif change=='release_duration':release['held_inputs'][0]['held_seconds']=2
    elif change=='identity_release_failed':recovery['release_error']='OSError'
    assert released_movement(p,r,recovery,g,release) is None


async def interrupt_guarded_movement(r,c,f,b,store,e,gate,missing,*,phase='search',key=1):
    c.hunt.phase=phase
    frame=f.capture();request='approved-movement'
    if phase=='travel':
        c.hunt.choose({'id':'offline_interrupted_route','label':'Offline interrupted route',
            'coordinate':[20.,10.],'zone_reference':'Offline Valley'},frame,'offline-travel-choice',1)
    _,_,guard,_=evidence()
    guard.update(request_id=request,source_frame_id=frame.frame_id,dispatch_frame_id=frame.frame_id,
        session_epoch=c.cycle.session_epoch,input_generation=c.cycle.input_generation)
    store.append(Event.create('dispatch_guard_checked',guard))
    r.task=asyncio.create_task(c.cycle._dispatch_with_receipt(
        {'type':'keypress','keycode':key,'hold_seconds':.5},frame=frame,request_id=request,
        session_epoch=c.cycle.session_epoch,candidate_set_version='fake',chosen_option='probe_forward'))
    await eventually(lambda:any(x[0]=='key' and x[-1] for x in b.events))
    gate[0]=missing;assert r.gate_state()=='identity'
    assert not e.armed and b.events[-1]==('release',)
    await r.pause()


@pytest.mark.parametrize('phase',['search','travel'])
def test_guarded_movement_releases_then_requires_fresh_world_without_replay(tmp_path,phase):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path);good,missing=gates();gate=[good]
        attach_gate(r,e,gate);e.arm();epoch=c.cycle.session_epoch;deadline=c.deadline
        try:
            await interrupt_guarded_movement(r,c,f,b,store,e,gate,missing,phase=phase)
            receipt=deepcopy(c.cycle.last_receipt)
            assert r.state=='PAUSED_IDENTITY' and not r.stopping and not c.stopped
            assert not receipt['completed'] and receipt['error']=='CancelledError'
            assert len(receipt['input_steps'])==2 and r.input_reconciled()
            assert c.hunt.unassessed[-1]['status']=='unknown_unassessed' and not c.hunt.retry_credit
            assert bool(c.hunt.unresolved_motion)==(phase=='search')
            assert c.world_resume_phase==phase
            assert events(store,'grind_interrupted_movement_reconciled')[-1]['gameplay_outcome']=='unknown'
            count=len(b.events);assert not r.resume() and len(b.events)==count
            gate[0]=good;assert r.resume() and c.require_world
            assert c.cycle.session_epoch!=epoch and c.deadline==deadline and c.cycle.last_receipt==receipt
            assert sum(x[0]=='key' and x[-1] for x in b.events)==1
            # A later focus pause before any new receipt must retain reconciliation.
            r.state='PLAYING';gate[0]=replace(good,foreground=False);await r.pause()
            assert r.state=='PAUSED_FOCUS' and not r.stopping
            gate[0]=good;assert r.resume() and c.require_world
            assert c.cycle.last_receipt==receipt and r.input_reconciled()
            assert len(events(store,'grind_interrupted_movement_reconciled'))==1
        finally:await cleanup(e,store)
    asyncio.run(run())


@pytest.mark.parametrize('change',['receipt_mutated','ledger_gap','earlier_unknown','owner_changed','operator','deadline'])
def test_reconciled_movement_cannot_hide_later_violations(tmp_path,change):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path);good,missing=gates();gate=[good]
        attach_gate(r,e,gate);e.arm()
        try:
            await interrupt_guarded_movement(r,c,f,b,store,e,gate,missing)
            assert r.state=='PAUSED_IDENTITY'
            gate[0]=good
            if change=='receipt_mutated':
                c.cycle._last_receipt['selected_binding']['hold_seconds']=.7
                store.connection.execute("UPDATE events SET payload_json=? WHERE event_type='execution_receipt'",(json.dumps(c.cycle.last_receipt),))
            elif change=='ledger_gap':c.cycle._input_generation+=1
            elif change=='earlier_unknown':
                receipt=deepcopy(c.cycle.last_receipt);receipt['dispatch_unknown']=True
                store.connection.execute("UPDATE events SET payload_json=? WHERE event_type='execution_receipt'",(json.dumps(receipt),))
            elif change=='owner_changed':
                changed=deepcopy(good.identity_evidence);changed['window']['owner_pid']=43
                gate[0]=replace(good,identity_evidence=changed)
                assert r.gate_state()=='invalid'
            elif change=='operator':await r.pause(operator=True)
            elif change=='deadline':c.deadline=time.time()-1
            assert not r.resume() and not e.armed
            if change=='operator':assert r.state=='PAUSED_OPERATOR' and not r.stopping
            else:assert r.stopping
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_released_final_movement_cannot_reconcile_an_earlier_unknown_receipt(tmp_path):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path);good,missing=gates();gate=[good]
        attach_gate(r,e,gate);e.arm()
        try:
            store.append(Event.create('execution_receipt',dict(generation_before=0,generation_after=0,
                possible_input=True,dispatch_unknown=True,input_steps=[],completed=False)))
            await interrupt_guarded_movement(r,c,f,b,store,e,gate,missing)
            assert r.stopping and r.reason=='partial_or_unknown_grind_input'
            assert not r.reconciled_movements and not events(store,'grind_interrupted_movement_reconciled')
        finally:await cleanup(e,store)
    asyncio.run(run())


def test_cast_with_same_guard_is_still_terminal(tmp_path):
    async def run():
        r,c,f,s,b,store,e=attached(tmp_path);good,missing=gates();gate=[good]
        attach_gate(r,e,gate);e.arm()
        try:
            await interrupt_guarded_movement(r,c,f,b,store,e,gate,missing,key=5)
            assert r.stopping and r.reason=='partial_or_unknown_grind_input'
            assert not events(store,'grind_interrupted_movement_reconciled')
        finally:await cleanup(e,store)
    asyncio.run(run())
