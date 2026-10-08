"""Simulated Sage/menu traces for committed combat; never gameplay proof."""

from sage_wow.agent.grind_search import HuntState
from test_grind_product_spec import scenario as legacy_scenario


def scenario(test):
    async def committed(rig):
        rig.c.config['committed_combat']=True
        rig.controls.update({name:{'keycode':code,'verified_from':'offline operator'}
            for name,code in (('strafe_left',100),('strafe_right',101))})
        original_casts=rig.casts
        rig.casts=lambda:[event for event in original_casts() if event[1].startswith('/cast ') and 'Smite' in event[1]]
        await test(rig)
    return legacy_scenario(committed)


def confirmed_target(target):
    return {**target,'visual_observation':{'selected_hud':'present','name':target['name'],
        'level':1,'target_kind':'creature','life_state':'alive'},'eligibility':'eligible'}


def add_current_observer(rig):
    original=rig.c.target_proposal
    async def proposal(frame):
        return confirmed_target(await original(frame))
    rig.c.target_proposal=proposal


@scenario
async def test_sage_burst_uses_configured_timing_and_no_idle_positioning(r):
    r.c.config.update(smite_burst_count=3,cast_wait_seconds=2.2)
    checks=[];sleeps=[]
    async def guard(binding,index):
        checks.append(index)
        return {'continue':True,'frame_id':f'fresh-{index}'}
    async def sleep(seconds):sleeps.append(seconds)
    r.executor.batch_guard=guard;r.executor._batch_sleep=sleep
    await r.choose('attack_mob_level_1')
    options=r.sage.calls[-1]['options']
    assert not {'forward','turn_left','turn_right','backward'} & options.keys()
    assert options['attack_mob_level_1'].binding=={'type':'cast_burst','spell':'Smite',
        'count':3,'interval_seconds':2.2,'expected_target_name':'Young Wolf','start_attack':True}
    assert checks==[0,1,2] and sleeps==[2.2,2.2]
    assert len(r.casts())==3 and not r.c.stopped and not r.c.hunt.credited_kills


@scenario
async def test_fresh_living_target_recovers_unknown_damage_without_wandering(r):
    await r.choose('attack_mob_level_1')
    old=r.c.hunt.pending['receipt']['receipt_id']
    await r.choose(None);await r.choose(None)
    assert r.c.hunt.cast_obligation and r.c.hunt.pending is None
    add_current_observer(r)
    await r.choose('attack_mob_level_1')
    assert len(r.casts())==2 and not r.c.stopped and not r.c.hunt.cast_obligation
    assert not {'forward','turn_left','turn_right','no_selected_frame'} & r.sage.calls[-1]['options'].keys()
    assert any(x['outcome']=='unknown' and x['receipt_id']==old for x in r.c.hunt.outcomes)
    assert not r.c.hunt.credited_kills and r.c.hunt.failures['combat']==0


@scenario
async def test_fresh_alive_feedback_can_repeat_without_inventing_damage(r):
    await r.choose('attack_mob_level_1')
    old=r.c.hunt.pending['receipt']['receipt_id']
    add_current_observer(r)
    await r.choose('attack_mob_level_1')
    assert len(r.casts())==2 and not r.c.hunt.cast_obligation
    assert any(x['outcome']=='unknown' and x['receipt_id']==old for x in r.c.hunt.outcomes)
    assert not any(x['kind']=='damage_observation' for x in r.c.hunt.progress_facts)
    assert not r.c.hunt.credited_kills


@scenario
async def test_confirmed_range_error_leaves_bearing_and_approach_to_sage(r):
    await r.choose('attack_mob_level_1')
    r.world.error='Out of range'
    await r.choose('position_error')
    await r.choose('forward')
    options=r.sage.calls[-1]['options']
    assert {'forward','turn_left','turn_right','backward','strafe_left','strafe_right'} <= options.keys()
    assert 'attack_mob_level_1' not in options
    assert r.c.hunt.cast_obligation and not r.c.hunt.retry_credit


