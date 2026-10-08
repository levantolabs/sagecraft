"""Small grind-only decision surface. OCR proposals never establish selection."""
from pathlib import Path
from dataclasses import replace
from copy import deepcopy
import hashlib

from sage_wow.agent.cycle import ActionCandidate, CycleResult
from sage_wow.agent.grind_inventory import effective_loot_enabled, context as inventory_context
from sage_wow.control.target_names import plain_target_name


from sage_wow.agent.grind_cast_feedback import cue_key, unresolved_cues


RANGE_APPROACH_ACTIONS = ('forward', 'turn_left', 'turn_right', 'strafe_left', 'strafe_right', 'backward')


def range_approach_description(action):
    """A distance error supplies no bearing or traversable-path observation."""
    choices = {
        'forward': 'Move forward briefly only if the selected creature is visibly ahead and the safe path reduces the distance.',
        'turn_left': 'Turn left briefly to locate or face the selected creature and inspect a safe approach.',
        'turn_right': 'Turn right briefly to locate or face the selected creature and inspect a safe approach.',
        'strafe_left': 'Sidestep left briefly along visible safe ground to change the approach to the selected creature.',
        'strafe_right': 'Sidestep right briefly along visible safe ground to change the approach to the selected creature.',
        'backward': 'Move backward briefly along visible safe ground to leave an obstructed approach to the selected creature.',
    }
    return ('The cast was out of range; target bearing and a clear path are not established by that error. '
            + choices[action] + ' Reassess the result before another cast; movement alone proves no range correction.')


def correction_action_state(c, frame, target, pending, stage, *, mixed=None, probe=None):
    """A known correction task can ask for an action without re-quizzing presence."""
    h=c.hunt;error=h.cast_error or {};hud=target.get('hud') or {}
    visual=target.get('visual_observation') or {}
    history=h.target_history.get(h.combat_history_key) or {}
    source=error.get('assessment_source') or {}
    from sage_wow.agent.grind_only import key
    from sage_wow.agent.grind_encounter_recovery import capture_scope
    return bool(stage in {'inspect','reinspect'} and pending is None and not mixed and not probe
        and c.config.get('committed_combat',False) and c.fresh(frame) and not c.cycle._scope_error(frame)
        and not h.blocked and not h.input_effect_unverified and not h.active_threat
        and not h.disengagement and not h.loot_request and not c.heal_pending and not h.no_mana
        and not h.encounter_ended and error.get('status')=='active'
        and error.get('kind') in {'range','facing','los','standing'}
        and error.get('encounter_id')==h.encounter and source.get('encounter_id')==h.encounter
        and source.get('target_continuity')==h.target_continuity
        and source.get('target_history_key')==h.combat_history_key
        and h.combat_history_key==(h.approach or {}).get('history_key')
        and source.get('source_scope')==capture_scope(c,frame)
        and (source.get('receipt') or {}).get('receipt_id')==error.get('cast_receipt_id')
        and (source.get('receipt') or {}).get('possible_input')
        and (source.get('receipt') or {}).get('session_epoch')==c.cycle.session_epoch
        and h.known_completed_input(source)
        and history.get('encounter_id')==h.encounter and not history.get('encounter_ended')
        and not history.get('completed') and not history.get('retired_by_acquisition')
        and key(target.get('name'))==h.last_target and key(history.get('name'))==h.last_target
        and key((source.get('target') or {}).get('name'))==h.last_target
        and not target.get('self_target') and not target.get('invalid_text')
        and visual.get('selected_hud')=='present' and visual.get('life_state')=='alive'
        and target.get('eligibility')=='eligible'
        and hud.get('frame_id')==frame.frame_id and hud.get('player_health') is not None
        and hud['player_health']>c.config['critical_health_threshold']
        and (hud.get('health_confidence') or 0)>=.8)


def correction_action_context(c, target, withheld):
    error=c.hunt.cast_error
    unavailable=[item['action'] for item in withheld if item['action'] in
        {*RANGE_APPROACH_ACTIONS,'attack','reject_selected_target','stand_pulse','turn_around'}]
    return (f'World of Warcraft. Selected living eligible creature: {target["name"]}, level {target["levels"]}. '
        f'Our last linked cast reported {error["kind"]}: {error["text"]!r}. '
        'Choose our next action using the CURRENT world scene. The creature is in the world; '
        'its fixed HUD portrait does not show its direction. Choose a short movement to improve the approach, '
        'or clear this target and resume hunting. A turn changes our view/facing; a sidestep changes our path. '
        'Use visible clear ground. The harness will observe the result before permitting another cast. '
        f'Currently unavailable: {unavailable}. Earlier unsuccessful or unassessed methods keep their retry limits. '
        'Passive chat and the normal HUD do not block actions.')


def absence_conflicts(target):
    """Contradiction requires assessment; it does not establish presence."""
    from sage_wow.agent.grind_inspection import selection_conflicts
    return selection_conflicts(target)


def compact_context(level, target, stage, last=None, *, band=None, preference=''):
    low,high=band or target.get('allowed_band',(max(1,level-2),level))
    if stage=='clear':
        return ('World of Warcraft. We just pressed Escape to drop a rejected selection. '
            'Assess the CURRENT selected-target portrait and attached health/level HUD. '
            'Choose target_cleared if that HUD is absent, or clear_failed if it remains. '
            'The BEFORE CLEAR image is historical comparison only. Background creatures, '
            'corpses, passive chat and the player HUD do not establish a selected target. '
            'This assessment does not establish death, damage, loot or kill credit. '
            f'Current target proposals: name {target.get("name")!r}, levels {target.get("levels",[])}. '
            f'Current attributed observation: {target.get("visual_observation",{})}. '
            f'Current HUD resources: {target.get("hud",{})}. '
            'An active typing field or blocking menu requires UI recovery; own death or '
            'an immediate survival emergency requires recovery.')
    if stage=='recovery':
        return (f'Goal: resume finding and fighting eligible creatures at levels {low} to {high}. My level is {level}. {preference} '
            'Current task: choose one useful recovery action from the CURRENT world view. '
            'The selected-target HUD may now be empty even though earlier combat images showed a selected creature. '
            'An empty or uncertain target HUD does not prevent inspecting a possible corpse, selecting a new target, '
            'changing viewpoint on visibly safe ground, or choosing another hunting patch. '
            'Choose among the offered actions using their current visible prerequisites. '
            'Do not wait for proof of past damage or kill credit to choose a recovery action; those outcomes remain unknown. '
            'Only current evidence establishes a target, obstacle, typing field, blocking menu, or survival emergency. '
            'Passive chat and ordinary HUD are normal. Unknown past combat and old selected-target bookkeeping '
            'do not themselves mean the current world cannot be assessed. '
            f'Current target proposals: name {target.get("name")!r}, levels {target.get("levels",[])}. '
            f'Current HUD resources: {target.get("hud",{})}. Last result: {last or "unknown"}.')
    if stage in {'acquire','planning'} and not target.get('name') and target.get('presence') is False:
        return (f'Goal: find and kill creatures at levels {low} to {high}. My level: {level}. {preference} '
            'Current task: find a hunting location. No selected enemy is established. '
            'Use the world view to choose a search direction or an offered hunting-area hypothesis. '
            'An empty target HUD is normal during navigation; an enemy need not already be visible to explore. '
            'Choose short movement on visibly traversable ground, or choose an area to enter the navigation flow. '
            'Area references do not establish a safe route or the current population. '
            f'Current player resources: {target.get("hud",{})}. Last result: {last or "unknown"}. '
            'Passive chat, action bars, minimap and quest markers do not block movement. '
            'Reassess an active typing field, blocking menu, own death or an immediate survival threat.')
    selected = 'inspect the actual current portrait/bar HUD; prior bookkeeping is not a presence answer'
    return (f'Goal: kill eligible creatures. My confirmed level: {level}. Allowed creature levels: '
            f'{low} to {high}. {preference} Selected target: {selected}; name proposal '
            f'{target.get("name")!r}, level proposals {target.get("levels",[])}. '
            +(f'Fresh attributed target observation: {target["visual_observation"]}; eligibility {target.get("eligibility","unknown")}. '
              if target.get('visual_observation') else '')+
            f'Current HUD resources (visible bar fractions, unknown when unreadable): {target.get("hud",{})}. '
            f'Cast diagnostic observations (never damage/kill proof): {target.get("combat_diagnostics",{})}. '
            f'Last relevant result: {last or "unknown"}. '
            'Ordinary passive chat history, action bars, minimap, tooltips and quest tracker are normal HUD. '
            'Only an active typing field, blocking modal/menu or loading prevents world input. '
            'A selected creature attacking us is ordinary combat; urgent means own death, immediate '
            'survival/resource emergency or an unexpected threat requiring interruption. '
            'Own health at or below 30% is a meaningful survival concern; normal damage above that does not by itself interrupt attacks. '
            'Find the actual portrait with attached unit health/level HUD in the current image; '
            'Our cast receipt alone proves neither damage nor kill credit.')


