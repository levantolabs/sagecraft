"""Selected-unit approach/timing contract using small synthetic offline rigs."""
from test_grind_product_spec import retain_legacy_no_effect
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import time

import pytest
from sage_wow.agent import cycle
from sage_wow.agent.cycle import ActionCandidate, TravelDecisionBudget
from sage_wow.agent.grind_search import HuntState
from test_grind_product_spec import Rig,scenario
from test_grind_travel_recovery import TravelClock,events


@pytest.mark.parametrize('name',['Generic Creature A','Other Creature B'])
def test_search_failure_does_not_ban_selected_approach_or_establish_fight(tmp_path,name):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.world.name='';r.c.config['move_seconds']=.05
            await r.choose('forward');r.c.pause_focus();r.c.resume_focus();await r.choose('world_normal_confirmed')
            await r.choose('forward');await r.choose('motion_no_useful_effect')
            await r.choose('target_enemy');r.world.name=name
            await r.choose('forward')
            call=r.sage.calls[-1]
            assert call['instructions'].startswith('Is an actual selected target portrait/bar frame present,')
            assert r.c.hunt.phase=='approach' and r.c.hunt.encounter==0 and r.c.hunt.encounter_ended
            assert not r.casts() and not r.c.hunt.learned and not r.c.hunt.progress_facts
            assert r.c.hunt.motion_count('forward','search')==2
            assert r.c.hunt.pending['purpose']=='approach'
            assert {'forward','turn_left','turn_right','attack_mob_level_1','no_selected_frame','reject_selected_target','cannot_assess','ui_blocked','recover_now'}==call['options'].keys()
        finally:await r.close()
    asyncio.run(run())


@scenario
async def test_outcome_null_cannot_overwrite_receipt_and_resolves_once(r):
    r.c.config['move_seconds']=.05
    await r.choose('forward');pending=r.c.hunt.pending;receipt=pending['receipt']['receipt_id'];inputs=len(r.physical_keys())
    await r.choose(None)
    assert r.c.hunt.pending is pending and len(r.physical_keys())==inputs
    assert r.c.hunt.unclear==1
    assert r.sage.calls[-1]['instructions']=='Comparing BEFORE with CURRENT, did that movement bring us closer to Young Wolf, face it better, or improve the approach?'
    assert all(item.binding['type']=='observe_only' for item in r.sage.calls[-1]['options'].values())
    await r.choose('motion_useful')
    assert r.c.hunt.pending is None and r.c.hunt.unclear==0
    assert len([outcome for outcome in r.c.hunt.outcomes if outcome['receipt_id']==receipt])==1
    assert r.c.hunt.sector==0 and not r.c.hunt.progress_facts
    await r.choose('attack_mob_level_1')
    assert r.c.hunt.encounter==1 and len(r.casts())==1
    assert not r.c.hunt.progress_facts


@scenario
async def test_two_measured_failures_withhold_unchanged_method_across_ocr_and_reselection(r):
    r.c.config['move_seconds']=.05
    for _ in range(2):await r.choose('forward');await r.choose('motion_no_useful_effect')
    identity=r.c.hunt.approach['history_key'];debt=dict(r.c.hunt.action_failures)
    await r.choose('reject_selected_target');r.world.name='';await r.choose('target_cleared')
    await r.choose('target_enemy');r.world.name='  Young Wolf!  ';r.world.target_level=None
    await r.choose('cannot_assess')
    assert r.c.hunt.approach['history_key']==identity
    assert 'forward' not in r.sage.calls[-1]['options']
    assert all(r.c.hunt.action_failures.get(k,0)>=v for k,v in debt.items())
    r.controls['strafe_left']={'keycode':20,'verified_from':'offline fixture'}
    await r.choose('turn_left')
    assert {'turn_left','turn_right'}<=r.sage.calls[-1]['options'].keys()
    assert not any(option.startswith('attack_mob') for option in r.sage.calls[-1]['options'])