@scenario
async def test_failed_forward_approach_can_turn_without_erasing_debt_or_granting_a_cast(r):
    await r.choose('attack_mob_level_1')
    r.world.error='Out of range'
    await r.choose('position_error')
    owner=r.c.hunt.approach['history_key']
    debt=r.c.hunt.motion_key('forward','cast_correction',owner)
    for _ in range(2):
        await r.choose('forward')
        await r.choose('motion_no_useful_effect')
    await r.choose('turn_right')
    options=r.sage.calls[-1]['options']
    assert 'forward' not in options and 'attack_mob_level_1' not in options
    assert {'turn_left','turn_right','backward','strafe_left','strafe_right'} <= options.keys()
    assert r.c.hunt.action_failures[debt]==2
    assert r.c.hunt.cast_obligation and not r.c.hunt.retry_credit
    assert r.c.hunt.pending['purpose']=='cast_correction'
    assert len(r.casts())==1 and not r.c.hunt.credited_kills
    await r.choose('motion_useful')
    r.world.error=''
    await r.choose('attack_mob_level_1')
    assert len(r.casts())==2 and r.c.hunt.action_failures[debt]==2
    assert not r.c.hunt.credited_kills


@scenario
async def test_unresolved_alternate_approach_does_not_become_cast_permission(r):
    await r.choose('attack_mob_level_1')
    r.world.error='Out of range'
    await r.choose('position_error')
    await r.choose('strafe_left')
    await r.choose('motion_no_useful_effect')
    await r.choose(None)
    assert 'attack_mob_level_1' not in r.sage.calls[-1]['options']
    assert r.c.hunt.cast_obligation and not r.c.hunt.retry_credit
    assert len(r.casts())==1 and not r.c.hunt.credited_kills


@scenario
async def test_observed_death_requests_loot_without_assuming_credit(r):
    r.c.config['loot_enabled']=True
    await r.choose('attack_mob_level_1')
    old=r.c.hunt.pending['receipt']['receipt_id']
    r.world.health='black'  # Current selected bar agrees with the death assessment.
    await r.choose('dead')
    request=r.c.hunt.loot_request
    assert request['target']['name']=='Young Wolf'
    assert request['receipt_id']==old and request['image_path']
    assert not request['credit_known'] and not r.c.hunt.credited_kills
    assert r.c.hunt.compact_stage=='loot'


def test_unknown_native_input_cannot_be_reconciled_by_target_life(tmp_path):
    h=HuntState({},'offline')
    target=confirmed_target({'name':'Young Wolf','levels':[1]})
    from types import SimpleNamespace
    history=h.approach_for(target);h.combat_history_key=history['key']
    h.pending={'family':'combat','action':'cast','receipt':{'receipt_id':'partial',
        'completed':False,'dispatch_unknown':True,'generation_after':1},
        'source_frame_id':'before','encounter_id':1}
    h.archive_pending('uncertain_execution')
    assert h.input_effect_unverified
    assert not h.confirm_living_target(target,SimpleNamespace(frame_id='fresh'))
    assert 'input unknown' in h.cast_obligation


@scenario
async def test_clean_burst_guard_cancellation_keeps_completed_input_known(r):
    r.c.config.update(smite_burst_count=3,cast_wait_seconds=2.2)
    async def guard(binding,index):
        return {'continue':index==0,'frame_id':f'fresh-{index}',
            'reason':'selected target no longer alive' if index else 'selected living target'}
    async def sleep(seconds):pass
    r.executor.batch_guard=guard;r.executor._batch_sleep=sleep
    await r.choose('attack_mob_level_1')
    assert len(r.casts())==1 and not r.c.stopped
    pending=r.c.hunt.pending
    assert pending['receipt']['completed'] and not pending['receipt']['dispatch_unknown']
    r.c.hunt.archive_pending('fresh_selected_death_needs_observation')
    assert not r.c.hunt.input_effect_unverified
    assert not r.c.hunt.credited_kills


@scenario
async def test_postcast_target_disappearance_hands_off_suspected_corpse_without_credit(r):
    r.c.config['loot_enabled']=True
    await r.choose('attack_mob_level_1')
    old=r.c.hunt.pending['receipt']['receipt_id']
    r.world.name=''
    await r.choose('inspect_recent_corpse')
    request=r.c.hunt.loot_request
    assert request['suspected'] and not request['death_observed'] and not request['credit_known']
    assert request['target']['name']=='Young Wolf' and request['receipt_id']==old
    assert r.c.hunt.compact_stage=='loot' and r.c.hunt.pending is None
    assert not r.c.hunt.target_dead_observed and not r.c.hunt.credited_kills
    assert not any(x['kind']=='selected_encounter_death' for x in r.c.hunt.progress_facts)
    assert any(x['receipt_id']==old and x['outcome']=='unknown' for x in r.c.hunt.outcomes)
    assert len(r.casts())==1 and not r.c.stopped