async def decide_combat(c, frame, target, measurement, pending, linked, level):
    # Import lazily: the controller owns composition and all continuity guards.
    from sage_wow.agent.grind_only import composed_evidence_image, key
    from sage_wow.agent.grind_progression import deferred, selected_policy, selected_context, selected_inspection
    from sage_wow.agent import grind_travel_scout as travel_scout
    from sage_wow.agent.grind_encounter_recovery import (
        reassessment_candidate, probe_credit, record_reassessment, consume_probe, disengaging)
    h=c.hunt; controls=c.profile.values['controls']['bindings']
    if pending and 'selected_task_abandon' in pending:
        from sage_wow.agent.grind_relocation import decide_selected_exit
        return await decide_selected_exit(c,frame,target,measurement)
    if (pending and pending.get('family')=='clear' and c.fresh(frame) and not c.cycle._scope_error(frame)
        and (target.get('hud') or {}).get('frame_id')!=frame.frame_id):
        # Clearing must observe the current bars even when resource actions are disabled.
        from sage_wow.agent.grind_resources import hud_resources
        target={**target,'hud':hud_resources(c,frame)}
    from sage_wow.agent.grind_correction_handoff import current as correction_current, retry_current
    measurement={**measurement,'combat_hud':dict(target.get('hud') or {})}
    review=h.review_cast_observations(target,pending,frame,linked=linked,
        fresh=c.fresh(frame) and not c.cycle._scope_error(frame),
        session_epoch=c.cycle.session_epoch,input_generation=c.cycle.input_generation) if c.config.get('committed_combat',False) else None
    review_needed=bool(review and review.get('unchanged_resource_casts',0)>=2)
    review_identity=({'owner':review['owner'],'scope':dict(review['scope']),
        'reviewed_receipts':list(review['reviewed_receipts'])} if review_needed else None)
    review_inspected=bool(review_needed and (review.get('inspection') or {}).get('identity')==review_identity)
    target={**target,'combat_diagnostics':review} if review else target
    if c.fresh(frame) and not c.cycle._scope_error(frame):h.confirm_living_target(target,frame)
    from sage_wow.agent.grind_cast_feedback import retained_error_candidate
    queued_errors=retained_error_candidate(c,frame,target)
    mixed=None if queued_errors else reassessment_candidate(c,frame,target)
    probe=None if queued_errors else probe_credit(c,frame,target)
    visual=target.get('visual_observation') or {}
    fresh_living=c.fresh(frame) and not c.cycle._scope_error(frame) and visual.get('selected_hud')=='present' and visual.get('life_state')=='alive' and target.get('eligibility')=='eligible'
    explicit_exit=bool((h.strategy_required or {}).get('selected_exit') or disengaging(c))
    scouting_opportunity=fresh_living and not explicit_exit and not (pending and pending.get('family') != 'target') and not deferred(c,target,frame,measurement) and (h.strategy_required or {}).get('reason') in {'out_of_band_scout','empty_local_search','level_progression'}
    if c.config.get('committed_combat',False) and h.compact_stage=='recovery' and target.get('name') and h.selected_presence is not False:
        h.compact_stage='inspect'
    # A living HUD does not cancel Sage's accepted exit from an unresolved
    # engagement. Pending input feedback still takes precedence below.
    if fresh_living and not explicit_exit and (scouting_opportunity or not (h.planning_requested and h.phase=='choose_area')):
        h.planning_requested=False
        if scouting_opportunity and h.phase=='choose_area':h.phase='search'
        if h.compact_stage in {'acquire','recovery','reinspect','planning'}:
            h.compact_stage='inspect';h.selected_presence=True
    # Schedule an actionable review, never a veto on the cast needed to end a
    # dry spell. Useful observed movement is progress toward hunting, not damage.
    if not pending and h.active_seconds-max(h.no_progress_at,h.last_progress_active_at,h.progress_review_at)>=360:
        h.progress_review_at=h.active_seconds
        prior_stage=h.compact_stage
        recovery=h.request_recovery('no_combat_progress',c.last_signature)
        if target['name'] and prior_stage!='acquire':h.compact_stage='inspect'
        c.event('grind_progress_recovery_requested',dict(recovery))
    if c.fresh(frame) and not c.cycle._scope_error(frame) and not travel_scout.active(c):
        recovery_plan=h.plan_unresolved_recovery(frame,target)
        if recovery_plan:c.event('grind_recovery_planning_requested',recovery_plan)
        empty_search=h.plan_empty_search(frame,target)
        if empty_search:c.event('grind_empty_search_planning',empty_search)
    stage='planning' if h.planning_requested and h.phase=='choose_area' else h.compact_stage
    if selected_inspection(c,frame,target,pending,measurement): stage='inspect'
    if pending and pending['family']=='combat': stage='feedback'
    elif pending and pending['family']=='clear': stage='clear'
    elif pending and pending['family']=='target': stage='inspect'
    elif travel_scout.active(c): stage='reinspect' if h.compact_stage=='reinspect' else 'inspect'
    elif stage not in {'acquire','inspect','reinspect','planning','recovery'}: stage='inspect' if target['name'] else 'acquire'
    # Reconcile this receipt before using historical death to resume hunting.
    if not pending and not travel_scout.active(c) and h.target_dead_observed and not fresh_living and stage not in {'recovery','planning','reinspect'}:stage='acquire'
    from sage_wow.agent.grind_hunt_entry import idle_acquisition, acquisition_context
    from sage_wow.agent import grind_loot_budget as loot_budget
    if stage in {'acquire','recovery','planning'} and (not target.get('name') or loot_budget.handoff_pending(c)):
        # Resource processing may be disabled or unable to act. Gate the small
        # idle menu on this frame's own bars, not old recovery bookkeeping.
        from sage_wow.agent.grind_resources import hud_resources
        target={**target,'hud':hud_resources(c,frame)}
    from sage_wow.agent.grind_navigation_recovery import goal_capacity_exhausted
    retained_travel_debt=bool(h.travel_policy.get('retry_menu')) or goal_capacity_exhausted(c)
    retained_travel_menu=(travel_scout.travel_owned(c) and retained_travel_debt
        and not travel_scout.threat_current(c,frame))
    action_search=stage in {'acquire','recovery'} and not retained_travel_menu and (idle_acquisition(c,target,pending,frame)
        or loot_budget.acquisition_ready(c,target,pending,frame))
    action_planning=stage=='planning' and idle_acquisition(c,target,pending,frame)
    target={**target,'presence':h.selected_presence,'allowed_band':h.target_band(level)}
    from sage_wow.agent.grind_ineffective_cast import recovery_candidate, action_context
    ineffective=recovery_candidate(c,frame,target,pending,stage)
    ineffective_probe=recovery_candidate(c,frame,target,pending,stage,retry=True)
    semantic_retry=bool(h.retry_credit and (h.retry_credit_source or {}).get('kind')=='ineffective_cast_correction')
    from sage_wow.agent import grind_inspection as inspection
    boundary_mode = bool((action_search or stage=='inspect') and inspection.needs_boundary(c,frame,target,pending))
    boundary_exhausted=bool(boundary_mode and inspection.boundary_spent(c))
    if boundary_exhausted:
        handed_off=inspection.handoff_boundary(c,frame,target,pending)
        pending=h.pending;linked=h.linked(frame,c.cycle.input_generation)
        if not handed_off:
            from sage_wow.agent.grind_navigation_recovery import wait
            return wait(c,'interrupted_absence_boundary_exhausted',frame,target)
        if h.phase=='travel':return await c.travel(frame,await c.rows(frame),target,measurement)
        stage='planning' if h.planning_requested and h.phase=='choose_area' else 'acquire'
        idle=idle_acquisition(c,target,pending,frame)
        action_search=stage=='acquire' and idle;action_planning=stage=='planning' and idle
    boundary = inspection.boundary_request(c,frame,target) if boundary_mode and not boundary_exhausted else None
    if boundary:
        stage='inspect';action_search=False;action_planning=False
    identity=str(('ineffective_cast',ineffective['owner'],ineffective['receipt_id']) if ineffective else
        ('compact',stage,pending['receipt']['receipt_id'] if pending else h.selection_revision,h.encounter))
    if boundary:identity=boundary['key']
    if not c.select_question(identity,stage):return CycleResult('grind_blocked',detail='Unresolved question capacity retained')
    options=[];meta={};withheld=[];scout_states={}
    malformed_name=bool(target.get('name')) and not plain_target_name(target['name'])
    hud=target.get('hud') or {}
    selected_living_bar=bool(c.fresh(frame) and not c.cycle._scope_error(frame)
        and hud.get('frame_id')==frame.frame_id and target.get('name')
        and not target.get('self_target') and hud.get('target_health') is not None
        and hud['target_health']>0 and (hud.get('target_health_confidence') or 0)>=.8)
    valid=lambda:c.fresh(frame) and not c.cycle._scope_error(frame) and not h.blocked and not h.input_effect_unverified
    from sage_wow.agent.grind_relocation import exhausted_exit, exit_context
    action_exit=exhausted_exit(c,frame,target,pending,stage,mixed=mixed,probe=probe)
    action_correction=not action_exit and correction_action_state(c,frame,target,pending,stage,mixed=mixed,probe=probe)
    if ineffective:
        # Keep physical corrections under ordinary incoming threat; only the
        # small idle-style prompt excludes urgent/current-threat decisions.
        action_correction=bool(not h.active_threat and hud.get('frame_id')==frame.frame_id
            and hud.get('player_health') is not None and hud['player_health']>c.config['critical_health_threshold']
            and (hud.get('health_confidence') or 0)>=.8)
    scout=None
    low,high=h.target_band(level)
    if (level in h.target_bands_by_player_level and valid() and visual.get('selected_hud')=='present'
        and visual.get('life_state')=='alive' and visual.get('target_kind')=='creature'
        and type(visual.get('level')) is int and not low<=visual['level']<=high
        and target.get('name') and not target.get('self_target') and not target.get('invalid_text')
        and target.get('levels')==[visual['level']]):
        scout={'frame_id':frame.frame_id,'captured_at':frame.captured_at,
            'source_image':frame.image_path,'source_hash':hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
            'session_epoch':c.cycle.session_epoch,'input_generation':c.cycle.input_generation,
            'name':target['name'],'level':visual['level'],'band':[low,high],
            'visual_provenance':dict(target.get('visual_provenance') or {})}
    def current_review():
        return (valid() and h.cast_review is review and review_identity is not None
            and not review.get('retired_by') and review.get('unchanged_resource_casts',0)>=2
            and review['owner']==str((key(target['name']),h.target_lives.get(key(target['name']),0)))
            and not h.target_history.get(review['owner'],{}).get('completed')
            and {'owner':review.get('owner'),'scope':review.get('scope'),
                'reviewed_receipts':review.get('reviewed_receipts')}==review_identity)
    def observe(name,text,**data):
        if boundary_exhausted and (data.get('absence') or data.get('reinspect') or name=='cannot_assess'):
            withheld.append({'action':name,'reason':'interrupted absence boundary assessment capacity spent'})
            return
        if action_correction and data.get('reinspect'):
            withheld.append({'action':name,'reason':'fresh selected living eligibility is established; choose a correction action'})
            return
        if data.get('strategy') and stage=='planning':
            withheld.append({'action':name,'reason':'Already planning; choose an actual destination or visible exploration path'})
            return
        if action_exit and data.get('reinspect'):return
        factual = bool(data.get('reinspect') and not data.get('cast_reinspect'))
        if factual:
            state = inspection.availability(c,frame,target)
            if state['state'] != 'runnable':
                withheld.append({'action':name, 'reason':state['reason'], 'sensing_availability':state})
                return
            data['factual_reinspect'] = True
        if data.get('absence') and not data.get('cleared') and absence_conflicts(target):
            withheld.append({'action':name,'reason':'absence conflicts with current calibrated selected evidence'})
            return
        if name=='no_selected_frame':
            from sage_wow.agent.grind_absence_handoff import capture
            data['encounter_absence']=capture(c,frame,target,pending)
        if data.get('death') and selected_living_bar:
            withheld.append({'action':name,'reason':'current calibrated selected-target health bar is positively alive',
                'frame_id':frame.frame_id,'target_health':hud['target_health'],
                'target_health_confidence':hud['target_health_confidence']})
            return
        precondition=(lambda:valid() and reassessment_candidate(c,frame,target)==mixed) if data.get('mixed_assessment') else current_review if data.get('cast_reinspect') else (lambda:valid() and inspection.availability(c,frame,target)['state']=='runnable') if factual else None
        options.append(ActionCandidate(name,text,{'type':'observe_only'},
            **({'precondition':precondition} if precondition else {})));meta[name]=data
    tab_witness = {'value':None}
    async def motor(name,binding,text,family,purpose,guard=None,seconds=None,allowance=None,**data):
        if family=='clear':
            from sage_wow.agent.grind_clear import DESCRIPTION
            text+=DESCRIPTION
        if name=='reject_selected_target' and inspection.selected_unavailable(c,frame,target) and not (action_exit or action_correction or ineffective or target.get('eligibility')=='ineligible' or inspection.current_combat_owner(c,target,pending)):
            withheld.append({'action':name,'reason':'unassessed selected sensing requires accepted strategy and typed task release'})
            return
        if allowance and not allowance():
            withheld.append({'action':name,'reason':'two ineffective or unassessed corrections for this selected target'});return
        if family=='clear' and not h.allowed(binding,family):
            withheld.append({'action':name,'reason':'two ineffective or unresolved clears in this observed sector'});return
        b=controls.get(binding) or {}
        if not b.get('verified_from') or type(b.get('keycode')) is not int:
            withheld.append({'action':name,'reason':'verified binding unavailable'});return
        if seconds is None:seconds=1.0 if binding=='forward' else c.config['turn_seconds'] if binding.startswith('turn') else .15 if family=='motion' else .08
        raw = await c.raw_target_proposal(frame) if family=='target' and guard is None else None
        if raw is not None:tab_witness['value']=inspection.prepare_witness(c,frame,raw)
        def motor_valid():
            allowed = (valid() and (not allowance or allowance())
                and (not data.get('renew_attempt') or h.pixel_renewal_allowed(target))
                and (family!='target' or h.target_selection_available()))
            if raw is not None and not inspection.witness_current(c,frame,raw,tab_witness['value']):
                tab_witness['value']=None
            return allowed
        options.append(ActionCandidate(name,text,{'type':'keypress','keycode':b['keycode'],'hold_seconds':seconds},
            precondition=motor_valid,dispatch_guard=guard))
        meta[name]={'action':binding,'family':family,'purpose':purpose,**data}
    def corrected_rejection(history):
        """A linked changed target relation can reconsider a failed clear."""
        correction=h.correction_evidence or {}
        if (not valid() or not fresh_living or h.pending is not None or not h.retry_credit
            or history['key']!=h.combat_history_key or not correction.get('granted')
            or correction.get('purpose') not in {'cast_correction','standing'}
            or correction.get('outcome')!='motion_useful'
            or correction.get('encounter_id')!=h.encounter
            or not correction_current(c,frame,target)
            or correction.get('generation_after')!=c.cycle.input_generation):return None
        return {'source':'linked_observed_correction','history_key':history['key'],
            'rejections':history['rejections'],'correction_receipt_id':correction['receipt_id'],
            'frame_id':frame.frame_id,'input_generation':c.cycle.input_generation,
            'session_epoch':c.cycle.session_epoch}
    async def attacks(*, renewable=False, same=False, continue_living=False, reassess_rejection=None, acquisition=None, mixed_probe=None):
        if current_review():
            withheld.append({'action':'attack','reason':'repeated unchanged target health and own mana require a correction or selected encounter exit before another cast'})
            return
        if semantic_retry and not ineffective_probe:
            withheld.append({'action':'attack','reason':'ineffective-cast correction proof is no longer current; choose a fresh correction or clear'})
            return
        if queued_errors:
            withheld.append({'action':'attack','reason':'remaining receipt-bound error cues require assessment after the current correction'})
            return
        if deferred(c,target,frame,measurement):
            withheld.append({'action':'attack','reason':selected_context(selected_policy(c,target,frame,measurement))})
            return
        if not plain_target_name(target.get('name')):
            withheld.append({'action':'attack','reason':'observed target name does not meet the executor binding contract; reinspect without guessing or altering the text'})
            return
        low,high=h.target_band(level)
        exact_band=level in h.target_bands_by_player_level
        if exact_band and len(target['levels'])!=1:
            withheld.append({'action':'attack','reason':'requested exact level band needs a fresh numeric target observation; a combined attack choice cannot supply the missing level'})
            return
        for n in range(low,high+1):
            if target['levels'] and target['levels']!=[n]:continue
            correction_probe=bool(reassess_rejection and reassess_rejection.get('source')=='linked_observed_correction')
            ineffective_retry=bool(h.no_effect_retries>=2 and h.retry_credit)
            retry_owner=h.combat_history_key
            correction_credit=dict(h.retry_credit_source or {}) if (h.retry_credit_source or {}).get('kind') in {'linked_correction','ineffective_cast_correction'} else None
            count=1 if review_needed or acquisition or mixed_probe or correction_probe or ineffective_retry else c.config.get('smite_burst_count',1)
            binding=({'type':'cast_burst','spell':'Smite','count':count,
                'interval_seconds':c.config['cast_wait_seconds'],'expected_target_name':target['name']}
                if count>1 else {'type':'cast_guarded','spell':'Smite'})
            if c.config.get('committed_combat',False):
                history=h.approach_for(target,renew_completed=False)
                binding['start_attack']=bool(renewable or not history or not history.get('auto_attack_started'))
                binding['expected_target_name']=target['name']
            text=(f'The actual selected portrait/bar frame is present: living eligible non-player hostile or attackable neutral exactly level {n}; '
                'Smite ready, normal world, plausible range/facing. '
                +(f'Cast a guarded burst of {count} Smites; each pulse requires a fresh matching living target. ' if count>1 else 'Cast one Smite, including a bounded range probe. ')
                +('Repeated target-health and own-mana observations did not change: choose this single cast only after checking stance, visible range and facing; otherwise use a concrete correction. ' if review_needed else '')
                +'Ordinary incoming damage is normal combat. Prior damage may remain unknown; do not claim damage or kill credit from receipts.')
            if mixed_probe:text+=' This is the single probe from your mixed-result reassessment. Inspect current facing/range; earlier damage did not establish them. Failed corrections remain recorded.'
            if correction_probe:text+=' Your linked useful correction changed the selected-target relation after the earlier rejection. Reconsider it with one guarded cast; rejection and failed-method history remain recorded.'
            if ineffective_retry:text+=' This is one guarded retry after earlier ineffective casts; assess its result before any further cast.'
            dispatch_policy = {}
            def attack_valid(expected_level=n, policy_state=dispatch_policy):
                from sage_wow.agent.grind_progression import dispatch_basis
                if policy_state and policy_state.get('basis') != dispatch_basis(c):return False
                if renewable and not h.pixel_renewal_allowed(target):return False
                if not valid() or deferred(c,target,frame,measurement) or not retry_current(c,frame,target):return False
                if correction_credit and (not h.retry_credit or h.retry_credit_source!=correction_credit):return False
                if ineffective_retry and (not h.retry_credit or h.combat_history_key!=retry_owner):return False
                if ineffective_probe and recovery_candidate(c,frame,target,pending,stage,retry=True)!=ineffective_probe:return False
                if mixed_probe and probe_credit(c,frame,target)!=mixed_probe:return False
                if acquisition:
                    from sage_wow.agent.grind_acquisition import acquisition_proof
                    if (h.pending is not pending or acquisition_proof(c,frame,pending,
                        h.linked(frame,c.cycle.input_generation))!=acquisition):return False
                if exact_band and (target['levels']!=[expected_level]
                    or h.target_band(c.level.last_confirmed_level)!=(low,high)):
                    return False
                if not reassess_rejection:return True
                proof=reassess_rejection;current=h.target_history.get(proof['history_key'])
                if proof.get('source')=='linked_observed_correction':
                    return current is not None and corrected_rejection(current)==proof
                return (h.pending is pending and pending is not None and h.known_completed_input(pending)
                    and h.linked(frame,c.cycle.input_generation)
                    and pending.get('family')=='target' and pending.get('action')=='target_enemy'
                    and pending['receipt'].get('possible_input')
                    and pending['receipt']['receipt_id']==proof['target_receipt_id']
                    and pending['receipt'].get('session_epoch')==proof['session_epoch']
                    and pending['receipt'].get('generation_after')==proof['input_generation']
                    and c.cycle.input_generation==proof['input_generation'] and c.cycle.session_epoch==proof['session_epoch']
                    and current is not None and current['rejections']==proof['rejections']
                    and (h.approach or {}).get('history_key')==proof['history_key'])
            options.append(ActionCandidate(f'attack_mob_level_{n}',text,binding,
                precondition=attack_valid,dispatch_guard=await c.guard(frame,target,exact_level=n,strategy_state=dispatch_policy)))
            meta[f'attack_mob_level_{n}']={'action':'cast','family':'combat','purpose':'cast','mob_level':n,
                'dispatch_policy':dispatch_policy,'inspect_living':True,'renew_attempt':renewable,'new_encounter':renewable or not same,
                'continue_living':continue_living,**({'effect':'unknown'} if continue_living else {})}
            if acquisition:
                meta[f'attack_mob_level_{n}'].update(acquisition=acquisition,new_encounter=True)
            if mixed_probe:meta[f'attack_mob_level_{n}']['mixed_probe']=mixed_probe
            if reassess_rejection:meta[f'attack_mob_level_{n}']['reassess_rejection']=reassess_rejection
    if pending and pending.get('family')=='combat' and not h.recent_combat:
        h.retain_combat_evidence(pending)
    recent=h.recent_combat
    selection_missing=(visual.get('selected_hud')=='absent' or not target.get('name') and visual.get('selected_hud')!='present')
    if (stage!='clear' and effective_loot_enabled(c) and recent and not recent.get('handoff_requested')
        and loot_budget.request_allowed(c,recent)
        and selection_missing and not h.intentional_clear_explains(recent) and not h.input_effect_unverified and h.known_completed_input(recent)):
        observe('inspect_recent_corpse','Our completed recent attack was followed by disappearance of the selected target HUD. Inspect the nearby recent fight for a corpse and possible loot now. Disappearance alone proves neither death nor our kill credit; keep prior damage unknown.',suspect_corpse=True)
    cues=measurement.get('error_cues',[])
    specific_cast_error=False
    error_task=None
    if stage=='feedback':
        question='What happened after the last Smite at this selected creature?'
        if not linked:
            observe('lost','The earlier selection/receipt continuity is lost without proven death; archive unknown outcome and reacquire.',lost=True)
        else:
            fresh=unresolved_cues(pending,frame,measurement)
            if fresh:
                cue=fresh[0]
                error_task=cue
                question=('An error was shown after this linked cast. Which offered correction or error assessment should we take? '
                    'Damage may also have occurred; the error does not establish that the entire cast failed.')
                observe('position_error' if cue['kind'] in {'range','facing','los'} else 'cast_error_observed',f'The linked post-cast evidence supports this {cue["kind"]} error: {cue["text"]!r}. Retain it for correction or reassessment; damage may also have occurred.',effect='combat_unchanged',cast_error=cue)
                if c.config.get('committed_combat',False) and key(target.get('name'))==key((pending.get('target') or {}).get('name')):
                    corrections={'range':RANGE_APPROACH_ACTIONS,'facing':('turn_left','turn_right'),
                        'los':('strafe_left','strafe_right','backward'),'standing':('forward',)}.get(cue['kind'],())
                    specific_cast_error=bool(corrections)
                    for action in corrections:
                        purpose='standing' if cue['kind']=='standing' else 'cast_correction'
                        owner=pending.get('target_history_key')
                        allowance=lambda action=action,purpose=purpose,owner=owner: (h.pending is pending
                            and owner==h.combat_history_key and owner==(h.approach or {}).get('history_key')
                            and h.motion_count(action,purpose,owner)<2)
                        option='stand_pulse' if cue['kind']=='standing' else action
                        if cue['kind']=='range':
                            description=range_approach_description(action)
                        else:
                            description=(f'Our just-completed cast reports {cue["text"]!r}. '
                                +('Target not in front means rotate toward the visibly selected creature.' if cue['kind']=='facing' else
                                  'Must be standing means one brief movement pulse to stand, then reassess.' if cue['kind']=='standing' else
                                  'Line of sight is blocked: change the visible approach on safe ground.'))
                        await motor(option,action,description,'motion',purpose,
                            c.travel_guard(frame,target,selected=True),seconds=.08 if cue['kind']=='standing' else None,
                            allowance=allowance,inspect_living=True,cast_error=cue,effect='combat_unchanged')
                observe('error_not_supported',f'This proposed {cue["kind"]} cue ({cue["text"]!r}) is false, stale, unrelated or not visibly supported for this cast. Reject only this cue and keep the cast receipt; no retry permission. A faded current message alone does not invalidate the retained post-cast image.',reject_error=[cue_key(cue)])
            same=key(target['name'])==key(pending.get('target',{}).get('name'))
            if same:
                if not error_task:
                    observe('damaged_alive','Linked BEFORE/AFTER shows this same creature actually lost health and is still alive; own contribution cannot be established from the evidence; do not choose if our attributable hit is visible.',effect='combat_effect',damage=True)
                    observe('own_damaged_alive','Linked images/combat log visibly attribute a damaging hit to our Smite on this same living creature, even if others assist.',effect='combat_effect',damage=True,own_damage=True)
                observe('dead','This same creature is visibly dead. Absence alone is insufficient; own kill credit cannot be established; do not choose if own damage AND kill credit are visibly attributable.',effect='combat_effect',death=True)
                observe('dead_credited','This same creature is visibly dead AND our linked damaging/lethal hit is attributable AND own kill credit is explicit in our lethal/credit log or own XP increase over an uncontaminated interval with no other plausible XP event. Assistance is allowed; corpse/receipts alone are insufficient.',effect='combat_effect',death=True,own_damage=True,credit=True)
                if error_task:
                    withheld.append({'action':'ordinary_cast_outcomes','reason':'assess or correct the linked error first; damage or no-effect answers would discard the unresolved cue'})
            if selection_missing and h.known_completed_input(pending):
                observe('dead_after_selection_cleared','The previously selected creature from the linked BEFORE fight is now visibly a corpse in the CURRENT view or retained AFTER CAST GUARD frame. Identify that same recent creature from its visible corpse/log evidence; target HUD absence alone is insufficient. Our kill credit remains unknown.',effect='combat_effect',death=True,recent_target=pending.get('target'))
                observe('dead_credited_after_selection_cleared','The linked recent creature is visibly dead AND our damaging/lethal hit and its kill credit are explicit in visible combat/XP evidence in the CURRENT view or retained AFTER CAST GUARD frame. No other plausible XP event; HUD absence or input receipts alone prove neither death nor credit.',effect='combat_effect',death=True,own_damage=True,credit=True,recent_target=pending.get('target'))
            if (same and fresh_living and not fresh and h.known_completed_input(pending)
                and not h.input_effect_unverified and not (h.cast_error and h.cast_error.get('status')=='active')):
                await attacks(same=True,continue_living=True)
            observe('casting','Our cast or cooldown is visibly still active; wait and retain this exact receipt.',cast_active=True)
            observe('lost','Selection disappeared or changed without proven death. Archive unknown outcome and reacquire; no kill credit.',lost=True)
    elif stage=='clear':
        question='Did the rejected selection clear?'
        observe('target_cleared','The actual selected portrait/bar frame is now absent. Clear verified; acquire afresh without inheriting old outcome evidence.',effect='target_cleared',cleared=True,absence=True)
        observe('clear_failed','The selected frame remains; no fresh attempt allowance.',effect='clear_failed')
    elif stage=='planning':
        question='Choose the next hunting destination or visible exploration path. Unresolved movement or casting does not establish an empty local target search.'
        if (h.strategy_required or {}).get('reason')=='level_progression':
            question='Choose a different hunting subregion for the current primary targets. Leave the starting wolf patch; catalog locations are hypotheses and require safe visual travel and fresh target inspection.'
        if (h.strategy_required or {}).get('reason')=='empty_local_search':
            question='Repeated Tab attempts found no selected enemy here. Which hunting area or visible exploration path should we travel toward next?'
        if (h.strategy_required or {}).get('reason')=='out_of_band_scout':
            question=f'The inspected creature was outside the requested levels {low}–{high}. Which attributed area or different visible path should we scout for suitable creatures? One sighting does not establish the whole population.'
        fixed_area=c.config.get('fixed_hunting_area')
        fixed_here=False
        local_selected_exit=False
        if fixed_area:
            from sage_wow.agent.grind_progression import fixed_region_current
            fixed_here=fixed_region_current(c,frame,measurement)
            from sage_wow.agent.grind_relocation import current_combat_exit
            local_selected_exit=fixed_here and current_combat_exit(c,frame,target,pending)
            question='Stay on the user-selected hunting grounds and kill the configured nearby creatures until the level goal. Return to those grounds if outside; once there, keep searching locally. Brief combat positioning is allowed. Do not tour other hunting areas.'
            observe('wait_for_local_opportunity','Wait five seconds for a local opportunity or a clearer route, then observe again. No movement, target claim, progress or retry allowance.',local_wait=True)
            if local_selected_exit:
                question='We chose to leave this unresolved encounter while staying on the same hunting grounds. Clear the selected target and verify the result before searching locally, or wait for a changed situation.'
                await motor('reject_selected_target','escape',
                    'Clear this unresolved selected encounter so we can search for another nearby creature in the same hunting area. This does not claim the creature is dead or ineligible. Verify that the selection cleared before acquiring another target.',
                    'clear','clear',await c.clear_guard(frame,target),reject=True,abandon=True,
                    allowance=lambda:current_combat_exit(c,frame,target,pending))
        for area in ([] if fixed_here else h.candidates(level)[:2]):
            observe('choose_area:'+area['id'],f'Try attributed hunting hypothesis {area["label"]!r} at {area.get("coordinate")}; expected creatures {area.get("primary_targets",[])}. Source: {area.get("source",{})}. Route and current population remain unverified.',area=dict(area))
        if not fixed_area:
            observe('explore_visible','Choose a different currently visible safe exploration patch; no invented offscreen route.',visual=True)
        if (fixed_here and not local_selected_exit) or (not fixed_area and not h.strategy_required and not h.hunting_progression_by_player_level.get(level)):
            observe('search_here','Continue inspecting this visible patch without resetting failed methods.',phase='search')
        else:
            withheld.append({'action':'search_here','reason':
                'clear and verify the unresolved selected encounter before acquiring another local target' if local_selected_exit else
                'requested strategy change still needs observed useful movement; this does not establish that nearby targets are absent'})
    elif stage in {'acquire','recovery'}:
        recovering=stage=='recovery'
        paged_travel_search=travel_scout.travel_owned(c) and retained_travel_debt
        optional_search_blocked=paged_travel_search and not travel_scout.threat_current(c,frame)
        question=('Which offered action should we take now to find and select a hunting target?' if action_search else
            'Current evidence or local methods have not resolved the hunt. Choose one safe action to change the view, recheck the selected HUD, or inspect another patch.' if recovering else
            'No usable living target is selected. What single action should we take to find one?')
        if recovering and not action_search:
            observe('reinspect_selected_frame','Reinspect the current magnified selected-target HUD on a fresh frame. A visible out-of-band unit is present but ineligible; missing text is uncertainty.',reinspect=True)
            if target['name']:
                await motor('reject_selected_target','escape','The selected unit is unsuitable or its encounter cannot be resolved. Clear this actual selection once, then verify absence. Unknown prior effects remain unknown.','clear','clear',await c.clear_guard(frame,target),
            reject=not (selected_policy(c,target,frame,measurement)['kind']=='ground_unknown' and target.get('eligibility')=='eligible'),abandon=bool(h.cast_obligation),out_of_band_scout=scout)
        if optional_search_blocked:
            withheld.append({'action':'target_enemy','reason':'retained travel question owns optional scouting; restore current travel evidence before its remaining menu page'})
        elif travel_scout.travel_owned(c) and not travel_scout.threat_current(c,frame):
            scout_choice=travel_scout.candidate(c,frame,target,measurement,valid)
            if scout_choice:
                option,scout_state=scout_choice
                options.append(option);scout_states[option.option]=scout_state
                meta[option.option]={'action':'target_enemy','family':'target','purpose':'target'}
            else:withheld.append({'action':'target_enemy','reason':'travel scout requires idle current evidence and a new attributable patch after its previous selection'})
        elif h.target_selection_available():
            opening=(' If the fresh selection meets the target rules, the harness immediately attempts one opening Smite.'
                if c.config.get('target_opening_cast') else ' Assess the fresh selection before attacking.')
            threat_targeting=travel_scout.travel_owned(c)
            await motor('target_enemy','target_enemy','Select a nearby enemy with Tab.'+opening,'target','target',
                c.travel_guard(frame,target,selected=False) if threat_targeting else None,
                allowance=(lambda:travel_scout.threat_current(c,frame)) if threat_targeting else None)
        else:withheld.append({'action':'target_enemy','reason':'two unsuccessful selections; waiting for the bounded selection retry cooldown or verified changed search sector'})
        base_moves=('turn_left','turn_right','forward')
        expanded=recovering and (not action_search or sum(h.motion_count(a,'search')>=2 for a in base_moves)>=2)
        for action in (base_moves+('backward','strafe_left','strafe_right') if expanded else base_moves):
            if optional_search_blocked:
                withheld.append({'action':action,'reason':'retained travel question owns optional search movement; generic recovery cannot renew its menu coverage'})
            elif h.motion_count(action,'search')<2:
                description=('Move forward 1.0 seconds on the visible safe path to search.' if action=='forward' else
                    f'Turn {action.split("_")[1]} {c.config["turn_seconds"]:.2f} seconds to inspect another visible sector.' if action.startswith('turn') else
                    f'One short {action} movement on visibly safe ground to change the obstructed viewpoint; assess the result before repeating.')
                await motor(action,action,description,'motion','search',c.travel_guard(frame,target,selected=False),
                    allowance=(lambda:travel_scout.threat_current(c,frame)) if paged_travel_search else None)
            else:withheld.append({'action':action,'reason':'two ineffective or unresolved attempts in this observed search sector'})
        # Replanning is available before every ordinary motor is exhausted. A
        # declaration does not renew physical allowances.
        if not action_search or h.recovery_requested or h.confirmed_target_absences>=2 or len(options)<2:
            observe('change_search_strategy','Choose another hunting approach or destination; retain the actual targeting and movement outcomes.',strategy=True)
    else:
        question=('Clear this unresolved target or choose a hunting destination?' if action_exit else
            'Which offered correction or disengagement action should we take after these ineffective casts?' if ineffective else
            'Which offered action should we take next?' if action_correction else
            'Is an actual selected target portrait/bar frame present, and what should we do next? Background creatures and tooltips do not count as a selected frame.')
        history=h.approach_for(target,renew_completed=False) if target['name'] else None
        renewable=h.target_dead_observed or (h.attempt_absence and h.attempt_changed_view and h.pixel_renewal_allowed(target))
        if history:
            same=history['key']==h.combat_history_key
            obligation=history['cast_obligation'] or (h.cast_obligation if same else None)
            debt=history['combat_failures'] if not same else max(history['combat_failures'],h.failures['combat'])
            exhausted=not renewable and history['correction_rounds']>=3
            rejected=history['rejections']>(history.get('rejection_reassessment') or {}).get('rejections',0)
            reassessment=corrected_rejection(history) if rejected else None
            # A fresh eligible selection can reconsider an earlier rejection;
            # it does not identify a new creature or erase old combat debt.
            if (rejected and fresh_living and linked and pending and pending.get('family')=='target'
                and pending.get('action')=='target_enemy' and h.known_completed_input(pending)
                and pending['receipt'].get('possible_input')
                and pending['receipt'].get('session_epoch')==c.cycle.session_epoch):
                reassessment={'history_key':history['key'],'rejections':history['rejections'],
                    'target_receipt_id':pending['receipt']['receipt_id'],'frame_id':frame.frame_id,
                    'input_generation':c.cycle.input_generation,'session_epoch':c.cycle.session_epoch}
            permission=renewable or bool(probe) or (not rejected or reassessment is not None) and not exhausted and (not obligation and debt<2 or h.retry_credit and same)
            # A completed fresh acquisition is a new local attempt. A species
            # name cannot carry an old positional error as current authority.
            from sage_wow.agent.grind_acquisition import acquisition_proof
            acquisition=(acquisition_proof(c,frame,pending,linked)
                if (fresh_living or selected_living_bar) and
                    (obligation or debt>=2 or exhausted) else None)
            if acquisition:permission=True
            purpose='cast_correction' if obligation and not renewable else 'approach'
            if same and h.cast_error and h.cast_error.get('encounter_id')==h.encounter and h.cast_error['kind']=='standing' and h.cast_error.get('status')=='active' and h.motion_count('forward','standing',history['key'])<2:
                await motor('stand_pulse','forward','Fresh must-stand error: one safe0.08-second movement pulse, then assess stance/target relation before a fresh cast probe. No sit toggle.','motion','standing',c.travel_guard(frame,target,selected=True),seconds=.08)
            low,high=h.target_band(level)
            visual=target.get('visual_observation')
            visual_suitable=target.get('eligibility','unknown' if visual else 'eligible')=='eligible'
            if not visual_suitable:withheld.append({'action':'attack','reason':'selected target eligibility is '+str(target.get('eligibility','unknown'))})
            if rejected and not reassessment and not probe:withheld.append({'action':'attack','reason':'this local target attempt was rejected; require fresh eligible reacquisition or evidenced changed encounter/approach'})
            if obligation and not acquisition and not probe:withheld.append({'action':'attack','reason':obligation})
            if exhausted and not acquisition and not probe:withheld.append({'action':'attack','reason':'correction methods exhausted; clear/reacquire or change search strategy'})
            if not deferred(c,target,frame,measurement) and not target['self_target'] and not target['invalid_text'] and visual_suitable and (not target['levels'] or len(target['levels'])==1 and low<=target['levels'][0]<=high):
                if not c.config.get('committed_combat',False):
                    for action in (('forward','turn_left','turn_right','backward','strafe_left','strafe_right') if obligation else ('forward','turn_left','turn_right')):
                        if renewable or h.motion_count(action,purpose,history['key'])<2:
                            description=('Move forward 1.0 seconds toward the selected creature on safe ground.' if action=='forward' else
                                f'One short safe {action} adjustment toward the selected creature.')
                            await motor(action,action,description,'motion',purpose,c.travel_guard(frame,target,selected=True),inspect_living=True,renew_attempt=renewable)
                else:
                    # Moving during an ordinary living encounter interrupts casts.
                    # Accepted ineffective outcomes also need a bounded exit;
                    # they do not establish any particular error cause.
                    error=h.cast_error if same and h.cast_error and h.cast_error.get('status')=='active' else None
                    correction={'range':RANGE_APPROACH_ACTIONS,'facing':('turn_left','turn_right'),
                        'los':('strafe_left','strafe_right','backward')}.get((error or {}).get('kind'),())
                    specific_cast_error=bool(correction)
                    if ineffective:correction=RANGE_APPROACH_ACTIONS
                    for action in correction:
                        if h.motion_count(action,'cast_correction',history['key'])<2:
                            description=(('Move forward 1.0 seconds on visible clear ground.' if action=='forward' else
                                f'Turn {action.split("_")[1]} {c.config["turn_seconds"]:.2f} seconds to change our facing/view.' if action.startswith('turn_') else
                                f'Sidestep {action.split("_")[1]} 0.15 seconds on visible clear ground.' if action.startswith('strafe_') else
                                'Move backward 0.15 seconds on visible clear ground.') if action_correction or ineffective else
                                range_approach_description(action) if error['kind']=='range' else
                                f'One short safe {action} correction for the confirmed {error["kind"]} error.')
                            owner=history['key']
                            allowance=lambda action=action,owner=owner: (owner==h.combat_history_key
                                and owner==(h.approach or {}).get('history_key')
                                and h.motion_count(action,'cast_correction',owner)<2
                                and (not ineffective or recovery_candidate(c,frame,target,pending,stage)==ineffective))
                            await motor(action,action,description,'motion','cast_correction',
                                c.travel_guard(frame,target,selected=True),allowance=allowance,
                                inspect_living=True,renew_attempt=renewable,ineffective_cast_recovery=ineffective)
                        else:
                            withheld.append({'action':action,'reason':'two ineffective or unassessed corrections for this selected target'})
                    if (error and error['kind']=='facing'
                        and all(h.motion_count(action,'cast_correction',history['key'])>=2
                            for action in ('turn_left','turn_right'))
                        and h.motion_count('turn_around','cast_correction',history['key'])<1):
                        # Reuse the general gameplay motor's broad stationary
                        # inspection. Two tiny turns need not bring a rear
                        # target into view. This is a different physical method,
                        # offered once; short-turn and rejection debt survive.
                        await motor('turn_around','turn_right',
                            'The short facing corrections did not resolve this encounter. Make one broad stationary turn for 1.5 seconds to bring the selected creature into view, then inspect the linked before/after. This is a bounded turn, not a promised angle or proof of corrected facing; no cast until improvement is observed.',
                            'motion','cast_correction',c.travel_guard(frame,target,selected=True),
                            seconds=1.5,action='turn_around',inspect_living=True)
                if permission and (not exhausted or acquisition or probe):
                    await attacks(renewable=renewable,same=same,reassess_rejection=reassessment,acquisition=acquisition,mixed_probe=probe)
                if obligation and h.cast_error and h.cast_error['kind'] in {'cooldown','interrupted'} and debt<2:
                    observe('cast_ready','The interrupted/cooldown cast is now visibly ready; allow one fresh bounded probe, no damage claim.',ready=True)
        visual=target.get('visual_observation')
        if not visual or visual['selected_hud']!='present':
            observe('no_selected_frame','No actual selected portrait with attached health/level unit HUD is visible in the current image. A present frame with unreadable eligibility is cannot_assess, not this answer. No input; acquire.',absence=True,effect='target_absent' if pending and pending['family']=='target' else None)
        else:withheld.append({'action':'no_selected_frame','reason':'fresh attributed observation sees a present target HUD'})
        # Clearing an unsuitable unit before every Tab can repeatedly acquire
        # that same unit. Preserve the selection while Sage cycles once; the
        # ordinary target receipt then owns inspection of whatever Tab selects.
        def next_target_available():
            observed=target.get('visual_observation') or {}
            provenance=target.get('visual_provenance') or {}
            return bool(valid() and c.current() and not h.active_threat
                and h.pending is pending and (pending is None or
                    pending.get('family')=='target' and h.known_completed_input(pending))
                and observed.get('selected_hud')=='present'
                and observed.get('target_kind')=='creature' and observed.get('life_state')=='alive'
                and provenance.get('continuity_frame_id',provenance.get('frame_id'))==frame.frame_id
                and target.get('name') and not target.get('self_target') and not target.get('invalid_text')
                and selected_policy(c,target,frame,measurement)['kind']=='unsupported_species'
                and deferred(c,target,frame,measurement))
        if next_target_available() and h.target_selection_available():
            await motor('select_next_target','target_enemy',
                'This selected creature is outside the configured hunting species. Press Tab once while keeping it selected to inspect the next nearby enemy; clearing first may select the same unsuitable creature again. Do not attack from this choice. Inspect the resulting current portrait before any attack.',
                'target','target',await c.clear_guard(frame,target),allowance=next_target_available)
        await motor('reject_selected_target','escape',('Clear this selected target to leave the unresolved approach and hunt another; verify the clear before selecting again.' if action_exit or action_correction or ineffective else
            ('The selected eligible creature has unknown primary-ground membership. Deliberately clear this selection once and verify absence. Missing location does not make this creature unsuitable.' if selected_policy(c,target,frame,measurement)['kind']=='ground_unknown' and target.get('eligibility')=='eligible' else 'An actual selected portrait/bar HUD frame IS visibly present, and THAT selected unit is dead, friendly, player, outside the allowed band or unsuitable. Clear once and verify absence. Do not choose for ground corpses/world names or an empty HUD region; those require no_selected_frame.'))
            +(' '+selected_context(selected_policy(c,target,frame,measurement)) if deferred(c,target,frame,measurement) else '')
            +(' This confirmed out-of-band creature will be retained as a local scouting sighting; then plan another visible search approach.' if scout else ''),
            'clear','clear',await c.clear_guard(frame,target),
            reject=not (selected_policy(c,target,frame,measurement)['kind']=='ground_unknown' and target.get('eligibility')=='eligible'),abandon=bool(history and history['cast_attempted']),out_of_band_scout=scout,progression_reject=selected_policy(c,target,frame,measurement)['kind'] in {'outside_primary_region','unsupported_species'} and deferred(c,target,frame,measurement))
        if (selected_policy(c,target,frame,measurement)['kind']=='ground_unknown' or h.unclear>=2 or stage=='reinspect' or
            level in h.target_bands_by_player_level and len(target['levels'])!=1) and not (review_inspected or review_needed or ineffective or action_correction or action_exit):
            observe('reinspect_selected_frame','Inspect a fresh magnified selected-target HUD crop because presence or eligibility was unresolved. No physical input.',reinspect=True,cast_reinspect=review_needed)
        if action_correction or ineffective or h.recovery_requested or history and (history['rejections'] or history['correction_rounds']>=3):
            observe('change_search_strategy','Current engagement is unresolved or unsuitable; inspect another visible/attributed patch while retaining unknown effects and failed corrections.',strategy=True)
    # A specific attributable error supersedes generic unchanged-cast
    # diagnostics, even after its methods are exhausted. Offering aliases here
    # would both duplicate the question and bypass per-method correction debt.
    if action_exit:
        removed=[option.option for option in options if meta.get(option.option,{}).get('family') in {'motion','combat'}]
        options=[option for option in options if option.option not in removed]
        for name in removed:
            meta.pop(name,None);withheld.append({'action':name,'reason':'encounter correction limit exhausted; choose clear or relocation'})
    if review_needed and (specific_cast_error or error_task or ineffective or action_exit):
        withheld.append({'action':'generic_cast_diagnostics',
            'reason':'the attributable cast error already defines the correction and its retained method history'})
    if review_needed and not (specific_cast_error or error_task or ineffective or action_exit) and stage in {'feedback','inspect','reinspect'}:
        question='Completed cast inputs repeatedly left target health and own mana unchanged. Choose a visibly supported correction or leave this selected encounter before casting again. These observations do not establish damage or kill credit.'
        if 'change_search_strategy' not in meta:
            observe('change_search_strategy','The repeated cast observations remain unresolved. Assess another visible or attributed patch while retaining unknown cast effects and failed methods.',strategy=True)
        if 'reject_selected_target' not in meta:
            await motor('reject_selected_target','escape','Leave this unresolved selected encounter, then verify the clear. Prior cast effects remain unknown.',
                'clear','clear',await c.clear_guard(frame,target),abandon=bool((h.target_history.get(h.combat_history_key) or {}).get('cast_attempted')))
        await motor('approach_for_range_check','forward','The selected living creature is visibly too far for a plausible Smite. Move forward briefly toward it on safe ground to correct that visible range problem; choose only if distance is the supported cause.','motion','cast_correction',c.travel_guard(frame,target,selected=True),seconds=.5,inspect_living=True,diagnostic_correction=True)
        await motor('stand_for_cast','forward','Our player is visibly sitting or kneeling and cannot cast from that stance. One brief safe movement pulse stands us up; choose only if that stance is visibly supported.','motion','standing',c.travel_guard(frame,target,selected=True),seconds=.08,inspect_living=True,diagnostic_correction=True)
    if mixed and stage in {'inspect','reinspect','recovery','planning'}:
        observe('reassess_cast_ready','The retained BEFORE cast and current selected living HUD show a health decrease despite the recorded error. Inspect the current creature and facing/range: one fresh guarded Smite probe is justified. This does not prove our damage, erase the error, or reset failed corrections.',mixed_assessment=True,probe_ready=True)
        observe('cast_error_persists','The target health decreased, but the current stance/range/facing still does not justify a cast probe. Retain the error and failed methods; correct or explicitly disengage.',mixed_assessment=True)
    if (malformed_name and stage in {'feedback','inspect','reinspect'} and 'reinspect_selected_frame' not in meta
        and not (review_needed or ineffective or action_correction or action_exit)):
        observe('reinspect_selected_frame','The observed target name cannot form a valid guarded input binding. Reinspect the current selected HUD on a fresh frame without guessing or altering the text.',reinspect=True)
    if not (action_search or action_planning or action_correction or action_exit):
        observe('ui_blocked','An active typing field or blocking modal/menu currently prevents world input; passive chat/HUD does not.',ui=True)
        observe('recover_now','Own death, immediate survival/resource emergency or unexpected threat requires interruption. Ordinary selected creature attacking us is normal combat.',recover=True)
        observe('dead_or_unrecoverable','Our own player is visibly dead; stop and release, no corpse recovery.',stop=True)
        if stage!='feedback' and not (review_needed or error_task or ineffective or action_correction or action_exit):
            observe('cannot_assess','Current evidence is insufficient for this question. No input, no invented outcome.',unclear=True)
    # Exhausted sensing of an earlier selection is not exhausted local search.
    # A current concrete acquisition/planning menu already owns its exits.
    inspection_handoff = stage not in {'clear','feedback'} and inspection.selected_unavailable(c,frame,target) and not (action_search or action_planning or action_correction or action_exit or error_task)
    if inspection_handoff and 'change_search_strategy' not in meta:
        observe('change_search_strategy','Target inspection remains unresolved after its bounded attempts. Assess another visible or attributed search approach; retain unknown effects. This does not attack or claim a target was absent.',strategy=True)
    if inspection_handoff and 'change_search_strategy' in meta:
        meta['change_search_strategy']['inspection_handoff']=True
    if h.active_threat and not disengaging(c):
        options=[option for option in options if not meta.get(option.option,{}).get('strategy')]
        meta.pop('change_search_strategy',None)
        if stage not in {'feedback','clear'}:
            observe('disengage_threat','We are still under a current threat. Deliberately attempt to escape along a visible safe path instead of attacking. This is an escape intent, not proof the attacker stopped; failed methods and unfinished combat stay recorded.',disengage_threat=True)
    handoff_episode=inspection.current_work(c)
    handoff_scope=(c.cycle.session_epoch,c.cycle.input_generation)
    def handoff_valid():
        return (valid() and inspection.current_work(c) is handoff_episode
            and handoff_episode is not None and inspection.selected_unavailable(c,frame,target)
            and (c.cycle.session_epoch,c.cycle.input_generation)==handoff_scope
            and h.pending is pending and (pending is None or h.known_completed_input(pending)))
    if meta.get('change_search_strategy',{}).get('inspection_handoff'):
        options=[replace(option,precondition=handoff_valid) if option.option=='change_search_strategy' else option
            for option in options]
    panels=[]
    evidence=pending if pending and linked else (h.cast_error or {}).get('assessment_source') if mixed or probe else recent if selection_missing else None
    if evidence and evidence.get('source_image') and Path(evidence['source_image']).is_file():
        label=('BEFORE CLEAR — HISTORICAL REJECTED SELECTION' if evidence.get('family')=='clear' else
            'BEFORE TARGET SELECTION — NOT AN ATTACK' if evidence.get('family')=='target' else
            'BEFORE RECENT COMBAT — UNKNOWN DAMAGE UNTIL OBSERVED')
        panels.append((evidence['source_image'],evidence['source_hash'],label,36))
    guard_frame=(recent or {}).get('last_guard_frame')
    if stage!='clear' and selection_missing and guard_frame and Path(guard_frame['image_path']).is_file():
        panels.append((guard_frame['image_path'],guard_frame['sha256'],'RETAINED CAST GUARD FRAME — HISTORICAL, INSPECT VISIBLE CORPSE/XP',36))
    error_evidence=(pending or {}).get('position_error_evidence') or (evidence or {}).get('position_error_evidence')
    error_frame=(error_evidence or {}).get('frame') or {}
    error_path=error_frame.get('image_path')
    if stage!='clear' and error_path and Path(error_path).is_file() and not any(panel[0]==error_path for panel in panels):
        panels.append((error_path,error_evidence['sha256'],'RETAINED CAST ERROR — ATTRIBUTED TO THIS RECEIPT',36))
    if action_search or action_planning:panels=[]
    panels=tuple(panels)
    image=await c.prepare_image(frame,composed_evidence_image,frame,target['box'],
        Path(frame.image_path).with_name(f'grind-view-{frame.frame_id}.png'),history=panels,
        alternate=stage in {'reinspect','recovery'},player_badge=tuple(c.config['player_level_box']),
        crop_label='CALIBRATED TARGET HUD REGION — MAY BE EMPTY',
        omit_target=action_search or stage in {'acquire','planning'} or h.selected_presence is False and stage not in {'inspect','reinspect','recovery','clear'})
    if isinstance(image,CycleResult):return image
    context=(exit_context(c,target) if action_exit else action_context(target) if ineffective else correction_action_context(c,target,withheld) if action_correction else acquisition_context(c,level) if action_search else
        compact_context(level,target,stage,h.compact_last_result,preference=h.target_preference(level)))+inventory_context(c)
    if stage in {'acquire','planning','recovery'} or action_search:
        from sage_wow.agent.grind_progression import region_context
        context+=region_context(c,frame,measurement)
    if target.get('name') and (target.get('visual_observation') or {}).get('selected_hud')=='present':
        context+=' '+selected_context(selected_policy(c,target,frame,measurement))
    if stage in {'acquire','planning','recovery'} and h.selected_presence is False and h.confirmed_target_absences:
        context+=(f' Earlier completed selections followed by no selected enemy: {h.confirmed_target_absences}. '
            'Choose the next offered action from the CURRENT world view.')
    if stage=='planning' and h.strategy_required:
        context+=(' The requested strategy change has not reached a different observed search sector. '
            'Choose a destination or visible exploration path to assess actual movement. Movement and casting outcomes do not establish target-selection absence. '
            f'Current cast constraint: {h.cast_obligation or "none"}. Failed action counts: {h.failures}.')
        if h.scouting_observations:context+=' Recent local scouting sightings (not a population census): '+str(h.scouting_observations[-3:])
    if selected_living_bar:context+=' The current selected creature has a positive calibrated health bar. A nearby corpse or older same-name death log does not establish death of this selected creature.'
    if stage!='clear' and not action_search and getattr(c,'combat_log_context',None):context+=' Recent official combat log facts: '+str(c.combat_log_context)
    if mixed or probe:context+=' Mixed cast evidence to reassess, never automatic damage/facing authority: '+str(mixed or probe)
    if h.active_threat:context+=' Current incoming threat evidence: '+str(h.active_threat)+'. A failed target clear is not disengagement. Keep fighting/healing/correcting, or explicitly choose disengage_threat.'
    if stage=='recovery' and not action_search:context+=' Recovery reason: '+str((h.recovery_requested or {}).get('reason','unresolved local evidence'))+'. No old outcome becomes a success.'
    c.event('grind_compact_question',{'stage':stage,'question':question,'options':[x.option for x in options],
        'withheld':withheld,'search_revision':h.search_revision,'recovery':h.recovery_requested,
        'concrete_action_menu':action_search or action_planning or action_correction or action_exit})
    if len(options)<2:
        # No invented physical fallback or duplicate alias to satisfy the API.
        if (action_search or action_correction or action_exit) and any(x.option=='change_search_strategy' for x in options):
            h.phase='choose_area';h.planning_requested=True
            fact={'reason':'exhausted_local_actions','frame_id':frame.frame_id,
                'captured_at':frame.captured_at,'search_revision':h.search_revision,
                'action_authority':False,'progress_credit':False}
            if not h.strategy_required:h.strategy_required=dict(fact)
            c.event('grind_search_actions_exhausted',fact)
            return CycleResult('grind_reobserve',detail='Local actions exhausted; choose a destination on the next fresh frame')
        c.enter_blocked('blocked_no_available_actions',c.last_signature)
        return CycleResult('grind_blocked',detail='No complete action menu; retain failed-method history')
    if boundary:
        context += ' This is a current selected-HUD factual assessment after interrupted sensing. Unknown remains unknown; it grants no input or observer budget.'
        options=[option for option in options if option.option in {'no_selected_frame','cannot_assess','ui_blocked','recover_now','dead_or_unrecoverable'}]
        options=[replace(option,precondition=(lambda previous=option.precondition: inspection.boundary_current(c,boundary) and (previous is None or previous()))) for option in options]
    absence_pin=inspection.assessment_pin(c,frame,target) if any(meta.get(option.option,{}).get('absence') for option in options) else None
    probe_pin=deepcopy(probe)
    probe_error_pin=deepcopy(h.cast_error) if probe else None
    result=await c.decide(frame,image,context,question,options,travel_budget=c.travel_budget,compact=True,sensing_boundary=boundary)
    receipt=result.receipt or {};chosen=getattr(result.decision,'chosen',None);data=meta.get(chosen,{})
    accepted=result.status=='dispatched' and receipt==c.cycle.last_receipt and receipt.get('completed') and not receipt.get('dispatch_unknown') and not receipt.get('error')
    if boundary and not inspection.boundary_current(c,boundary):
        inspection.finish_boundary(c,boundary,result,{'unclear':True,'discarded':True})
        c.event('grind_interrupted_boundary_discarded', {'request_id':boundary.get('request_id'),'reason':'source_task_superseded'})
        return CycleResult('grind_reobserve',detail='Interrupted absence source changed; admitted debt retained')
    if accepted and data.get('absence') and not data.get('cleared') and absence_pin and not inspection.assessment_current(c,absence_pin):
        c.event('grind_target_absence_assessment_discarded', {'request_id':receipt.get('request_id'),'reason':'current_source_changed'})
        if boundary:inspection.finish_boundary(c,boundary,result,{'unclear':True,'discarded':True})
        return CycleResult('grind_reobserve',detail='Current absence assessment authority changed')
    if accepted and data.get('mixed_assessment') and reassessment_candidate(c,frame,target)!=mixed:
        return CycleResult('grind_reobserve',detail='Mixed cast reassessment scope changed; constraint retained')
    if accepted and data.get('inspection_handoff') and not handoff_valid():
        c.event('grind_target_inspection_handoff_rejected',{'reason':'inspection episode or pending authority changed'})
        return CycleResult('grind_reobserve',detail='Inspection handoff authority changed; pending effects retained')
    if receipt.get('possible_input') and not accepted:c.stop('partial_or_unknown_grind_input')
    elif accepted:
        from sage_wow.agent.grind_progression import accepted_placement
        if data.get('family')=='combat':accepted_placement(c,target,receipt,data.get('dispatch_policy'),authenticate=True)
        if receipt.get('possible_input'):
            loot_budget.finish_handoff(c)
        if data.get('mixed_assessment'):
            record_reassessment(c,mixed,frame,result.decision.envelope.request_id,ready=data.get('probe_ready',False))
        if data.get('mixed_probe'):
            consume_probe(c,probe_pin,receipt,frame,probe_error_pin)
        if data.get('reassess_rejection'):
            proof=data['reassess_rejection'];history=h.target_history[proof['history_key']]
            history['rejection_reassessment']={**proof,'cast_receipt_id':receipt['receipt_id']}
            c.event('grind_rejection_reassessed',history['rejection_reassessment'])
        if data.get('family')=='combat':h.retain_burst_guard_evidence(receipt)
        # A selected name proposal is fallible, but contradicting absence must
        # be reconciled before charging the Tab as a failed selection. The
        # linked receipt survives this changed-perception request.
        visual=target.get('visual_observation')
        presence_conflict=bool(data.get('absence') and (
            absence_conflicts(target)
            or data.get('cleared') and h.selection_absence_conflict(target)
            or travel_scout.active(c) and travel_scout.absence_conflicts(target)))
        if presence_conflict:
            data={'reinspect':True,'presence_conflict':True,'unclear':True}
            h.compact_stage='reinspect';h.selected_presence=None
            c.event('grind_target_presence_conflict',{'frame_id':frame.frame_id,
                'name_proposal':target['name'],'visual_observation':visual,
                'request_id':result.decision.envelope.request_id,'next_action':inspection.availability(c,frame,target)['state'],
                'sensing_availability':inspection.availability(c,frame,target)})
        elif data.get('reject_error'):
            pending['rejected_error_cues']=list(dict.fromkeys(pending['rejected_error_cues']+data['reject_error']));pending.pop('position_error_evidence',None)
        else:
            if data.get('suspect_corpse'):
                h.archive_pending('sage_requested_recent_corpse_inspection_unknown_damage');pending=None;linked=False
                recent=h.recent_combat
                recent['handoff_requested']=True
                request={'suspected':True,'death_observed':False,'credit_known':False,
                    'target':dict(recent.get('target') or {}),'frame_id':frame.frame_id,'image_path':frame.image_path,
                    'encounter_id':recent.get('encounter_id'),'receipt_id':recent['receipt']['receipt_id'],
                    'combat_source_frame_id':recent.get('source_frame_id'),'combat_source_image':recent.get('source_image'),
                    'last_guard_frame':recent.get('last_guard_frame'),'request_id':result.decision.envelope.request_id}
                h.loot_request=request if loot_budget.request_allowed(c,request) else None
                h.compact_stage='loot' if h.loot_request else 'acquire'
                h.selected_presence=False;h.phase='search';h.encounter_ended=True
                h.recovery_requested=None
                c.event('grind_loot_requested',h.loot_request)
            if data.get('inspect_living'):
                if pending and pending['family']=='target' and linked:
                    h.resolve('target_acquired',frame,measurement,pending=pending,linked=True);pending=None;linked=False
                if data.get('acquisition'):
                    from sage_wow.agent.grind_acquisition import reassess_acquisition
                    reassess_acquisition(c,target,data['acquisition'])
                elif data.get('renew_attempt') and h.pixel_renewal_allowed(target):h.renew_attempt(target)
                h.selected_presence=True;h.compact_stage='inspect'
                if data.get('family')=='motion':h.phase='approach'
                h.eligible_inspection={'frame_id':frame.frame_id,'image_path':frame.image_path,'name_proposal':target['name'],'level_proposals':target['levels'],'request_id':result.decision.envelope.request_id}
            if data.get('own_damage'):
                h.own_damage_evidence={'frame_id':frame.frame_id,'before_frame_id':pending['source_frame_id'],'source_image':pending['source_image'],'receipt_id':pending['receipt']['receipt_id'],'encounter_id':h.encounter,'request_id':result.decision.envelope.request_id}
                c.event('grind_own_damage_observed',h.own_damage_evidence)
            if data.get('credit'):
                h.credited_kills.append({'encounter_id':h.encounter,'death_frame_id':frame.frame_id,'cast_receipt_id':pending['receipt']['receipt_id'],'eligible_selection':dict(h.eligible_inspection or {}),'own_damage':dict(h.own_damage_evidence),'request_id':result.decision.envelope.request_id,'exclusive_solo_known':False})
                c.event('grind_credited_kill',h.credited_kills[-1])
            if data.get('no_effect'):
                h.no_effect_retries+=1
            if data.get('lost'):h.archive_pending('selection_lost_unknown');pending=None;linked=False
            if data.get('inspection_handoff'):
                from sage_wow.agent.grind_relocation import capture_selected_task
                data['selected_disposition']=capture_selected_task(c,frame,target,pending,result)
                if data['selected_disposition']:
                    h.archive_pending('selected_target_sensing_unavailable');pending=None;linked=False
            if chosen in scout_states:
                await travel_scout.accept(c,result,scout_states[chosen],level)
            elif data.get('local_wait'):
                import time
                c.wait_until=time.time()+5
                c.event('grind_local_wait',{'frame_id':frame.frame_id,'seconds':5,'area':c.config.get('fixed_hunting_area'),'progress_credit':False})
            else:
                await c.apply_choice(frame,target,measurement,pending,linked,result,{**data,'scoped_question':True},chosen,level)
            if data.get('ineffective_cast_recovery') and h.pending and h.pending['receipt']['receipt_id']==receipt['receipt_id']:
                h.pending['ineffective_cast_recovery']=dict(data['ineffective_cast_recovery'])
            if data.get('family')=='combat' and any(x.get('dispatched') for x in (receipt.get('execution') or {}).get('opening_commands',[])):
                if h.approach:h.target_history[h.approach['history_key']]['auto_attack_started']=True
            if data.get('cast_reinspect') and current_review():
                review['inspection']={'identity':review_identity,'frame_id':frame.frame_id,
                    'request_id':result.decision.envelope.request_id,'receipt_id':receipt['receipt_id']}
                c.target_observation=None
            if data.get('diagnostic_correction') and h.cast_review:
                h.cast_review['unchanged_resource_casts']=0
                h.cast_review['reason']='Sage chose a visibly supported correction; reassess its effect before a new cast'
            if data.get('continue_living'):
                # The prior completed input's damage is unknown. This new Sage
                # attack choice confirms current life without inventing damage.
                h.cast_obligation=None;h.remember_approach()
            if data.get('no_effect') and h.no_effect_retries==1:
                h.cast_obligation=None;h.retry_credit=True;h.remember_approach()
            if data.get('death') or data.get('lost') or data.get('absence'):
                h.compact_stage='acquire';h.selected_presence=False
            if data.get('death'):
                if effective_loot_enabled(c):
                    if h.recent_combat:h.recent_combat['handoff_requested']=True
                    request={'death_observed':True,'suspected':False,'target':{k:(data.get('recent_target') or target).get(k) for k in ('name','levels','box','badge','visual_observation')},'frame_id':frame.frame_id,
                        'image_path':frame.image_path,'encounter_id':h.encounter,'receipt_id':(pending or {}).get('receipt',{}).get('receipt_id'),
                        'credit_known':bool(data.get('credit')),'request_id':result.decision.envelope.request_id}
                    h.loot_request=request if loot_budget.request_allowed(c,request) else None
                    h.compact_stage='loot' if h.loot_request else 'acquire'
                    c.event('grind_loot_requested',h.loot_request)
                import time
                if time.time()-c.level.last_attempt_at>=30:c.level.last_attempt_at=0
            if data.get('reinspect'):
                h.compact_stage='reinspect';h.selected_presence=None
                if data.get('factual_reinspect'):
                    c.target_reinspection_operation={'episode_id':(c.target_inspection_episode or {}).get('id'),
                        'request_id':receipt['request_id'],'state':'committed',
                        'scope':inspection.scope(c,frame),'generation':c.cycle.input_generation,
                        'configuration':inspection.configuration(c),'controller_revision':c.revision,
                        'target_continuity':h.target_continuity,
                        'source_image':frame.image_path,
                        'source_sha256':hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()}
            if data.get('absence') or data.get('lost') and not target['name']:
                h.target_continuity+=1
                h.attempt_absence=True
            if data.get('family')=='target':h.compact_stage='inspect';h.selected_presence=None
            if data.get('damage') or data.get('credit'):
                h.no_progress_at=h.active_seconds;h.recovery_requested=None
            if data.get('cast_error') and data['cast_error']['kind'] in {'range','facing','los','cooldown','interrupted','standing','resist','evade'}:
                h.compact_stage='inspect';h.phase='approach';h.remember_approach()
            h.compact_last_result=chosen
            travel_scout.finish_assessment(c,frame,target,data)
            from sage_wow.agent.grind_navigation_recovery import finish_assessment
            finish_assessment(c,frame,target,data)
    if accepted and data.get('family')=='target':inspection.attach_witness(c,tab_witness['value'],frame,result)
    if boundary:inspection.finish_boundary(c,boundary,result,data)
    else:c.finish_question(result,data)
    c.flush_outcomes()
    c.store.save_checkpoint('grind_only',{'hunt':h.context(),'deadline':c.deadline,'baseline_verified':c.baseline,'level':level})
    return result