@pytest.mark.parametrize('interruption',['focus','safety'])
def test_interrupted_moves_share_two_attempt_bound_without_measured_failures(tmp_path,interruption):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.c.config['move_seconds']=.05
            for _ in range(2):
                await r.choose('forward')
                if interruption=='focus':
                    r.c.pause_focus();r.c.resume_focus();await r.choose('world_normal_confirmed')
                else:
                    await r.choose('recover_now');await r.choose('recovered_resume')
            await r.choose('cannot_assess')
            assert 'forward' not in r.sage.calls[-1]['options']
            identity=r.c.hunt.approach['history_key'];debt=r.c.hunt.motion_key('forward','approach',identity)
            assert r.c.hunt.unresolved_motion[debt]==2 and r.c.hunt.action_failures.get(debt,0)==0
            assert r.c.hunt.unclear==1 and all(o['outcome']=='unknown' for o in r.c.hunt.outcomes if o['purpose']=='approach')
            assert 'hunt_plan' not in r.sage.calls[-1]['prompt']
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('error',['Target out of range','You are not facing target','Target not in line of sight'])
def test_fresh_cast_error_owns_question_and_one_linked_correction_retry(tmp_path,error):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();r.c.config['turn_seconds']=.05
            await r.choose('attack_mob_level_1');assert not r.c.hunt.progress_facts
            r.world.error=error
            await r.choose('position_error')
            assert not any(name.startswith('attack_mob') for name in r.sage.calls[-1]['options'])
            assert r.c.hunt.phase=='approach' and r.c.hunt.cast_obligation
            r.world.error=''
            await r.choose('cannot_assess')
            assert not any(name.startswith('attack_mob') for name in r.sage.calls[-1]['options']) and not r.c.hunt.retry_credit
            await r.choose('turn_left');assert r.c.hunt.pending['purpose']=='cast_correction'
            await r.choose('motion_useful');assert r.c.hunt.retry_credit
            correction=r.c.hunt.correction_evidence
            await r.choose('attack_mob_level_1')
            assert len(r.casts())==2 and not r.c.hunt.retry_credit and correction['granted']
            assert not r.c.hunt.progress_facts
            assert 'We have not cast yet' not in r.sage.calls[-1]['instructions']
        finally:await r.close()
    asyncio.run(run())


@scenario
async def test_stage_toggle_and_fallback_preserve_unresolved_question_and_core_choices(r):
    await r.choose(None);await r.choose('cannot_assess')
    assert r.c.hunt.unclear==2
    assert {'forward','turn_left','turn_right','reject_selected_target','cannot_assess','recover_now','ui_blocked'}<=r.sage.calls[-1]['options'].keys()
    assert 'dead_or_unrecoverable' not in r.sage.calls[-1]['options']
    await r.choose(None)
    assert not r.c.hunt.blocked and r.c.hunt.recovery_requested
    assert {'backward','reinspect_selected_frame','change_search_strategy'}<=r.sage.calls[-1]['options'].keys()
    assert sum(r.c.hunt.question_debt.values())==3
    assert not r.physical_keys()


@scenario
async def test_two_different_resolved_questions_do_not_accumulate_semantic_debt(r):
    await r.choose(None);await r.choose('forward')
    assert r.c.hunt.unclear==0
    await r.choose(None);await r.choose('motion_no_useful_effect')
    assert r.c.hunt.unclear==0 and not r.c.hunt.blocked
    await r.choose(None)
    assert r.c.hunt.unclear==1 and r.c.hunt.motion_count('forward','approach',r.c.hunt.approach['history_key'])==1