@scenario
async def test_recent_corpse_choice_survives_unknown_feedback_archival(r):
    r.c.config['loot_enabled']=True
    await r.choose('attack_mob_level_1')
    old=r.c.hunt.pending['receipt']['receipt_id']
    r.world.name=''
    await r.choose(None);await r.choose(None)
    assert r.c.hunt.pending is None and r.c.hunt.recovery_requested
    await r.choose('inspect_recent_corpse')
    assert r.c.hunt.loot_request['receipt_id']==old
    assert r.c.hunt.loot_request['suspected'] and not r.c.hunt.credited_kills
    assert len(r.casts())==1 and r.c.hunt.recent_combat['handoff_requested']


@scenario
async def test_sage_can_observe_actual_death_after_client_clears_selected_hud(r):
    r.c.config['loot_enabled']=True
    await r.choose('attack_mob_level_1')
    r.world.name=''
    await r.choose('dead_after_selection_cleared')
    assert r.c.hunt.target_dead_observed
    assert not r.c.hunt.credited_kills
    request=r.c.hunt.loot_request
    assert request['death_observed'] and not request['suspected'] and not request['credit_known']
    assert request['target']['name']=='Young Wolf'


@scenario
async def test_postcast_guard_frame_is_retained_in_later_feedback_composite(r):
    r.c.config.update(smite_burst_count=3,cast_wait_seconds=2.2,loot_enabled=True)
    guards=[]
    async def guard(binding,index):
        source=r.world.capture()
        guards.append(source)
        return {'continue':True,'frame_id':source.frame_id,'image_path':source.image_path}
    async def sleep(seconds):pass
    r.executor.batch_guard=guard;r.executor._batch_sleep=sleep
    await r.choose('attack_mob_level_1')
    reference=r.c.hunt.recent_combat['last_guard_frame']
    assert reference['frame_id']==guards[-1].frame_id
    render=r.c.prepare_image;seen=[]
    async def prepare(*args,**kwargs):
        seen.extend(kwargs.get('history',()))
        return await render(*args,**kwargs)
    r.c.prepare_image=prepare
    r.world.name=''
    await r.choose('inspect_recent_corpse')
    assert any(panel[0]==reference['image_path'] and panel[1]==reference['sha256'] for panel in seen)
    assert r.c.hunt.loot_request['last_guard_frame']==reference
    assert not r.c.hunt.credited_kills


@scenario
async def test_zero_input_guard_cancellation_preserves_postkill_frame_for_corpse_inspection(r):
    r.c.config.update(smite_burst_count=3,cast_wait_seconds=2.2,loot_enabled=True)
    async def success(binding,index):return {'continue':True,'frame_id':f'initial-{index}'}
    async def sleep(seconds):pass
    r.executor.batch_guard=success;r.executor._batch_sleep=sleep
    await r.choose('attack_mob_level_1')
    add_current_observer(r)
    terminal=[]
    async def cancelled(binding,index):
        r.world.name=''
        source=r.world.capture();terminal.append(source)
        return {'continue':False,'reason':'selected HUD absent after last cast',
            'frame_id':source.frame_id,'image_path':source.image_path}
    r.executor.batch_guard=cancelled
    await r.choose('attack_mob_level_1')
    assert not r.c.stopped and len(r.casts())==3 and r.c.hunt.pending is None
    reference=r.c.hunt.recent_combat['last_guard_frame']
    assert reference['frame_id']==terminal[-1].frame_id
    # Remove the fake always-present observer for the actual absent target.
    r.c.target_proposal=type(r.c).target_proposal.__get__(r.c)
    await r.choose('inspect_recent_corpse')
    assert r.c.hunt.loot_request['last_guard_frame']==reference
    assert r.c.hunt.loot_request['suspected'] and not r.c.hunt.credited_kills


@scenario
async def test_first_smite_enables_autoattack_once_per_encounter(r):
    await r.choose('attack_mob_level_1')
    assert r.sage.calls[-1]['options']['attack_mob_level_1'].binding['start_attack']
    add_current_observer(r)
    await r.choose('attack_mob_level_1')
    assert not r.sage.calls[-1]['options']['attack_mob_level_1'].binding['start_attack']
    assert sum(event[0]=='text' and event[1]=='/startattack [harm,nodead]' for event in r.backend.events)==1


def install_transient_error(r,kind,text):
    r.world.error=text
    early=r.world.capture()
    r.world.error=''
    r.c.hunt.pending['receipt']['execution']['post_cast_observations']=[
        {'phase':'early_post_cast','observed_error_cues':[{'kind':kind,'text':text}],
         'frame_id':early.frame_id,'image_path':early.image_path,'captured_at':early.captured_at}]
    return early


