"""Retry the chosen travel question after abstention, without new input authority."""
from copy import deepcopy


def basis(c):
    h=c.hunt
    return {'plan':deepcopy(h.plan),'strategy':deepcopy(h.strategy_required),
        'session_epoch':c.cycle.session_epoch,'input_generation':c.cycle.input_generation,
        'target_continuity':h.target_continuity,'search_revision':h.search_revision,
        'encounter_id':h.encounter,'owner':h.combat_history_key}


def ready(c, frame, target):
    h=c.hunt;hud=target.get('hud') or {};visual=target.get('visual_observation') or {}
    from sage_wow.agent.grind_relocation import selected_disposition
    disposed=selected_disposition(c,frame,target,h.pending)
    matching=bool(disposed and disposed['status']=='matching'
        and (h.plan or {}).get('relocation_request_id')==(h.strategy_required or {}).get('selected_exit',{}).get('request_id'))
    return bool(h.phase=='travel' and h.plan and h.plan.get('request_id')
        and c.current() and c.fresh(frame) and not c.cycle._scope_error(frame)
        and not c.require_world and not h.blocked and not h.input_effect_unverified
        and h.pending is None and not h.active_threat and not c.heal_pending
        and not h.no_mana and not h.loot_request and not h.disengagement
        # No absence claim: movement retires the old observer. Current positive
        # cues still preempt this ordinary task, as they do idle acquisition.
        and (matching or not target.get('name') and not target.get('levels')
        and not target.get('self_target') and not target.get('invalid_text')
        and not target.get('observation_conflict') and visual.get('selected_hud')!='present'
        and not visual.get('name') and visual.get('level') is None
        and not (hud.get('target_health') is not None and (hud.get('target_health_confidence') or 0)>=.8))
        and hud.get('frame_id')==frame.frame_id and hud.get('player_health') is not None
        and hud['player_health']>max(c.config['critical_health_threshold'],c.config['heal_health_fraction'])
        and (hud.get('health_confidence') or 0)>=.8)


def note(c, frame, target, result, offered_basis):
    h=c.hunt
    if (not result.no_input_abstention or c.null_answers<2 or not h.recovery_requested
            or offered_basis!=basis(c) or not ready(c,frame,target)):
        return
    h.recovery_requested['travel_task']={'basis':offered_basis,'frame_id':frame.frame_id,
        'request_id':result.decision.envelope.request_id,
        'claim':'Retry the same chosen travel question; no input, movement or progress authority'}


def current(c):
    recovery=c.hunt.recovery_requested or {};marker=recovery.get('travel_task') or {}
    return bool(c.null_answers>=2 and c.hunt.phase=='travel' and marker
        and recovery.get('latest_reason',recovery.get('reason'))=='repeated_null_provider_answer'
        and marker['basis']==basis(c))


def resume(c, frame, target, pending, phase):
    h=c.hunt
    if (phase!='travel' or h.compact_stage!='recovery' or pending is not None
            or not current(c) or not ready(c,frame,target)):
        return False
    h.compact_stage='acquire'
    c.event('grind_travel_question_resumed',{'frame_id':frame.frame_id,
        'plan_request_id':h.plan['request_id'],
        'abstained_request_id':h.recovery_requested['travel_task']['request_id'],
        'input_authority':False,'progress_credit':False})
    return True