@pytest.mark.parametrize('kind',['hold','cast','sequence'])
def test_complete_binding_must_fit_deadline_before_any_input(tmp_path,monkeypatch,kind):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();clock=TravelClock(monkeypatch,r);source=r.world.capture();started=datetime.fromisoformat(source.captured_at).timestamp()
            binding={'type':'keypress','keycode':1,'hold_seconds':1} if kind=='hold' else {'type':'cast_guarded','spell':'Smite'} if kind=='cast' else {'type':'keypress_sequence','keycodes':[1,2]}
            duration=1 if kind=='hold' else .3 if kind=='cast' else .26
            async def late_guard(_):
                clock.offset=12-duration+.05
                return cycle.DispatchValidation(True,r.world.capture(),'synthetic fresh guard')
            candidate=ActionCandidate('test',kind,binding,dispatch_guard=late_guard)
            budget=TravelDecisionBudget(started,started+12,question_kind='approach')
            r.sage.answers.append('test')
            with r.c.cycle.scoped_execution_scope(task_id=r.c.task_id,objective_revision=r.c.revision,session_epoch=r.c.cycle.session_epoch,
                    deadline_epoch=budget.deadline,input_generation=r.c.cycle.input_generation,is_current=r.c.current,max_frame_age_seconds=12):
                result=await r.c.cycle.decide_and_execute(source,source.image_path,'test','test',[candidate,ActionCandidate('hold','No input',{'type':'observe_only'})],travel_budget=budget,max_frame_age_seconds=12)
            assert result.status=='grind_timing_expired' and not r.physical_keys() and not r.casts()
            assert events(r,'grind_timing_expired')[-1]['required_seconds']==pytest.approx(duration)
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('phase',['old_source','ocr','no_reserve','late_answer','guard_ocr','session'])
def test_approach_deadline_classified_as_timing_without_semantic_or_motion_debt(tmp_path,monkeypatch,phase):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();clock=TravelClock(monkeypatch,r);source=r.world.capture();r.sage.answers.append('attack_mob_level_1')
            before_calls=len(r.sage.calls)
            if phase=='old_source':source=replace(source,captured_at=(datetime.now(timezone.utc)-timedelta(seconds=13)).isoformat())
            elif phase=='session':r.c.deadline=time.time()+.2
            elif phase in {'ocr','no_reserve'}:
                ocr=r.c.ocr
                def slow(path):
                    rows=ocr(path);clock.offset=13 if phase=='ocr' else 9.2;return rows
                r.c.ocr=slow
            elif phase=='late_answer':
                async def late():clock.offset=9.5
                r.sage.hook=late
            elif phase=='guard_ocr':
                ocr=r.c.ocr
                def slow_dispatch(path):
                    rows=ocr(path)
                    if len(r.sage.calls)>before_calls:clock.offset=13
                    return rows
                r.c.ocr=slow_dispatch
            result=await r.c.process(source)
            assert result.status=='grind_timing_expired',(phase,result.status,result.detail)
            assert r.c.timing_failures==1 and r.c.null_answers==0 and r.c.hunt.unclear==0
            assert not r.c.hunt.action_failures and not r.c.provider_failures and not r.casts()
            if phase in {'old_source','ocr','no_reserve','session'}:assert len(r.sage.calls)==before_calls
        finally:await r.close()
    asyncio.run(run())


@pytest.mark.parametrize('reasoning',['off','auto'])
def test_answer_with_older_than_ten_source_uses_one_budget_and_fresh_guard(tmp_path,monkeypatch,reasoning):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();clock=TravelClock(monkeypatch,r)
            if reasoning=='auto':await r.choose(None)
            provider=r.sage.decide_image_choice;seen=[]
            async def request(*args,**kwargs):
                seen.append(args[-1]);result=await provider(*args,**kwargs);clock.offset=8;return result
            r.sage.decide_image_choice=request
            capture=r.c.capture
            def guard_capture():clock.offset=10.5;return capture()
            r.c.capture=guard_capture
            r.sage.answers.append('attack_mob_level_1')
            result=await r.c.process(r.world.capture())
            assert result.status=='dispatched' and len(r.casts())==1 and seen==[reasoning]
            assert r.c.timing_failures==0 and r.c.hunt.unclear==0
            assert events(r,'travel_provider_budget')[-1]['provider_allowance_seconds']<=9
        finally:await r.close()
    asyncio.run(run())


@scenario
async def test_timing_backoff_is_separate_and_valid_answer_resets_it(r):
    for count,delay in enumerate((1,2,4,15),1):
        source=replace(r.world.capture(),captured_at=(datetime.now(timezone.utc)-timedelta(seconds=13)).isoformat())
        before=time.time();result=await r.c.process(source)
        assert result.status=='grind_timing_expired' and r.c.timing_failures==count
        assert delay-.1<=r.c.wait_until-before<=delay+.2
    assert r.c.hunt.blocked['reason']=='blocked_timing' and r.c.hunt.unclear==0 and r.c.provider_failures==0
    assert not r.sage.calls[2:] and not r.casts()