@scenario
async def test_transient_postcast_range_error_offers_bounded_approach_choices_immediately(r):
    await r.choose('attack_mob_level_1')
    install_transient_error(r,'range','Out of range')
    await r.choose('strafe_right')
    options=r.sage.calls[-1]['options']
    assert {'forward','turn_left','turn_right','backward','strafe_left','strafe_right'} <= options.keys()
    assert 'attack_mob_level_1' not in options
    assert r.c.hunt.cast_error['kind']=='range'
    assert r.c.hunt.pending['purpose']=='cast_correction' and len(r.casts())==1


@scenario
async def test_transient_postcast_facing_error_offers_rotation_immediately(r):
    await r.choose('attack_mob_level_1')
    install_transient_error(r,'facing','Target is not in front of you')
    await r.choose('turn_left')
    options=r.sage.calls[-1]['options']
    assert {'turn_left','turn_right'}<=options.keys() and 'forward' not in options
    assert 'Target not in front means rotate' in options['turn_left'].description
    assert r.c.hunt.cast_error['kind']=='facing' and len(r.casts())==1


@scenario
async def test_transient_postcast_standing_error_offers_brief_stand_pulse(r):
    await r.choose('attack_mob_level_1')
    install_transient_error(r,'standing','You must be standing to do that')
    await r.choose('stand_pulse')
    assert r.sage.calls[-1]['options']['stand_pulse'].binding['hold_seconds']==.08
    assert r.c.hunt.cast_error['kind']=='standing'
    assert r.c.hunt.pending['purpose']=='standing' and len(r.casts())==1


def add_precise_resource_observer(r,health):
    add_current_observer(r)
    original=r.c.target_proposal
    async def observed(frame):
        target=await original(frame)
        return {**target,'hud':{'frame_id':frame.frame_id,'target_health':health[0],
            'target_health_confidence':1.,'player_mana':1.}}
    r.c.target_proposal=observed


@scenario
async def test_repeated_unchanged_target_health_and_mana_offers_visible_correction(r):
    r.c.config.update(smite_burst_count=3,cast_wait_seconds=2.2)
    async def guard(binding,index):return {'continue':True,'frame_id':f'guard-{index}'}
    async def sleep(seconds):pass
    r.executor.batch_guard=guard;r.executor._batch_sleep=sleep
    add_precise_resource_observer(r,[1.])
    await r.choose('attack_mob_level_1')
    await r.choose('attack_mob_level_1')
    assert r.c.hunt.cast_review['unchanged_resource_casts']==1
    await r.choose('approach_for_range_check')
    options=r.sage.calls[-1]['options']
    assert 'attack_mob_level_1' not in options
    assert {'change_search_strategy','reject_selected_target','approach_for_range_check','stand_for_cast'}<=options.keys()
    assert not {'reinspect_cast_problem','no_effect','cannot_assess'} & options.keys()
    assert r.c.hunt.pending['purpose']=='cast_correction' and len(r.casts())==6
    assert not r.c.hunt.credited_kills


@scenario
async def test_range_error_supersedes_generic_review_with_uncertain_health_and_exhausted_inspection(r):
    from sage_wow.agent.grind_inspection import begin
    add_precise_resource_observer(r,[1.])
    await r.choose('attack_mob_level_1')
    await r.choose('attack_mob_level_1')
    r.world.error='Out of range'
    await r.choose(None)
    assert r.c.hunt.cast_review['unchanged_resource_casts']==2
    original=r.c.target_proposal
    async def uncertain(frame):
        target=await original(frame)
        # Exercise an exhausted observation episode without changing receipt,
        # target, search, or cast-review authority.
        for _ in range(3):begin(r.c,frame,target)
        return {**target,'hud':{**target['hud'],'target_health_confidence':.5}}
    r.c.target_proposal=uncertain
    result=await r.choose(None)
    assert result.status=='needs_more_evidence'
    options=r.sage.calls[-1]['options']
    assert len(options)<=20
    assert {'forward','turn_left','turn_right','strafe_left','strafe_right','backward',
        'dead','dead_credited','error_not_supported',
        'ui_blocked','recover_now'}<=options.keys()
    assert 'cannot_assess' not in options
    assert 'dead_or_unrecoverable' not in options  # Current own bar is living.
    assert not {'reinspect_cast_problem','approach_for_range_check','stand_for_cast',
        'change_search_strategy','attack_mob_level_1'} & options.keys()
    assert r.c.hunt.cast_review['unchanged_resource_casts']==2
    assert len(r.casts())==2 and not r.c.hunt.credited_kills


