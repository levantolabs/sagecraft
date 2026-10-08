"""Natural delayed recovery and canceled probe traces; offline IO/providers only."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import time

import pytest

from sage_wow.agent import grind_resources
from sage_wow.agent.grind_encounter_recovery import completed_smite_submission, consume_probe
from test_grind_survival_guard import SurvivalRig
from test_grind_product_spec import Rig
from test_active_attacker_recovery import start, install_hud, facing_failure, smites
from test_grind_trial35_recovery import entry_world


def virtual_clock(r, monkeypatch):
    """Advance elapsed time, including authentic fixture frame/receipt timestamps."""
    from sage_wow.control import receipts
    real=time.time;clock={'offset':0.}
    monkeypatch.setattr(time,'time',lambda:real()+clock['offset'])
    monkeypatch.setattr(receipts,'utc_now',lambda:datetime.fromtimestamp(time.time(),timezone.utc).isoformat())
    capture=r.world.capture
    def current_frame():
        return replace(capture(),captured_at=datetime.fromtimestamp(time.time(),timezone.utc).isoformat())
    r.world.capture=r.c.capture=current_frame
    return clock


async def observe(r):
    r.c.wait_until=0
    return await r.c.process(r.world.capture())


def heal_outcomes(r, ident):
    return [x for x in r.c.hunt.outcomes if x['receipt_id']==ident]


def test_two_delayed_heals_settle_only_after_fresh_benefit(tmp_path,monkeypatch):
    async def run():
        r=SurvivalRig(tmp_path)
        try:
            clock=virtual_clock(r,monkeypatch)
            await r.start();r.enable_resources()
            for _ in range(2):
                r.health=.2
                await r.choose('heal_self')
                state=r.c.heal_pending;ident=state['receipt_id'];deadline=state['deadline']
                calls=len(r.sage.calls);inputs=len(r.backend.events)
                clock['offset']+=2.2
                await observe(r)
                assert r.c.heal_pending is state and not heal_outcomes(r,ident)
                assert not r.c.hunt.failures['recovery']
                assert len(r.sage.calls)==calls and len(r.backend.events)==inputs
                assert state['deadline']==deadline
                clock['offset']+=.6;r.health=.86
                await observe(r)
                assert r.c.heal_pending is None and r.c.hunt.pending is None
                assert [x['outcome'] for x in heal_outcomes(r,ident)]==['resources_improved']
                assert not r.c.hunt.failures['recovery'] and r.world.name=='Young Wolf'
            assert len([x for x in r.casts() if x[1]=='/cast [@player] Lesser Heal'])==2
            assert not r.c.hunt.credited_kills
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('missing',[False,True])
def test_two_deadline_nonprogress_windows_remain_bounded(tmp_path,monkeypatch,missing):
    async def run():
        r=SurvivalRig(tmp_path)
        try:
            clock=virtual_clock(r,monkeypatch)
            await r.start();r.enable_resources()
            original=grind_resources.hud_resources
            uncertain={'active':False}
            def bars(c,frame):
                value=original(c,frame)
                if uncertain['active']:value.update(player_health=None,health_confidence=0)
                return value
            monkeypatch.setattr(grind_resources,'hud_resources',bars)
            for count in (1,2):
                uncertain['active']=False
                await r.choose('heal_self');ident=r.c.heal_pending['receipt_id']
                uncertain['active']=missing
                clock['offset']+=3;await observe(r)
                assert r.c.heal_pending and not heal_outcomes(r,ident)
                clock['offset']+=3.1;await observe(r)
                # A post-deadline bar cannot establish an outcome inside the
                # original window, even if the current bar is readable.
                assert [x['outcome'] for x in heal_outcomes(r,ident)]==['unknown']
                assert r.c.hunt.failures['recovery']==count
            uncertain['active']=False
            await r.choose(None)
            assert r.c.hunt.phase=='recover' and 'heal_self' not in r.sage.calls[-1]['options']
            r.health=.88;await r.choose(None)
            context=r.sage.calls[-1]['prompt']
            assert 'prior non-improving or unassessed recovery outcomes' in context
            assert 'critical health is still' not in context and 'have no observed net health improvement' not in context
            assert 'recovered_resume' in r.sage.calls[-1]['options']
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['identity','source','generation','epoch','pending','receipt','config'])
def test_changed_heal_owner_cannot_take_later_resource_credit(tmp_path,monkeypatch,change):
    async def run():
        r=SurvivalRig(tmp_path)
        try:
            virtual_clock(r,monkeypatch)
            await r.start();r.enable_resources();await r.choose('heal_self')
            state=r.c.heal_pending;ident=state['receipt_id'];replacement=None
            if change=='identity':r.own_name='Other Player'
            elif change=='source':Path(state['pending_snapshot']['source_image']).write_bytes(b'changed-source')
            elif change=='generation':r.c.cycle._input_generation+=1
            elif change=='epoch':r.c.cycle.session_epoch='different-session'
            elif change=='config':r.c.config['heal_health_fraction']+=.01
            else:
                replacement=deepcopy(r.c.hunt.pending)
                replacement['receipt']['receipt_id']='different-receipt'
                if change=='pending':r.c.hunt.pending=replacement
                else:
                    r.c.hunt.pending.clear();r.c.hunt.pending.update(replacement)
                    replacement=r.c.hunt.pending
            r.health=.88
            await observe(r)
            assert r.c.heal_pending is None
            outcomes=heal_outcomes(r,ident)
            assert len(outcomes)==1 and outcomes[0]['outcome']=='unknown'
            assert not any(x['outcome']=='resources_improved' for x in outcomes)
            if replacement is not None:
                assert r.c.hunt.pending is replacement and not replacement['outcome_consumed']
            failures=dict(r.c.hunt.action_failures)
            grind_resources.finish_heal_observation(r.c,reason='duplicate_retirement_attempt')
            assert r.c.hunt.action_failures==failures and len(heal_outcomes(r,ident))==1
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('interruption',['pause','ui','death'])
def test_real_interruption_lifecycle_never_double_charges_heal(tmp_path,monkeypatch,interruption):
    async def run():
        r=SurvivalRig(tmp_path)
        try:
            clock=virtual_clock(r,monkeypatch)
            await r.start();r.enable_resources();await r.choose('heal_self')
            ident=r.c.heal_pending['receipt_id']
            if interruption=='pause':
                r.c.pause_focus()
                assert r.c.paused and r.c.heal_pending is None and r.c.hunt.pending is None
                assert len(heal_outcomes(r,ident))==1
                failures=dict(r.c.hunt.action_failures)
                r.c.pause_focus()
                assert r.c.hunt.action_failures==failures and len(heal_outcomes(r,ident))==1
            elif interruption=='death':
                r.ui='death';r.health=0
                await observe(r)
                assert r.c.stopped and r.c.reason=='own_death_modal_observed'
                assert not any(x['outcome']=='resources_improved' for x in heal_outcomes(r,ident))
            else:
                state=entry_world(r,opened=True)
                await r.choose('close_visible_ui')
                assert not state['open'] and r.c.hunt.pending['family']=='ui'
                assert len(heal_outcomes(r,ident))==1
                failures=dict(r.c.hunt.action_failures)
                await r.choose('world_normal_confirmed')
                r.health=.9;clock['offset']+=7
                await observe(r)
                assert r.c.heal_pending is None and len(heal_outcomes(r,ident))==1
                assert all(r.c.hunt.action_failures.get(k)==v for k,v in failures.items() if k.startswith('recovery'))
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('early_frame',[False,True])
@pytest.mark.parametrize('offset',[-.01,0.,.01])
def test_health_benefit_belongs_only_to_frames_within_original_horizon(tmp_path,monkeypatch,early_frame,offset):
    async def run():
        r=SurvivalRig(tmp_path)
        try:
            clock=virtual_clock(r,monkeypatch)
            await r.start();r.enable_resources();await r.choose('heal_self')
            ident=r.c.heal_pending['receipt_id'];deadline=r.c.heal_pending['deadline']
            if early_frame:
                clock['offset']+=2.2;await observe(r)
                assert r.c.heal_pending['deadline']==deadline and not heal_outcomes(r,ident)
            calls=len(r.sage.calls);keys=r.physical_keys();casts=r.casts()
            r.health=.88
            clock['offset']+=deadline+1-time.time()
            frame=replace(r.world.capture(),captured_at=datetime.fromtimestamp(deadline+offset,timezone.utc).isoformat())
            r.c.wait_until=0
            await r.c.process(frame)
            assert [x['outcome'] for x in heal_outcomes(r,ident)]==['unknown' if offset>0 else 'resources_improved']
            assert len(r.sage.calls)==calls and r.physical_keys()==keys and r.casts()==casts
            assert r.c.heal_pending is None and r.c.hunt.pending is None
            assert r.c.hunt.failures['recovery']==int(offset>0)
            await r.choose(None)
            assert r.c.current_hud['player_health']>.8 and r.c.hunt.failures['recovery']==int(offset>0)
            assert 'heal_self' not in r.sage.calls[-1]['options']
            assert 'critical health is still' not in r.sage.calls[-1]['prompt']
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation',['in_place','copied_alias','different_receipt','already_consumed'])
def test_retired_original_receipt_cannot_be_archived_and_charged_again(tmp_path,monkeypatch,mutation):
    async def run():
        r=SurvivalRig(tmp_path)
        try:
            virtual_clock(r,monkeypatch)
            await r.start();r.enable_resources();await r.choose('heal_self')
            original=r.c.hunt.pending;ident=original['receipt']['receipt_id']
            if mutation=='copied_alias':r.c.hunt.pending=deepcopy(original)
            elif mutation=='different_receipt':
                r.c.hunt.pending=deepcopy(original)
                r.c.hunt.pending['receipt']['receipt_id']='independent-replacement'
            elif mutation=='already_consumed':r.c.hunt.archive_pending('independent_prior_archival')
            alias=r.c.hunt.pending
            if alias:alias['measurement']['review_mutation']='changed metadata'
            r.health=.88;await observe(r)
            assert r.c.heal_pending is None
            assert [x['outcome'] for x in heal_outcomes(r,ident)]==['unknown']
            if mutation=='different_receipt':
                assert r.c.hunt.pending is alias and not alias['outcome_consumed']
                assert not any(x['receipt_id']=='independent-replacement' for x in r.c.hunt.outcomes)
                return
            assert r.c.hunt.pending is None
            if alias:assert alias['outcome_consumed']
            if mutation!='already_consumed':assert original['outcome_consumed']
            failures=dict(r.c.hunt.action_failures);family=r.c.hunt.failures['recovery']
            assert failures['recovery:0:heal']==1
            calls=len(r.sage.calls);keys=r.physical_keys();casts=r.casts()
            r.c.pause_focus()
            assert len(heal_outcomes(r,ident))==1 and r.c.hunt.action_failures==failures
            assert r.c.hunt.failures['recovery']==family
            assert len(r.sage.calls)==calls and r.physical_keys()==keys and r.casts()==casts
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('cancel',['rejected','timeout','submitted'])
def test_probe_credit_and_physical_constraint_have_separate_ownership(tmp_path,cancel):
    async def run():
        r=Rig(tmp_path)
        try:
            await start(r);hud,_=install_hud(r);await facing_failure(r)
            hud['target_health']=.4;await r.choose('reassess_cast_ready')
            h=r.c.hunt;before=len(smites(r));debt=deepcopy(h.action_failures)
            async def guard(binding,index):
                if cancel=='timeout':raise asyncio.TimeoutError()
                return {'continue':cancel=='submitted','reason':'fresh_facing_error' if cancel=='rejected' else 'current_clear'}
            async def post(binding,index,phase):return {'continue':True}
            async def no_wait(seconds):pass
            r.executor.batch_guard=guard;r.executor.batch_post_guard=post;r.executor._batch_sleep=no_wait
            r.c.config['cast_wait_seconds']=1.5
            result=await r.choose('attack_mob_level_1')
            assert h.cast_error['reassessment']['status']=='consumed'
            assert h.action_failures==debt
            if cancel=='submitted':
                assert completed_smite_submission(result.receipt)
                assert h.cast_error['status']=='superseded_by_guarded_probe' and len(smites(r))==before+1
            else:
                assert not result.receipt['possible_input'] and len(smites(r))==before
                assert h.cast_error['status']=='active' and h.cast_obligation
                for _ in range(2):
                    await r.choose(None)
                    options=r.sage.calls[-1]['options']
                    assert {'turn_left','turn_right'}<=options.keys()
                    assert not {'reassess_cast_ready','attack_mob_level_1'} & options.keys()
            assert not h.credited_kills and not h.own_damage_evidence
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation',['start_attack_only','wrong_command','incomplete_step','wrong_step_owner',
    'wrong_generation','replacement_error'])
def test_probe_retirement_requires_original_owner_and_complete_smite_trace(tmp_path,mutation):
    async def run():
        r=Rig(tmp_path)
        try:
            await start(r);hud,_=install_hud(r);await facing_failure(r)
            hud['target_health']=.4;await r.choose('reassess_cast_ready')
            original=deepcopy(r.c.hunt.cast_error)
            # Produce a genuine completed fake-native receipt through the current controller.
            await r.choose('attack_mob_level_1')
            receipt=deepcopy(r.c.cycle.last_receipt)
            assert completed_smite_submission(receipt)
            if mutation=='replacement_error':
                replacement=deepcopy(r.c.hunt.cast_error)
                replacement['cast_receipt_id']='replacement-error'
                r.c.hunt.cast_error=replacement
                before=deepcopy(replacement)
                frame=r.world.capture()
                assert not consume_probe(r.c,original['reassessment'],receipt,frame,original)
                assert r.c.hunt.cast_error==before
            else:
                if mutation=='start_attack_only':
                    receipt['execution']['completed_steps']=[]
                elif mutation=='wrong_command':receipt['execution']['command']='/startattack [harm,nodead]'
                elif mutation=='incomplete_step':receipt['input_steps'][-1]['status']='attempted'
                elif mutation=='wrong_step_owner':receipt['input_steps'][0]['execution_id']='other-receipt'
                elif mutation=='wrong_generation':receipt['input_steps'][-1]['input_generation']+=1
                assert not completed_smite_submission(receipt)
        finally:await r.close()
    asyncio.run(run())