def test_blocked_checks_at_fifteen_and_fortyfive_cannot_postpone_sixty_due(tmp_path,monkeypatch):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();await r.choose('cannot_assess');clock=TravelClock(monkeypatch,r)
            # Capability/UI loading blocks retain bounded observation cadence;
            # ordinary ambiguity now uses actionable recovery instead.
            r.c.enter_blocked('blocked_loading',r.c.last_signature)
            episode=r.c.hunt.blocked;due=episode['assessment_due_at'];start=episode['entered_at'];calls=len(r.sage.calls)
            for elapsed in (15.1,45.1):
                clock.offset=elapsed
                r.c.wait_until=0
                result=await r.c.process(r.world.capture())
                assert result.status=='grind_blocked' and episode['assessment_due_at']==due
                assert episode['next_observation_at']<=due and len(r.sage.calls)==calls
            r.c.enter_blocked(episode['reason'],episode['evidence_signature']);assert episode['assessment_due_at']==due
            clock.offset=60.1
            await r.choose('blocked_still_unresolved')
            assert len(r.sage.calls)==calls+1 and episode['last_assessment_at']>=start+60
            assert episode['assessment_due_at']==pytest.approx(episode['last_assessment_at']+60)
            assert not r.physical_keys()
        finally:await r.close()
    asyncio.run(run())


@scenario
async def test_own_player_and_conflicting_or_out_of_band_ocr_never_offer_cast(r):
    r.world.name='Test Player';await r.choose('reject_selected_target')
    assert not any(option.startswith('attack_mob') for option in r.sage.calls[-1]['options'])
    r.world.name='';await r.choose('target_cleared');await r.choose('target_enemy')
    r.world.name='Generic Creature';r.world.target_level=4
    await r.choose('reject_selected_target')
    assert not any(option.startswith('attack_mob') for option in r.sage.calls[-1]['options']) and not r.casts()


@scenario
async def test_same_name_reselection_preserves_cast_obligation_and_first_attempt_history(r):
    await r.choose('attack_mob_level_1');r.world.error='Target out of range';await r.choose('position_error')
    await r.choose('reject_selected_target');r.world.name='';r.world.error='';await r.choose('target_cleared')
    await r.choose('target_enemy');r.world.name='Young Wolf';r.world.target_level=None
    await r.choose('cannot_assess')
    assert not any(option.startswith('attack_mob') for option in r.sage.calls[-1]['options'])
    assert 'We have not cast yet' not in r.sage.calls[-1]['instructions']
    history=r.c.hunt.target_history[r.c.hunt.approach['history_key']]
    assert history['cast_attempted'] and history['cast_obligation'] and len(r.casts())==1


@scenario
async def test_transient_range_cue_then_null_and_disappearance_does_not_reopen_attack(r):
    await r.choose('attack_mob_level_1');r.world.error='Target out of range'
    await r.choose(None)
    evidence=r.c.hunt.pending['position_error_evidence']
    r.world.error=''
    assert evidence is r.c.hunt.pending['position_error_evidence']
    assert not any(name.startswith('attack_mob') for name in r.sage.calls[-1]['options'])
    assert r.c.hunt.unclear==1 and len(r.casts())==1
    await r.choose('position_error')
    assert r.c.hunt.cast_obligation and r.c.hunt.phase=='approach'


@scenario
async def test_confirmed_changed_vantage_opens_new_method_without_erasing_failures(r):
    r.c.config.update(move_seconds=.05,turn_seconds=.05)
    for _ in range(2):await r.choose('forward');await r.choose('motion_no_useful_effect')
    old=dict(r.c.hunt.action_failures);revision=r.c.hunt.target_history[r.c.hunt.approach['history_key']]['method_revision']
    await r.choose('turn_left');assert 'forward' not in r.sage.calls[-1]['options']
    await r.choose('motion_useful');await r.choose('forward')
    assert r.c.hunt.target_history[r.c.hunt.approach['history_key']]['method_revision']==revision+1
    assert all(r.c.hunt.action_failures.get(k,0)>=v for k,v in old.items())


