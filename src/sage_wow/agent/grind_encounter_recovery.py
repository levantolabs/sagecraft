"""Receipt-scoped mixed cast review and fresh-threat task routing.

These observations select questions, never input or damage/kill credit. Native
log names/GUIDs are not current selected-target identity and cannot grant a cast.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
from pathlib import Path
import time
from sage_wow.agent.grind_search import target_identity


def capture_scope(c, frame):
    return {'session_epoch': c.cycle.session_epoch, 'source': frame.source,
            'width': frame.width, 'height': frame.height}


def retain_error_source(c, pending):
    """Keep the completed cast after its original feedback question closes."""
    if pending.get('family') != 'combat' or not c.hunt.known_completed_input(pending):
        return None
    return deepcopy({key: pending.get(key) for key in (
        'family', 'source_frame_id', 'source_image', 'source_hash', 'source_scope',
        'measurement', 'receipt', 'target', 'target_history_key',
        'encounter_id', 'selection_revision', 'target_continuity', 'position_error_evidence',
        'rejected_error_cues', 'assessed_error_cues')})


def mixed_cast_evidence(c, frame, target):
    """A lower living bar conflicts with 'entire cast ineffective', not facing.

    Only continuous local acquisition and retained calibrated image evidence
    can open this review. Text/native log claims alone deliberately cannot.
    """
    h = c.hunt
    error = h.cast_error or {}
    source = error.get('assessment_source') or {}
    receipt = source.get('receipt') or {}
    visual = target.get('visual_observation') or {}
    hud = target.get('hud') or {}
    before = (source.get('measurement') or {}).get('combat_hud') or {}
    owner = source.get('target_history_key')
    history = h.target_history.get(owner, {})
    if (error.get('status') != 'active' or error.get('kind') not in {'range', 'facing', 'los', 'standing'}
            or h.blocked or h.input_effect_unverified or not c.fresh(frame)
            or c.cycle._scope_error(frame) or h.pending is not None
            or not h.known_completed_input(source) or not receipt.get('possible_input')
            or receipt.get('receipt_id') != error.get('cast_receipt_id')
            or source.get('source_scope') != capture_scope(c, frame)
            or receipt.get('session_epoch') != c.cycle.session_epoch
            or source.get('target_continuity') != h.target_continuity
            or source.get('encounter_id') != h.encounter
            or not owner or owner != h.combat_history_key
            or owner != (h.approach or {}).get('history_key')
            or history.get('completed') or history.get('retired_by_acquisition')
            or target_identity(target.get('name')) != target_identity((source.get('target') or {}).get('name'))
            or target.get('self_target') or target.get('invalid_text')
            or visual.get('selected_hud') != 'present' or visual.get('life_state') != 'alive'
            or target.get('eligibility') != 'eligible'
            or hud.get('frame_id') != frame.frame_id
            or before.get('frame_id') != source.get('source_frame_id')
            or frame.frame_id == source.get('source_frame_id')):
        return None
    bh, ah = before.get('target_health'), hud.get('target_health')
    if (bh is None or ah is None or not 0 < ah < bh - .025
            or min(before.get('target_health_confidence') or 0,
                   hud.get('target_health_confidence') or 0) < .8):
        return None
    try:
        if hashlib.sha256(Path(source['source_image']).read_bytes()).hexdigest() != source['source_hash']:
            return None
        if datetime.fromisoformat(frame.captured_at) <= datetime.fromisoformat(receipt['occurred_at']):
            return None
    except (OSError, KeyError, TypeError, ValueError):
        return None
    return {'cast_receipt_id': receipt['receipt_id'], 'history_key': owner,
            'encounter_id': h.encounter, 'selection_revision': h.selection_revision,
            'target_continuity':h.target_continuity,
            'scope': capture_scope(c, frame), 'input_generation': c.cycle.input_generation,
            'source_frame_id': source['source_frame_id'], 'frame_id': frame.frame_id,
            'before_health': bh, 'current_health': ah,
            'claim': 'Selected health decreased across a completed cast; own damage and current facing remain unproven'}


def reassessment_candidate(c, frame, target):
    if (c.hunt.cast_error or {}).get('reassessment'):
        return None  # One answer/probe per errored receipt, not per new frame.
    return mixed_cast_evidence(c, frame, target)


def probe_credit(c, frame, target):
    review = (c.hunt.cast_error or {}).get('reassessment') or {}
    evidence = mixed_cast_evidence(c, frame, target)
    if review.get('status') != 'ready' or not evidence:
        return None
    if any(review.get(key) != evidence.get(key) for key in (
            'cast_receipt_id', 'history_key', 'encounter_id', 'selection_revision',
            'target_continuity', 'scope', 'input_generation', 'source_frame_id')):
        return None
    return review


def record_reassessment(c, proof, frame, request_id, *, ready):
    error = c.hunt.cast_error
    error['reassessment'] = {**proof, 'status': 'ready' if ready else 'constraint_retained',
                            'assessed_frame_id': frame.frame_id, 'request_id': request_id}
    if ready:
        # Choosing a current combat probe also ends an earlier planning/escape
        # intent. Otherwise the permission would be stranded in a travel menu.
        c.hunt.phase = 'fight'
        c.hunt.compact_stage = 'inspect'
        c.hunt.planning_requested = False
        c.hunt.disengagement = None
        if c.hunt.strategy_required:c.hunt.strategy_required.pop('selected_exit',None)
    c.event('grind_cast_reassessment', dict(error['reassessment']))


def completed_smite_submission(receipt):
    """A completed bookkeeping receipt or start-attack is not a Smite attempt."""
    binding=receipt.get('selected_binding') or {};execution=receipt.get('execution') or {}
    command='/cast [harm,nodead] Smite'
    if (binding.get('type')!='cast_guarded' or binding.get('spell')!='Smite'
        or execution.get('kind')!='cast_guarded' or execution.get('command')!=command
        or not receipt.get('completed') or receipt.get('error') or receipt.get('dispatch_unknown')
        or not receipt.get('possible_input')):return False
    completed=execution.get('completed_steps')
    if completed is not None and (len(completed)!=1 or any(
        item.get('command')!=command or not item.get('dispatched') for item in completed)):return False
    opening=execution.get('opening_commands') or []
    if opening and (binding.get('start_attack') is not True or len(opening)!=1
        or opening[0].get('command')!='/startattack [harm,nodead]' or not opening[0].get('dispatched')):return False
    commands=[item['command'] for item in opening]+[command]
    steps=receipt.get('input_steps') or []
    if (len(steps)!=5*len(commands) or receipt.get('generation_after',0)-receipt.get('generation_before',0)!=len(steps)
        or [step.get('kind') for step in steps]!=['key_down','key_up','text','key_down','key_up']*len(commands)):return False
    for index,step in enumerate(steps):
        details=step.get('details') or {}
        if (step.get('status')!='completed' or step.get('execution_id')!=receipt.get('receipt_id')
            or step.get('input_generation')!=receipt['generation_before']+index+1):return False
        if step['kind']=='text':
            if details.get('length')!=len(commands[index//5]):return False
        elif details.get('keycode')!=36:return False
    return True


def consume_probe(c, proof, receipt, frame, original_error):
    error = c.hunt.cast_error
    source=(original_error or {}).get('assessment_source') or {}
    try:
        intact=hashlib.sha256(Path(source['source_image']).read_bytes()).hexdigest()==source['source_hash']
    except (OSError,KeyError,TypeError):intact=False
    if (error!=original_error or (error or {}).get('reassessment')!=proof
        or not intact or not c.current() or receipt.get('scope_objective_revision')!=c.revision
        or (error or {}).get('status')!='active' or proof.get('status')!='ready'
        or proof.get('cast_receipt_id')!=(error or {}).get('cast_receipt_id')
        or proof.get('history_key')!=c.hunt.combat_history_key
        or proof.get('encounter_id')!=c.hunt.encounter
        or proof.get('selection_revision')!=c.hunt.selection_revision
        or proof.get('target_continuity')!=c.hunt.target_continuity
        or proof.get('scope')!=capture_scope(c,frame)
        or receipt!=c.cycle.last_receipt or not receipt.get('completed')
        or receipt.get('error') or receipt.get('dispatch_unknown')
        or receipt.get('session_epoch')!=c.cycle.session_epoch
        or receipt.get('generation_before')!=proof.get('input_generation')
        or receipt.get('generation_after')!=c.cycle.input_generation):
        c.event('grind_cast_reassessment_discarded',{'reason':'original_probe_owner_changed'})
        return False
    error['reassessment'] = {**proof, 'status': 'consumed', 'probe_receipt_id': receipt['receipt_id']}
    submitted=completed_smite_submission(receipt)
    if submitted:
        error['status'] = 'superseded_by_guarded_probe'
        c.hunt.cast_obligation = None
    c.event('grind_cast_reassessment', {**error['reassessment'],
        'prior_error': error['kind'], 'completed_smite_submission':submitted,
        'constraint_retained':not submitted,'damage_known': False, 'failed_methods_preserved': True})
    return True


def observe_threat(c, frame, target):
    """Recent incoming attack or measured own-health loss; selection alone is not threat."""
    h = c.hunt
    if not c.fresh(frame) or c.cycle._scope_error(frame):
        return None
    now = time.time()
    scope = capture_scope(c, frame)
    observed = datetime.fromisoformat(frame.captured_at).timestamp()
    hud = target.get('hud') or {}
    hp = hud.get('player_health')
    prior = h.threat_health_observation
    evidence = None
    if (hud.get('frame_id') == frame.frame_id and hp is not None
            and (hud.get('health_confidence') or 0) >= .8):
        if (prior and prior['scope'] == scope and 0 < observed - prior['at'] <= 10
                and hp < prior['health'] - .015):
            evidence = {'kind': 'current_own_health_loss', 'occurred_at': observed,
                        'before_frame_id': prior['frame_id'], 'frame_id': frame.frame_id,
                        'before_health': prior['health'], 'health': hp,
                        'selected_attacker_identity_known': False}
        h.threat_health_observation = {'scope': scope, 'at': observed,
                                      'health': hp, 'frame_id': frame.frame_id}
    for fact in getattr(c, 'combat_log_facts', []):
        at = fact.get('occurred_at')
        if (fact.get('incoming_to_player') and not fact.get('own_source')
                and fact.get('event') in {'SPELL_DAMAGE', 'SWING_DAMAGE', 'RANGE_DAMAGE',
                    'SPELL_PERIODIC_DAMAGE', 'SPELL_MISSED', 'SWING_MISSED'}
                and isinstance(at, (int, float)) and 0 <= now - at <= 10
                and str(fact.get('source_guid', '')).startswith(('Creature-', '0x'))
                and str(fact.get('dest_guid', '')).startswith(('Player-', '0x'))
                and (not evidence or at > evidence['occurred_at'])):
            evidence = {key: fact.get(key) for key in ('event', 'occurred_at', 'read_at',
                        'source_guid', 'source_name', 'dest_guid', 'dest_name')}
            evidence.update(kind='current_incoming_attack', selected_attacker_identity_known=False)
    if evidence:
        h.active_threat = {**evidence, 'scope': scope}
    threat = h.active_threat
    if threat and (threat['scope'] != scope or not 0 <= now - threat['occurred_at'] <= 10):
        h.active_threat = None
    return h.active_threat


def disengaging(c):
    intent = c.hunt.disengagement
    return bool(intent and intent.get('session_epoch') == c.cycle.session_epoch
                and intent.get('encounter_id') == c.hunt.encounter)


def route_threat(c, frame):
    h = c.hunt
    if (not h.active_threat or disengaging(c) or h.blocked or h.input_effect_unverified
            or h.phase not in {'travel', 'choose_area'}):
        return False
    previous = h.phase
    h.phase = 'fight' if h.combat_history_key else 'search'
    h.planning_requested = False
    h.compact_stage = 'inspect' if h.last_target else 'acquire'
    c.event('grind_active_threat_routed', {'from': previous, 'to': h.phase,
            'frame_id': frame.frame_id, 'evidence': h.active_threat,
            'claim': 'Current threat needs Sage combat/recovery or explicit disengagement; no attack authorized'})
    return True