@scenario
async def test_exhausted_range_methods_do_not_reopen_generic_diagnostic_aliases(r):
    add_precise_resource_observer(r,[1.])
    await r.choose('attack_mob_level_1')
    await r.choose('attack_mob_level_1')
    r.world.error='Out of range'
    await r.choose('position_error')
    actions=('forward','turn_left','turn_right','strafe_left','strafe_right','backward')
    for action in actions:
        for _ in range(2):
            await r.choose(action)
            await r.choose('motion_no_useful_effect')
    await r.choose(None)
    options=r.sage.calls[-1]['options']
    assert not set(actions) & options.keys()
    assert not {'approach_for_range_check','stand_for_cast','attack_mob_level_1'} & options.keys()
    assert {'reject_selected_target','change_search_strategy'}<=options.keys()
    assert 'cannot_assess' not in options  # Current calibrated own bars support a concrete exit.
    assert r.c.hunt.cast_obligation and not r.c.hunt.retry_credit
    assert all(r.c.hunt.motion_count(action,'cast_correction',r.c.hunt.approach['history_key'])==2
        for action in actions)
    assert len(r.casts())==2 and not r.c.hunt.credited_kills


@scenario
async def test_current_target_health_decrease_preserves_aggressive_burst(r):
    r.c.config.update(smite_burst_count=3,cast_wait_seconds=2.2)
    async def guard(binding,index):return {'continue':True,'frame_id':f'guard-{index}'}
    async def sleep(seconds):pass
    r.executor.batch_guard=guard;r.executor._batch_sleep=sleep
    health=[1.];add_precise_resource_observer(r,health)
    await r.choose('attack_mob_level_1')
    health[0]=.65
    await r.choose('attack_mob_level_1')
    options=r.sage.calls[-1]['options']
    assert options['attack_mob_level_1'].binding['type']=='cast_burst'
    assert options['attack_mob_level_1'].binding['count']==3
    assert 'approach_for_range_check' not in options
    assert r.c.hunt.cast_review['unchanged_resource_casts']==0
    assert not r.c.hunt.credited_kills


@scenario
async def test_positive_current_selected_health_vetoes_background_corpse_death_choices(r):
    r.c.config['loot_enabled']=True
    health=[1.];add_precise_resource_observer(r,health)
    await r.choose('attack_mob_level_1')
    health[0]=.268
    r.c.combat_log_context='Historical UNIT_DIED Ragged Young Wolf, different creature GUID; old corpse in world'
    await r.choose('attack_mob_level_1')
    options=r.sage.calls[-1]['options']
    assert not {'dead','dead_credited','dead_after_selection_cleared','dead_credited_after_selection_cleared'} & options.keys()
    assert 'attack_mob_level_1' in options and len(r.casts())==2
    assert not r.c.hunt.target_dead_observed and r.c.hunt.loot_request is None
    assert not r.c.hunt.credited_kills
    assert 'older same-name death log' in r.sage.calls[-1]['prompt']


@scenario
async def test_empty_current_target_bar_does_not_remove_actual_death_assessment(r):
    r.c.config['loot_enabled']=True
    health=[1.];add_precise_resource_observer(r,health)
    await r.choose('attack_mob_level_1')
    health[0]=0.
    await r.choose('dead')
    assert r.c.hunt.target_dead_observed and r.c.hunt.loot_request['death_observed']
    assert not r.c.hunt.credited_kills


@scenario
async def test_stale_positive_target_health_is_not_current_death_veto(r):
    await r.choose('attack_mob_level_1')
    r.world.health='black'  # Historical injected health is positive; current pixels are not.
    original=r.c.target_proposal
    async def stale(frame):
        return {**await original(frame),'hud':{'frame_id':'historical-frame','target_health':.268,
            'target_health_confidence':1.}}
    r.c.target_proposal=stale
    await r.choose('dead')
    assert r.c.hunt.target_dead_observed


@scenario
async def test_small_confident_positive_bar_still_withholds_death_choice(r):
    health=[1.];add_precise_resource_observer(r,health)
    await r.choose('attack_mob_level_1')
    health[0]=.01
    await r.choose('attack_mob_level_1')
    assert 'dead' not in r.sage.calls[-1]['options']
    assert not r.c.hunt.target_dead_observed and len(r.casts())==2