@scenario
async def test_a_b_a_keeps_cast_debt_and_returned_a_correction_grants_only_a_retry(r):
    r.c.config['turn_seconds']=.05
    await r.choose('attack_mob_level_1');r.world.error='Target out of range';await r.choose('position_error')
    a=r.c.hunt.approach['history_key'];a_encounter=r.c.hunt.encounter
    r.c.hunt.progress_before='a-progress';r.c.hunt.death_frame='a-death';r.c.hunt.xp_observed='a-xp';r.c.hunt.encounter_area='a-area'
    r.c.hunt.remember_approach()
    r.world.error='';r.world.name='Other Creature'
    # Prior error was already resolved; the selected distinct target owns a
    # fresh approach, independent of A's retained correction obligation.
    await r.choose('attack_mob_level_1');b=r.c.hunt.approach['history_key']
    r.world.error='Target not in line of sight';await r.choose('position_error')
    b_encounter=r.c.hunt.encounter
    r.c.hunt.progress_before='b-progress';r.c.hunt.death_frame='b-death';r.c.hunt.xp_observed='b-xp';r.c.hunt.encounter_area='b-area'
    r.c.hunt.remember_approach()
    b_debt=dict(r.c.hunt.target_history[b]);r.world.error='';r.world.name='Young Wolf'
    await r.choose('cannot_assess')
    assert r.c.hunt.approach['history_key']==a and r.c.hunt.encounter==a_encounter
    assert (r.c.hunt.progress_before,r.c.hunt.death_frame,r.c.hunt.xp_observed,r.c.hunt.encounter_area)==('a-progress','a-death','a-xp','a-area')
    assert r.c.hunt.cast_error is not r.c.hunt.target_history[a]['cast_error']
    assert r.c.hunt.target_history[a]['cast_error'] is not r.c.hunt.target_history[b]['cast_error']
    assert not any(name.startswith('attack_mob') for name in r.sage.calls[-1]['options'])
    await r.choose('turn_left');await r.choose('motion_useful');await r.choose('attack_mob_level_1')
    assert len(r.casts())==3 and not r.c.hunt.retry_credit
    assert r.c.hunt.target_history[b]['cast_obligation']==b_debt['cast_obligation']
    assert r.c.hunt.target_history[b]['combat_failures']==b_debt['combat_failures']
    assert r.c.hunt.target_history[b]['cast_error']['status']=='active'
    assert r.c.hunt.target_history[a]['cast_error']['status']=='retired_by_linked_correction'
    assert r.c.hunt.encounter==a_encounter and r.c.hunt.correction_rounds==0
    await retain_legacy_no_effect(r);r.world.name='Third Creature'
    await r.choose('attack_mob_level_1')
    assert len({a_encounter,b_encounter,r.c.hunt.encounter})==3 and r.c.hunt.encounter>b_encounter


def test_completed_lifetimes_compact_but_unresolved_history_capacity_cannot_bypass_approach(tmp_path):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start()
            # Completed lifetimes are old observed-dead units, not unresolved
            # same-name tasks. Compaction must not impose a 24-kill ceiling.
            for number in range(30):
                target={'name':'Generic Creature','levels':[1]}
                record=r.c.hunt.approach_for(target)
                assert record is not None
                record['completed']=True;r.c.hunt.last_target='generic creature';r.c.hunt.target_dead_observed=True
            assert len(r.c.hunt.target_history)<=24
            r.c.hunt.target_history={};r.c.hunt.target_dead_observed=False
            for number in range(24):assert r.c.hunt.approach_for({'name':f'Unresolved Unit {number}','levels':[1]})
            r.world.name='Untracked Unit';await r.choose('cannot_assess')
            assert not any(option.startswith('attack_mob') or option=='forward' for option in r.sage.calls[-1]['options'])
            assert not r.casts() and not r.physical_keys()
        finally:await r.close()
    asyncio.run(run())


@scenario
async def test_unresolved_question_capacity_does_not_alias_or_clear_unrelated_debt(r):
    r.c.hunt.question_debt={f'question-{number}':1 for number in range(64)}
    before=dict(r.c.hunt.question_debt)
    debt=r.c.hunt.motion_key('forward','search');r.c.hunt.action_failures[debt]=2
    await r.choose('cannot_assess')
    assert len(r.c.hunt.question_debt)==64 and r.c.hunt.archived_question_count==1
    archived=r.c.hunt.question_history[-1]
    assert before[archived['question_id']]==archived['unclear_answers']==1
    assert r.c.hunt.action_failures[debt]==2 and not r.physical_keys() and not r.c.stopped


@scenario
async def test_resolved_fight_to_distinct_target_uses_focused_movement_guard(r):
    r.c.config['move_seconds']=.05
    await r.choose('attack_mob_level_1');await retain_legacy_no_effect(r)
    assert r.c.hunt.pending is None and r.c.hunt.phase=='fight'
    r.world.name='Distinct Unit';await r.choose('forward')
    assert r.c.hunt.phase=='approach' and r.c.hunt.pending['purpose']=='approach'
    assert r.c.hunt.target_history[r.c.hunt.approach['history_key']]['cast_attempted'] is False


@pytest.mark.parametrize('due',[False,True])
def test_selected_new_target_pre_routing_ocr_inherits_source_budget_and_due_level_precedence(tmp_path,monkeypatch,due):
    async def run(due):
        directory=tmp_path/str(due);directory.mkdir();r=Rig(directory)
        try:
            await r.start();r.c.config['move_seconds']=.05
            await r.choose('attack_mob_level_1');await retain_legacy_no_effect(r)
            clock=TravelClock(monkeypatch,r);r.world.name='Distinct Unit'
            if due:
                r.c.level.last_attempt_at=0
                await r.choose('player_level_1')
                assert r.c.hunt.phase=='search' and len(r.casts())==1
                assert not r.c.hunt.pending
            ocr=r.c.ocr
            def routed_ocr(path):
                rows=ocr(path);clock.offset=max(clock.offset,6);return rows
            r.c.ocr=routed_ocr
            capture=r.c.capture
            def fresh_guard():clock.offset=10.5;return capture()
            r.c.capture=fresh_guard
            provider=r.sage.decide_image_choice
            async def late_answer(*args,**kwargs):
                result=await provider(*args,**kwargs);clock.offset=8;return result
            r.sage.decide_image_choice=late_answer
            source=r.world.capture();original=datetime.fromisoformat(source.captured_at).timestamp()
            r.sage.answers.append('forward');result=await r.c.process(source)
            assert result.status=='dispatched' and r.c.hunt.pending['purpose']=='approach'
            budget_event=events(r,'travel_provider_budget')[-1]
            assert budget_event['source_at']==original and budget_event['deadline']==pytest.approx(original+12)
            assert budget_event['provider_allowance_seconds']<3.1
        finally:await r.close()
    asyncio.run(run(due))


def test_question_capacity_preserves_history_and_immediate_safety_exit(tmp_path,monkeypatch):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start()
            r.c.hunt.question_debt={f'question-{number}':1 for number in range(64)}
            r.world.player_health='black'  # Death claim must not contradict a living bar.
            await r.choose('dead_or_unrecoverable')
            assert r.c.stopped and not r.physical_keys()
            assert len(r.c.hunt.question_debt)+r.c.hunt.archived_question_count==64
            assert r.c.hunt.archived_question_unclear==1
        finally:await r.close()
    asyncio.run(run())


def test_validation_capture_age_is_timing_even_before_original_deadline(tmp_path,monkeypatch):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();clock=TravelClock(monkeypatch,r);ocr=r.c.ocr
            calls=len(r.sage.calls)
            def slow_guard_ocr(path):
                rows=ocr(path)
                if len(r.sage.calls)>calls:clock.offset=1.8
                return rows
            r.c.ocr=slow_guard_ocr;r.sage.answers.append('forward')
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_timing_expired' and r.c.timing_failures==1
            reason=events(r,'grind_timing_expired')[-1]
            assert reason['phase']=='dispatch_capture_age' and reason['remaining_seconds']>9
            assert not r.physical_keys() and r.c.hunt.unclear==0 and not r.c.hunt.action_failures
        finally:await r.close()
    asyncio.run(run())


def test_completed_a_then_b_then_living_a_starts_new_lifetime_but_unknown_a_does_not():
    hunt=HuntState({},'offline')
    a=hunt.approach_for({'name':'Creature A','levels':[1]});a['completed']=True
    hunt.last_target='creature a';hunt.target_dead_observed=True
    hunt.approach_for({'name':'Creature B','levels':[1]})
    hunt.last_target='creature b';hunt.target_dead_observed=False
    second_a=hunt.approach_for({'name':'Creature A!','levels':[]})
    assert second_a['key']!=a['key'] and second_a['new_life'] and not second_a['cast_attempted']
    second_a['rejections']=2
    hunt.approach_for({'name':'Creature B','levels':[]})
    returned=hunt.approach_for({'name':'Creature A','levels':[1]})
    assert returned is second_a and returned['rejections']==2


def test_changed_method_history_compacts_without_erasing_earlier_failure_accounting():
    hunt=HuntState({},'offline');history=hunt.approach_for({'name':'Creature A','levels':[1]})
    for revision in range(100):
        history['method_revision']=revision
        hunt.action_failures[hunt.motion_key('forward','approach',history['key'])]=2
        hunt.unresolved_motion[hunt.motion_key('turn_left','approach',history['key'])]=1
        hunt.retire_motion_debt(history['key'],before_revision=revision-2)
    assert len(hunt.action_failures)<=3 and len(hunt.unresolved_motion)<=3
    assert sum(history['prior_method_debt'].values())+sum(hunt.action_failures.values())+sum(hunt.unresolved_motion.values())==300


def test_late_answer_preserves_prior_unresolved_semantic_question(tmp_path,monkeypatch):
    async def run():
        r=Rig(tmp_path)
        try:
            await r.start();await r.choose(None)
            identity=r.c.hunt.active_question;before=dict(r.c.hunt.question_debt)
            clock=TravelClock(monkeypatch,r)
            async def late():clock.offset=9.5
            r.sage.hook=late;r.sage.answers.append('forward')
            result=await r.c.process(r.world.capture())
            assert result.status=='grind_timing_expired' and r.c.hunt.active_question==identity
            assert r.c.hunt.question_debt==before and r.c.hunt.unclear==1
            assert not r.physical_keys() and r.c.hunt.pending is None
        finally:await r.close()
    asyncio.run(run())


@scenario
async def test_actual_selected_telemetry_change_is_not_timing_or_semantic_failure(r):
    await r.choose(None);before=dict(r.c.hunt.question_debt)
    async def change_selection():r.world.name='Changed Target'
    r.sage.hook=change_selection
    result=await r.choose('forward')
    assert result.status=='dispatch_guard_rejected' and r.c.timing_failures==0
    assert r.c.hunt.question_debt==before and r.c.hunt.unclear==1
    assert not r.physical_keys() and not r.c.hunt.action_failures


@scenario
async def test_first_cast_lost_to_b_then_returning_a_requires_linked_correction(r):
    r.c.config['turn_seconds']=.05
    await r.choose('attack_mob_level_1');a=r.c.hunt.approach['history_key']
    assert r.c.hunt.pending['target_history_key']==a
    r.world.name='Creature B';await r.choose('lost');await r.choose('target_enemy')
    assert r.c.hunt.target_history[a]['cast_obligation'] and not any(name.startswith('attack_mob') for name in r.sage.calls[-1]['options'])
    await r.choose('attack_mob_level_1');await retain_legacy_no_effect(r)
    b=r.c.hunt.approach['history_key'];b_obligation=r.c.hunt.target_history[b]['cast_obligation']
    r.world.name='Young Wolf';await r.choose('cannot_assess')
    assert r.c.hunt.approach['history_key']==a and not any(name.startswith('attack_mob') for name in r.sage.calls[-1]['options'])
    await r.choose('turn_left');await r.choose('motion_useful');await r.choose('attack_mob_level_1')
    assert len(r.casts())==3 and r.c.hunt.target_history[b]['cast_obligation']==b_obligation
