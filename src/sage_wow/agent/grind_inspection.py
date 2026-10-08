"""Bound unresolved target sensing without choosing a gameplay action."""
from pathlib import Path
from uuid import uuid4
import hashlib
from copy import deepcopy
import time
from datetime import datetime

MAX_ATTEMPTS = 2
MAX_ACTIVE_SECONDS = 60


def normalized(value):
    from sage_wow.agent.grind_search import target_identity
    return target_identity(value)


def _episode(c, frame, target, proof=None):
    name = normalized(target.get('name'))
    result = {'id': str(uuid4()), 'name': name,
        'search_revision': c.hunt.search_revision,
        'life_revision': c.hunt.target_lives.get(name, 0),
        'started_active_at': c.hunt.active_seconds, 'attempts': 0,
        'exhausted': False, 'source_image': frame.image_path,
        'source_sha256': hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
        'frame_source': frame.source, 'source_frame_id': frame.frame_id,
        'source_scope': scope(c, frame), 'configuration': configuration(c)}
    if proof:result['sensing_proof'] = deepcopy(proof)
    return result


def _fact(episode):
    return (episode or {}).get('boundary_absence') or (episode or {}).get('source_absence') or {}


def _fact_fresh(fact):
    try:return 0 <= time.time() - datetime.fromisoformat(fact['captured_at']).timestamp() <= 30
    except (KeyError, TypeError, ValueError):return False


def _safe(c, frame):
    return bool(c.current() and c.fresh(frame) and not c.cycle._scope_error(frame)
        and not c.require_world and not c.hunt.input_effect_unverified and not c.hunt.blocked)


def _stronger(c, target):
    h = c.hunt; hud = target.get('hud') or {}
    return bool(h.loot_request or c.heal_pending or h.no_mana or h.disengagement
        or h.phase in {'recover', 'ui_recover', 'input_effect_unverified'}
        or h.pending and h.pending.get('family') not in {'target', 'combat'}
        or hud.get('player_health') is not None and (hud.get('health_confidence') or 0) >= .8
            and hud['player_health'] <= max(c.config['critical_health_threshold'], c.config['heal_health_fraction']))


def _valid_ledger(c, frame, episode):
    return bool(episode and not episode.get('invalidated_scope_by') and episode.get('source_scope', scope(c, frame)) == scope(c, frame)
        and episode.get('configuration', configuration(c)) == configuration(c) and intact(episode))


def suspend_focus(c, *, reconciled):
    """Carry only unused sensing capacity through a reconciled focus pause."""
    e = c.target_inspection_episode
    prior = getattr(c, 'target_inspection_focus_resume', None)
    valid = bool(e and reconciled and not c.hunt.input_effect_unverified
        and e.get('name') and not _fact(e) and intact(e)
        and e.get('configuration') == configuration(c)
        and e.get('attempts', MAX_ATTEMPTS) < MAX_ATTEMPTS
        and (e.get('started_active_at') is None
             or c.hunt.active_seconds - e['started_active_at'] < MAX_ACTIVE_SECONDS))
    if (valid and prior and prior['episode_id'] == e['id']
        and prior['generation'] == c.cycle.input_generation
        and prior['source_scope'] == e.get('source_scope')):
        return  # A second pause before world verification cannot renew capacity.
    c.target_inspection_focus_resume = None
    if (not valid or e.get('invalidated_scope_by')
        or (e.get('source_scope') or {}).get('session_epoch') != c.cycle.session_epoch):
        return
    c.target_inspection_focus_resume = {'episode_id': e['id'],
        'generation': c.cycle.input_generation, 'source_scope': deepcopy(e['source_scope']),
        'configuration': deepcopy(e['configuration'])}


def resume_focus(c, frame, result):
    """Fresh verified world rebinds the ledger, never historical target facts."""
    ticket = getattr(c, 'target_inspection_focus_resume', None)
    c.target_inspection_focus_resume = None
    e = c.target_inspection_episode; receipt = result.receipt or {}
    current_scope = scope(c, frame)
    if (not ticket or not e or e['id'] != ticket['episode_id'] or not _safe(c, frame)
        or _fact(e) or not intact(e) or e.get('source_scope') != ticket['source_scope']
        or e.get('configuration') != ticket['configuration']
        or configuration(c) != ticket['configuration']
        or any(current_scope[k] != ticket['source_scope'][k] for k in ('source','width','height'))
        or c.cycle.input_generation != ticket['generation']
        or result.status != 'dispatched' or receipt != c.cycle.last_receipt
        or getattr(result.decision, 'chosen', None) != 'world_normal_confirmed'
        or receipt.get('request_id') != result.decision.envelope.request_id
        or receipt.get('source_frame_id') != frame.frame_id
        or receipt.get('session_epoch') != c.cycle.session_epoch
        or receipt.get('generation_before') != ticket['generation']
        or receipt.get('generation_after') != ticket['generation']
        or receipt.get('possible_input') or not c.hunt.known_completed_input({'receipt': receipt})):
        return False
    predecessor = {k: deepcopy(e.get(k)) for k in
        ('source_scope','source_image','source_sha256','source_frame_id','invalidated_scope_by')}
    e.update(source_scope=current_scope, source_image=frame.image_path,
        source_sha256=hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
        source_frame_id=frame.frame_id, frame_source=frame.source)
    e.pop('invalidated_scope_by', None)
    # Old observer/conflict/operation results retain their old epoch and cannot
    # authorize action. A new observer still spends this same ledger's capacity.
    c.event('grind_target_inspection_focus_rebound', {'episode_id': e['id'],
        'predecessor': predecessor, 'current_scope': current_scope,
        'world_receipt_id': receipt['receipt_id'], 'attempts_retained': e['attempts'],
        'started_active_at_retained': e['started_active_at'], 'combat_allowance_renewed': False})
    return True


def _residual_binding(c, frame, target, episode):
    """Current work can use old capacity, never acquire a new allowance."""
    if not selection_conflicts(target) or not _valid_ledger(c, frame, episode) or not _safe(c, frame):
        return None
    existing = episode.get('residual_work')
    if existing and existing['scope'] == scope(c, frame) and existing['continuity'] == c.hunt.target_continuity:
        return existing
    from sage_wow.agent.grind_acquisition import acquisition_proof
    proof = acquisition_proof(c, frame, c.hunt.pending, c.hunt.linked(frame, c.cycle.input_generation))
    if proof is None:
        from sage_wow.agent.grind_travel_scout import threat_current
        if not threat_current(c, frame):return None
        proof = {'kind': 'current_threat', 'threat': deepcopy(c.hunt.active_threat)}
    return {'scope': scope(c, frame), 'continuity': c.hunt.target_continuity,
        'acquisition': proof, 'name': normalized(target.get('name')),
        'source_image': frame.image_path,
        'source_sha256': hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
        'frame_source': frame.source}


def availability(c, frame, target):
    """Pure factual-observer availability, after the controller lifecycle refresh."""
    e = c.target_inspection_episode
    elapsed = max(0, c.hunt.active_seconds - e['started_active_at']) if e and e.get('started_active_at') is not None else 0
    result = {'episode_id': (e or {}).get('id'), 'source_frame_id': frame.frame_id,
        'remaining_attempts': max(0, MAX_ATTEMPTS - (e or {}).get('attempts', 0)),
        'remaining_active_seconds': max(0, MAX_ACTIVE_SECONDS - elapsed)}
    def answer(state, reason):return {**result, 'state': state, 'reason': reason}
    if not _safe(c, frame):return answer('unavailable', 'current_source_or_input_invalid')
    if _stronger(c, target):return answer('deferred', 'stronger_current_work')
    if not (c.config.get('selected_target_observer') or {}).get('enabled'):
        return answer('unavailable', 'observer_disabled')
    if e and not _valid_ledger(c, frame, e):return answer('unavailable', 'ledger_scope_or_configuration_invalid')
    if e and e.get('disposition') == 'source_resolved_absent' and not selection_conflicts(target):
        return answer('source_complete', 'empty_source_question_completed')
    if not result['remaining_attempts']:return answer('unavailable', 'observer_attempts_spent')
    if not result['remaining_active_seconds']:return answer('unavailable', 'observer_active_deadline_spent')
    if e and e.get('disposition') == 'source_resolved_absent' and not _residual_binding(c, frame, target, e):
        return answer('unavailable', 'positive_work_without_bound_residual_allowance')
    if time.time() - c.observer_last_at < 5:return answer('deferred', 'observer_throttle')
    return answer('runnable', 'interrupted_residual' if e and e.get('disposition') == 'source_resolved_absent' else 'remaining_allowance')


def requires_facts(c, target):
    """A historical observer limit cannot veto usable current combat evidence."""
    from sage_wow.control.target_names import plain_target_name
    if target.get('name') and not plain_target_name(target['name']):return True
    if target.get('eligibility') in {'eligible', 'ineligible'}:return False
    if target.get('self_target') or target.get('invalid_text'):return False
    level = c.level.last_confirmed_level
    return bool(target.get('observation_conflict') or target.get('eligibility')=='unknown' and len(target.get('levels') or [])!=1 or not target.get('name')
        or level in c.hunt.target_bands_by_player_level and len(target.get('levels') or []) != 1)


def selected_unavailable(c, frame, target):
    return bool(selection_conflicts(target) and requires_facts(c, target)
        and availability(c, frame, target)['state'] == 'unavailable')


def current_combat_owner(c, target, pending):
    """An existing actual combat task keeps its independent clear authority."""
    h=c.hunt;owner=h.combat_history_key;history=h.target_history.get(owner) or {}
    return bool(owner and not h.encounter_ended and not h.target_dead_observed
        and not (pending and pending.get('family')=='target')
        and owner==(h.approach or {}).get('history_key') and history.get('encounter_id')==h.encounter
        and not history.get('completed') and not history.get('retired_by_acquisition')
        and normalized(target.get('name'))==h.last_target
        and (h.recent_combat or {}).get('target_history_key')==owner
        and (h.recent_combat or {}).get('target_continuity')==h.target_continuity)


def current_work(c):
    return c.target_inspection_episode or getattr(c, 'target_sensing_work', None)


def refresh_work(c, frame, target):
    """An exit binding with no observer allowance and no combat owner."""
    if c.target_inspection_episode or not selected_unavailable(c, frame, target):return
    old = getattr(c, 'target_sensing_work', None)
    identity = (c.hunt.target_continuity, normalized(target.get('name')), c.hunt.search_revision)
    if old and old['identity'] == identity:return
    c.target_sensing_work = {'id': str(uuid4()), 'identity': identity,
        'source_frame_id': frame.frame_id, 'source_image': frame.image_path,
        'source_sha256': hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
        'source_scope': scope(c, frame), 'configuration': configuration(c),
        'kind': 'selected_work_without_observer_allowance'}


def refresh_operation(c, frame, target):
    operation = getattr(c, 'target_reinspection_operation', None)
    if not operation or operation['state'] in {'admitted', 'cancelled'}:return
    state = availability(c, frame, target)
    if not operation_current(c,frame,operation):
        status, reason = 'cancelled', 'selected_sensing_authority_changed'
    elif state['reason']=='current_source_or_input_invalid' and not c.hunt.input_effect_unverified:
        status, reason = 'deferred', 'current_source_or_world_wait'
    else:
        status, reason = ('committed' if state['state']=='runnable' else 'deferred' if state['state']=='deferred' else 'cancelled'), state['reason']
    if operation['state'] != status or operation.get('reason') != reason:
        operation.update(state=status, reason=reason)
        c.event('grind_target_reinspection_operation', deepcopy(operation))


def operation_current(c, frame, operation):
    return bool(operation and operation['scope']==scope(c,frame)
        and operation['generation']==c.cycle.input_generation
        and operation['episode_id']==(c.target_inspection_episode or {}).get('id')
        and operation['configuration']==configuration(c)
        and operation['target_continuity']==c.hunt.target_continuity
        and intact(operation))


def operation_ready(c, frame, target):
    operation=getattr(c,'target_reinspection_operation',None)
    return bool(operation and operation['state'] in {'committed','deferred'}
        and operation_current(c,frame,operation) and availability(c,frame,target)['state']=='runnable')


def retain_conflict(c, frame, rejection):
    """Bookkeeping is never an observer admission or a free allowance."""
    c.target_inspection_conflict = {**deepcopy(rejection), 'scope': scope(c, frame),
        'generation': c.cycle.input_generation, 'continuity': c.hunt.target_continuity,
        'revision': c.revision, 'configuration': configuration(c),
        'source_image':frame.image_path,
        'source_sha256':hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()}
    if c.target_inspection_episode is not None:
        c.target_inspection_episode['conflict'] = deepcopy(c.target_inspection_conflict)


def _retire_lineage(c, episode, reason):
    lineage = (episode or {}).get('lineage')
    if not lineage or lineage.get('retired_by'):return
    lineage['retired_by'] = reason
    c.hunt.question_debt.pop(lineage['question_key'], None)
    if getattr(c.hunt, 'pinned_boundary_question', None) == lineage['question_key']:
        c.hunt.pinned_boundary_question = None
    c.event('grind_interrupted_boundary_retired', {**deepcopy(lineage),
        'accounting_kind': 'admitted_boundary_requests', 'combat_allowance_renewed': False})


def _lineage(c, episode):
    if not episode.get('lineage'):
        key = str(('interrupted_absence_boundary', episode['id']))
        episode['lineage'] = {'id': episode['id'], 'question_key': key,
            'accounting_kind': 'admitted_boundary_requests', 'request_ids': [], 'previous_unclear': False}
        c.hunt.question(key)
        c.hunt.question_debt.setdefault(key, 0)
        c.hunt.pinned_boundary_question = key
    return episode['lineage']


def needs_boundary(c, frame, target, pending):
    """Ask about the real HUD after an interrupted empty source, before Tab."""
    e = c.target_inspection_episode; fact = _fact(e)
    if not e or not e.get('source_absence') or not _safe(c, frame) or _stronger(c, target):return False
    if selection_conflicts(target) or c.hunt.active_threat:return False
    if pending is not None and (pending.get('family')!='target' or not c.hunt.known_completed_input(pending)
        or not c.hunt.linked(frame,c.cycle.input_generation)):return False
    return bool(not fact or fact.get('invalidated_by') or fact.get('consumed_by')
        or fact.get('generation') != c.cycle.input_generation
        or fact.get('target_continuity') != c.hunt.target_continuity
        or fact.get('source_scope') != scope(c, frame) or fact.get('configuration') != configuration(c))


def boundary_spent(c):
    lineage=(c.target_inspection_episode or {}).get('lineage') or {}
    return bool(not lineage.get('retired_by') and lineage.get('question_key')
        and c.hunt.question_debt.get(lineage['question_key'],0)>=2)


def handoff_boundary(c, frame, target, pending):
    """A spent factual question cannot preempt unrelated guarded capabilities."""
    if not boundary_spent(c) or not needs_boundary(c,frame,target,pending):return False
    if pending is not None:
        # needs_boundary authenticated this exact completed target input.
        c.hunt.archive_pending('interrupted_absence_boundary_exhausted_unknown')
        c.flush_outcomes()
    from sage_wow.agent.grind_relocation import handoff_unknown
    return handoff_unknown(c,frame,target)


def boundary_request(c, frame, target):
    e = c.target_inspection_episode
    lineage = _lineage(c, e)
    return {**assessment_pin(c,frame,target), 'lineage': lineage, 'key': lineage['question_key'],
        'prior_count': c.hunt.question_debt.get(lineage['question_key'], 0),
        'reasoning': 'auto' if lineage.get('previous_unclear') and c.hunt.question_debt.get(lineage['question_key'], 0) else 'off'}


def assessment_pin(c, frame, target):
    return {'episode': c.target_inspection_episode,
        'frame': frame, 'source_sha256': hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
        'scope': scope(c, frame), 'configuration': configuration(c), 'revision': c.revision,
        'generation': c.cycle.input_generation, 'continuity': c.hunt.target_continuity,
        'selection_revision': c.hunt.selection_revision, 'pending': c.hunt.pending,
        'pending_snapshot': deepcopy(c.hunt.pending), 'target': deepcopy(target),
        'threat': deepcopy(c.hunt.active_threat),
        'current_hud': deepcopy(getattr(c, 'current_hud', None)),
        'phase': c.hunt.phase}


def boundary_current(c, request):
    e = request['episode']; lineage = request['lineage']
    return bool(e.get('lineage') is lineage and not lineage.get('retired_by') and assessment_current(c,request))


def assessment_current(c, request):
    e = request['episode']; frame = request['frame']
    return bool(c.target_inspection_episode is e and _safe(c, frame) and not _stronger(c, request['target'])
        and c.revision == request['revision'] and scope(c, frame) == request['scope']
        and configuration(c) == request['configuration'] and c.cycle.input_generation == request['generation']
        and c.hunt.target_continuity == request['continuity'] and c.hunt.selection_revision == request['selection_revision']
        and c.hunt.pending is request['pending'] and c.hunt.pending == request['pending_snapshot']
        and c.hunt.active_threat == request['threat'] and not selection_conflicts(request['target'])
        and c.hunt.phase == request['phase'] and getattr(c, 'current_hud', None) == request['current_hud']
        and intact({'source_image': frame.image_path, 'source_sha256': request['source_sha256']}))


def start_boundary(c, request, request_id):
    lineage = request['lineage']; key = request['key']; count = c.hunt.question_debt.get(key, 0)
    if (not boundary_current(c, request) or count != request['prior_count'] or count >= 2
        or request_id in lineage['request_ids'] or c.hunt.pinned_boundary_question != key):
        raise ValueError('Interrupted absence boundary admission is stale or spent')
    c.hunt.question_debt[key] = count + 1
    lineage['request_ids'].append(request_id)
    request['request_id'] = request_id
    c.event('grind_interrupted_boundary_admitted', {'lineage_id': lineage['id'], 'question_key': key,
        'request_id': request_id, 'frame_id': request['frame'].frame_id, 'admitted_count': count + 1,
        'accounting_kind': 'admitted_boundary_requests', 'reasoning': request['reasoning']})


def finish_boundary(c, request, result, data):
    if not request.get('request_id'):return
    lineage = request['lineage']; count = c.hunt.question_debt.get(request['key'], 0)
    unclear = result is None or result.no_input_abstention or result.status != 'dispatched' or bool(data.get('unclear'))
    actual_unclear = bool(result is not None and not data.get('discarded') and boundary_current(c,request)
        and (result.no_input_abstention or result.status=='dispatched'
            and getattr(result.decision,'chosen',None)=='cannot_assess' and data.get('unclear')))
    lineage['previous_unclear'] = actual_unclear
    lineage['last_outcome'] = getattr(result, 'status', 'interrupted')
    c.event('grind_interrupted_boundary_finished', {'lineage_id': lineage['id'],
        'request_id': request['request_id'], 'outcome': lineage['last_outcome'],
        'admitted_count': count, 'accounting_kind': 'admitted_boundary_requests',
        'reasoning_auto_prerequisite':actual_unclear})
    if unclear:
        c.wait_until = time.time() + 2
        if count >= 2 and boundary_current(c, request):c.request_recovery('interrupted_absence_boundary_unresolved')


def prepare_witness(c, frame, target):
    """Called before an ordinary unguarded Tab; never reconstructed post-input."""
    e = c.target_inspection_episode; fact = _fact(e)
    from sage_wow.agent.grind_resources import hud_resources
    actual = {**target, 'hud': hud_resources(c, frame)}
    if (not fact or fact.get('consumed_by') or fact.get('invalidated_by') or not _safe(c, frame)
        or _stronger(c, actual) or c.hunt.active_threat or selection_conflicts(actual)
        or fact['generation'] != c.cycle.input_generation or fact['target_continuity'] != c.hunt.target_continuity
        or fact['source_scope'] != scope(c, frame) or fact['configuration'] != configuration(c)
        or not intact(fact) or not _fact_fresh(fact)):
        return None
    # An authenticated absent HUD has no portrait identity to compare: these
    # pixels contain animated world scenery. Transport only its bounded fact,
    # under unchanged input ownership and the fresh positive-cue veto above.
    # This witness grants sensing after a real Tab, never a target or cast fact.
    return {'episode_id': e['id'], 'boundary_id': fact['id'], 'frame_id': frame.frame_id,
        'source_image': frame.image_path, 'source_sha256': hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
        'scope': scope(c, frame), 'configuration': configuration(c), 'revision': c.revision,
        'generation': c.cycle.input_generation, 'continuity': c.hunt.target_continuity,
        'candidate': 'target_enemy'}


def witness_current(c, frame, target, witness):
    return bool(witness and prepare_witness(c, frame, target) == witness)


def attach_witness(c, witness, frame, result):
    p = c.hunt.pending; r = result.receipt or {}
    if (witness and p and p.get('receipt') == r and r == c.cycle.last_receipt
        and getattr(result.decision, 'chosen', None) == witness['candidate']
        and result.decision.envelope.request_id == r.get('request_id')
        and r.get('source_frame_id') == r.get('dispatch_frame_id') == witness['frame_id'] == p['source_frame_id']
        and r.get('session_epoch') == witness['scope']['session_epoch']
        and r.get('generation_before') == witness['generation'] and c.hunt.known_completed_input(p)
        and p.get('source_hash') == witness['source_sha256'] and intact(witness)):
        p['sensing_boundary_witness'] = {**deepcopy(witness), 'request_id': r['request_id'], 'receipt_id': r['receipt_id']}


def scope(c, frame):
    return {'session_epoch': c.cycle.session_epoch, 'source': frame.source,
        'width': frame.width, 'height': frame.height}


def configuration(c):
    """Only calibrated sensing geometry participates in this provenance."""
    from sage_wow.agent.ui_layout import from_profile
    return {'boxes': deepcopy({key: c.config.get(key) for key in
        ('target_name_box', 'target_level_box', 'target_badge_continuity', 'selected_target_observer')}),
        'layout': deepcopy(from_profile(c.profile))}


def selection_conflicts(target):
    """Any attached selected bar contradicts absence, including a dead bar."""
    visual = target.get('visual_observation') or {}; hud = target.get('hud') or {}
    return bool(target.get('name') or target.get('levels') or target.get('self_target')
        or target.get('invalid_text') or target.get('observation_conflict')
        or visual.get('selected_hud') == 'present' or visual.get('name')
        or visual.get('level') is not None
        or hud.get('target_health') is not None and (hud.get('target_health_confidence') or 0) >= .8)


def intact(fact):
    try:
        return hashlib.sha256(Path(fact['source_image']).read_bytes()).hexdigest() == fact['source_sha256']
    except (OSError, KeyError, TypeError):
        return False


def complete_source_absence(c, frame, target, cached):
    """Finish this source question; retain its tombstone against empty-frame loops."""
    episode = c.target_inspection_episode
    if (not episode or episode['id'] != cached.get('inspection_episode_id')
        or selection_conflicts(target)):
        return False
    fact = {'id': episode['id'] + ':' + frame.frame_id,
        'source_frame_id': frame.frame_id, 'source_image': frame.image_path,
        'source_sha256': cached['source_sha256'], 'source_scope': scope(c, frame),
        'captured_at': frame.captured_at, 'generation': c.cycle.input_generation,
        'target_continuity': c.hunt.target_continuity,
        'configuration': configuration(c), 'consumed_by': None,
        'source_hud': deepcopy(target.get('hud') or {}),
        'observation': deepcopy(cached['result'])}
    episode.update(disposition='source_resolved_absent', source_absence=fact)
    c.event('grind_target_inspection_source_resolved', {**deepcopy(episode),
        'verdict': 'absent', 'current_absence_authority': False, 'gameplay_outcome_consumed': False})
    return True


def new_selected_task(c, frame, target):
    """One proved empty-to-positive sensing edge, never combat renewal."""
    episode = c.target_inspection_episode or {}; fact = _fact(episode)
    if not fact or fact.get('consumed_by') or fact.get('invalidated_by'):
        return None
    h = c.hunt
    hud = target.get('hud') or {}
    if (h.loot_request or c.heal_pending or h.no_mana or h.disengagement
        or h.phase in {'recover','ui_recover','input_effect_unverified'}
        or (hud.get('player_health') is not None and (hud.get('health_confidence') or 0) >= .8
            and hud['player_health'] <= max(c.config['critical_health_threshold'],c.config['heal_health_fraction']))):
        return None  # Stronger current work defers, never consumes the edge.
    if (not c.current() or not c.fresh(frame) or c.cycle._scope_error(frame)
        or c.require_world or h.input_effect_unverified or h.blocked
        or fact['source_scope'] != scope(c, frame) or fact['configuration'] != configuration(c)
        or not intact(fact) or target.get('self_target') or target.get('invalid_text')
        or target.get('observation_conflict')):
        return None
    # A semantic contradiction to an old absent projection is expected at the
    # boundary, but never supplied as independent positive identity evidence.
    visual = target.get('visual_observation') or {}
    positive = bool(target.get('name') or target.get('levels')
        or visual.get('selected_hud') == 'present' or visual.get('name')
        or visual.get('level') is not None
        or hud.get('target_health') is not None and hud['target_health'] > 0
            and (hud.get('target_health_confidence') or 0) >= .8)
    if not positive:
        return None
    from sage_wow.agent.grind_only import same_patch
    box = c.config.get('target_name_box') or target['box']
    changed_pixels = not same_patch(fact['source_image'], frame.image_path, box)
    if not changed_pixels and hud.get('target_health') is not None and hud['target_health'] > 0 and (hud.get('target_health_confidence') or 0) >= .8:
        from sage_wow.agent.ui_layout import UIRegions, from_profile
        health_box = UIRegions(frame, (frame.width,frame.height), from_profile(c.profile)).pixels('target_health',(0,0,1,1))
        changed_pixels = not same_patch(fact['source_image'],frame.image_path,health_box)
    if not changed_pixels:
        return None
    proof = None; pending = h.pending
    from sage_wow.agent.grind_acquisition import acquisition_proof
    acquired = acquisition_proof(c, frame, pending, h.linked(frame, c.cycle.input_generation))
    witness = (pending or {}).get('sensing_boundary_witness') or {}
    receipt = (pending or {}).get('receipt') or {}
    if (acquired and witness.get('boundary_id') == fact['id']
        and witness.get('episode_id') == episode['id']
        and witness.get('receipt_id') == receipt.get('receipt_id')
        and witness.get('request_id') == receipt.get('request_id')
        and receipt.get('source_frame_id') == receipt.get('dispatch_frame_id') == witness.get('frame_id') == pending.get('source_frame_id')
        and receipt.get('generation_before') == fact['generation'] == witness.get('generation')
        and pending.get('target_continuity') == fact['target_continuity']
        and h.target_continuity == fact['target_continuity'] + 1
        and pending.get('source_scope') == fact['source_scope'] == witness.get('scope')
        and pending.get('source_hash') == witness.get('source_sha256') and intact(witness)
        and not selection_conflicts(pending.get('target') or {})):
        proof = {'kind': 'linked_acquisition', **acquired, 'absence_transition_id': fact['id'],
            'preinput_witness': deepcopy(witness)}
    elif (pending is None and _fact_fresh(fact) and c.cycle.input_generation == fact['generation']
        and h.target_continuity == fact['target_continuity']):
        from sage_wow.agent.grind_encounter_recovery import observe_threat
        from sage_wow.agent.grind_travel_scout import threat_current
        observe_threat(c, frame, target)
        if threat_current(c, frame):
            proof = {'kind': 'current_threat_selection', 'frame_id': frame.frame_id,
                'absence_transition_id': fact['id'], 'threat': deepcopy(h.active_threat)}
    fact['consumed_by' if proof else 'invalidated_by'] = deepcopy(proof) if proof else frame.frame_id
    return proof


def reanchor_absence(c, frame, target, pending, result):
    """A genuine current absence assessment carries this task, without renewal."""
    episode = c.target_inspection_episode or {}; fact = _fact(episode)
    receipt = (pending or {}).get('receipt') or {}; assessment = result.receipt or {}
    from sage_wow.agent.grind_resources import hud_resources
    actual = {**target, 'hud': hud_resources(c, frame)}
    preceding = pending is None and c.hunt.pending is None or bool(pending and c.hunt.pending is pending
        and not pending.get('outcome_consumed') and pending.get('family') == 'target'
        and pending.get('action') == 'target_enemy' and c.hunt.known_completed_input(pending)
        and c.hunt.linked(frame, c.cycle.input_generation))
    if (not episode or not preceding or not _safe(c, frame) or _stronger(c, actual)
        or c.hunt.active_threat or selection_conflicts(actual)
        or result.status != 'dispatched' or assessment != c.cycle.last_receipt
        or getattr(result.decision, 'chosen', None) != 'no_selected_frame'
        or assessment.get('request_id') != result.decision.envelope.request_id
        or assessment.get('possible_input') or not c.hunt.known_completed_input({'receipt': assessment})
        or assessment.get('source_frame_id') != frame.frame_id
        or assessment.get('generation_before') != c.cycle.input_generation
        or assessment.get('generation_after') != c.cycle.input_generation
        or assessment.get('session_epoch') != c.cycle.session_epoch):
        return False
    predecessor = {k: deepcopy(fact.get(k)) for k in ('id', 'consumed_by', 'invalidated_by')}
    fact = {'id': episode['id'] + ':' + assessment['receipt_id'],
        'source_frame_id': frame.frame_id, 'source_image': frame.image_path,
        'source_sha256': hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
        'source_scope': scope(c, frame), 'configuration': configuration(c),
        'captured_at': frame.captured_at, 'generation': c.cycle.input_generation,
        'target_continuity': c.hunt.target_continuity, 'consumed_by': None,
        'predecessor': predecessor, 'assessment_receipt': deepcopy(assessment),
        'reanchored_by': {'target_receipt_id': receipt.get('receipt_id'), 'assessment_receipt_id': assessment['receipt_id']}}
    episode['boundary_absence'] = fact
    c.event('grind_target_inspection_absence_reanchored', deepcopy(fact))
    return True


def observe_absence_continuity(c, frame, target):
    """The first authentic current absence increment is not a new selection."""
    fact = _fact(c.target_inspection_episode)
    if (fact and not fact.get('consumed_by') and not fact.get('invalidated_by')
        and fact['generation'] == c.cycle.input_generation and fact['source_scope'] == scope(c, frame)
        and intact(fact) and c.current() and c.fresh(frame)
        and c.hunt.attributed_selection_absent(target, frame)
        and not selection_conflicts(target)
        and c.hunt.target_continuity in {fact['target_continuity'], fact['target_continuity'] + 1}):
        fact['target_continuity'] = c.hunt.target_continuity


def complete_clear_absence(c, frame, target, pending, result):
    """Verified selected-task release starts an empty tombstone, not retries."""
    from sage_wow.agent.grind_relocation import typed_release
    from sage_wow.agent.grind_resources import hud_resources
    release = typed_release(c.hunt)
    intent = (c.hunt.strategy_required or {}).get('selected_exit') or {}
    assessment = result.receipt or {}
    actual = {**target, 'hud': hud_resources(c, frame)}
    if (not release or intent.get('sensing_absence_recorded')
        or release['clear'].get('receipt') != (pending or {}).get('receipt')
        or release['assessment'].get('receipt') != assessment or assessment != c.cycle.last_receipt
        or getattr(result.decision, 'chosen', None) not in {'target_cleared','no_selected_frame'}
        or assessment.get('source_frame_id') != frame.frame_id
        or assessment.get('generation_after') != c.cycle.input_generation
        or assessment.get('session_epoch') != c.cycle.session_epoch
        or not c.current() or not c.fresh(frame) or c.cycle._scope_error(frame)
        or selection_conflicts(actual)):
        return False
    episode = c.target_inspection_episode
    if episode is None:return False
    fact = _fact(episode)
    episode['boundary_absence'] = {'id': episode['id'] + ':' + assessment['receipt_id'],
        'source_frame_id': frame.frame_id, 'source_image': frame.image_path,
        'source_sha256': hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
        'source_scope': scope(c, frame), 'configuration': configuration(c),
        'captured_at': frame.captured_at, 'generation': c.cycle.input_generation,
        'target_continuity': c.hunt.target_continuity, 'consumed_by': None,
        'predecessor': {k: deepcopy(fact.get(k)) for k in ('id', 'consumed_by', 'invalidated_by')},
        'assessment_receipt': deepcopy(assessment),
        'reanchored_by': {'target_receipt_id': pending['receipt']['receipt_id'], 'assessment_receipt_id': assessment['receipt_id']}}
    episode['disposition'] = 'source_resolved_absent'
    intent['sensing_absence_recorded'] = episode['boundary_absence']['id']
    c.event('grind_target_inspection_clear_absence', {'episode_id': episode['id'],
        'boundary': deepcopy(episode['boundary_absence']), 'observer_attempts_granted': 0,
        'combat_allowance_renewed': False})
    return True


def refresh(c, frame, target):
    episode = c.target_inspection_episode
    if not episode:
        return None
    if (not episode.get('invalidated_scope_by') and
        (c.hunt.input_effect_unverified or episode.get('source_scope',scope(c,frame)) != scope(c,frame)
            or episode.get('configuration',configuration(c)) != configuration(c))):
        episode['invalidated_scope_by'] = {'frame_id':frame.frame_id,
            'reason':'unverified_input_or_changed_sensing_scope'}
        c.event('grind_target_inspection_scope_invalidated', {'episode_id':episode['id'],
            **episode['invalidated_scope_by'], 'attempts_retained':episode['attempts']})
    fact = _fact(episode)
    assessed = fact.get('reanchored_by') or {}
    # Compact assessment records its absence continuity after apply_choice.
    # Admit that actual increment only while the exact authenticated no-input
    # assessment is still the last receipt; never a generic source+1 gap.
    if (assessed and not fact.get('consumed_by') and not fact.get('invalidated_by')
        and (c.cycle.last_receipt or {}).get('receipt_id') == assessed['assessment_receipt_id']
        and c.cycle.input_generation == fact['generation']
        and scope(c, frame) == fact['source_scope'] and intact(fact)
        and c.hunt.target_continuity in {fact['target_continuity'], fact['target_continuity'] + 1}):
        fact['target_continuity'] = c.hunt.target_continuity
    name = normalized(target.get('name'))
    # Existing search revisions require useful observed movement. Neither a
    # selection receipt nor a fresh frame/epoch is evidence of a new approach.
    changed = c.hunt.search_revision > episode['search_revision']
    if name and name == episode['name']:
        changed |= c.hunt.target_lives.get(name, 0) > episode['life_revision']
    # OCR drift alone must not renew the allowance. A different positive name
    # additionally needs changed calibrated name pixels on the retained source.
    identity_source = episode.get('residual_work') or episode
    source = Path(identity_source['source_image'])
    if (name and episode['name'] and name != episode['name'] and
            source.is_file() and frame.source == identity_source['frame_source'] and
            hashlib.sha256(source.read_bytes()).hexdigest() == identity_source['source_sha256']):
        from sage_wow.agent.grind_only import same_patch
        box = c.config.get('target_name_box')
        if box and not same_patch(source, frame.image_path, box):
            changed = True
    if changed:
        c.event('grind_target_inspection_retired', {**episode,
            'reason': 'observed_changed_target_or_search', 'next_frame_id': frame.frame_id})
        _retire_lineage(c, episode, 'observed_changed_target_or_search')
        c.target_inspection_episode = None
        return None
    if fact:
        proof = new_selected_task(c, frame, target)
        if proof:
            c.event('grind_target_inspection_retired', {**deepcopy(episode),
                'reason': 'authenticated_new_selected_sensing_task', 'next_frame_id': frame.frame_id,
                'sensing_proof': proof, 'combat_allowance_renewed': False})
            # Install atomically, even if current OCR already permits ordinary
            # assessment and no observer is needed. Proof cannot leak forward.
            interrupted = bool(episode.get('lineage') or episode.get('residual_work') or episode.get('boundary_absence'))
            _retire_lineage(c, episode, deepcopy(proof))
            c.target_inspection_episode = _episode(c, frame, target, proof)
            if interrupted:
                c.target_inspection_episode.update(interrupted_lineage=True, started_active_at=None)
            return c.target_inspection_episode
        if (not fact.get('consumed_by') and not fact.get('invalidated_by')
            and (fact['generation'] != c.cycle.input_generation or fact['source_scope'] != scope(c, frame)
                or fact['configuration'] != configuration(c))):
            fact['invalidated_by'] = {'frame_id': frame.frame_id, 'reason': 'selection_source_interrupted'}
            c.event('grind_target_inspection_boundary_invalidated', deepcopy(fact))
        binding = _residual_binding(c, frame, target, episode)
        if binding and selection_conflicts(target):
            episode['residual_work'] = binding
            episode['interrupted_lineage'] = True
    if name and not episode['name']:
        # Learning a previously missing name improves this same assessment.
        episode['name'] = name
        episode['life_revision'] = c.hunt.target_lives.get(name, 0)
    if episode.get('disposition')=='source_resolved_absent' and not selection_conflicts(target):return episode
    if not episode['exhausted'] and (episode['attempts'] >= MAX_ATTEMPTS or
            episode.get('started_active_at') is not None and c.hunt.active_seconds - episode['started_active_at'] >= MAX_ACTIVE_SECONDS):
        episode['exhausted'] = True
        c.event('grind_target_inspection_exhausted', dict(episode))
    return episode


def begin(c, frame, target):
    """Only the actual factual observer calls this atomic admission."""
    state = availability(c, frame, target)
    if state['state'] != 'runnable':return None
    refresh_operation(c,frame,target)
    claimed_operation=operation_ready(c,frame,target)
    episode = c.target_inspection_episode
    if episode is None:
        episode = _episode(c, frame, target)
        c.target_inspection_episode = episode
    if not episode['attempts']:episode['started_active_at'] = c.hunt.active_seconds
    if state['reason'] == 'interrupted_residual':
        episode['residual_work'] = _residual_binding(c, frame, target, episode)
        episode['interrupted_lineage'] = True
    episode['attempts'] += 1
    operation = getattr(c, 'target_reinspection_operation', None)
    if claimed_operation:
        operation.update(state='admitted', admitted_episode_id=episode['id'], attempt=episode['attempts'])
        c.event('grind_target_reinspection_operation', deepcopy(operation))
    episode.pop('conflict', None)
    c.target_inspection_conflict = None
    c.event('grind_target_inspection_attempt', {**deepcopy(episode), 'admission': state,
        'combat_allowance_renewed': False})
    return episode['id']


def resolve(c, cached, verdict, frame):
    episode = c.target_inspection_episode
    if (episode and cached.get('inspection_episode_id') == episode['id'] and
            verdict in {'eligible', 'ineligible', 'absent'} and
            (episode.get('disposition') != 'source_resolved_absent' or episode.get('interrupted_lineage'))):
        c.event('grind_target_inspection_resolved', {**episode,
            'verdict': verdict, 'resolved_frame_id': frame.frame_id})
        if episode.get('interrupted_lineage'):
            episode['resolved_verdict'] = verdict
            episode.pop('conflict', None)
        else:c.target_inspection_episode = None


def exhausted(c):
    return bool(c.target_inspection_episode and c.target_inspection_episode['exhausted'])


def unresolved(c, target, frame=None):
    episode = c.target_inspection_episode
    conflict = getattr(c, 'target_inspection_conflict', None)
    if (conflict and frame is not None and conflict.get('scope') == scope(c,frame)
        and conflict.get('revision') == c.revision and conflict.get('configuration') == configuration(c)
        and conflict.get('generation') == c.cycle.input_generation
        and conflict.get('continuity') == c.hunt.target_continuity and intact(conflict)):
        from sage_wow.agent.grind_only import same_patch, same_target_badge
        from sage_wow.agent.ui_layout import UIRegions, from_profile
        # A source conflict is historical when the selected HUD changes. In
        # particular it cannot manufacture presence on a later blank HUD.
        regions=UIRegions(frame,(frame.width,frame.height),from_profile(c.profile))
        boxes=[target.get('box'),c.config.get('target_name_box') or target.get('box'),
            regions.pixels('target_health',(0,0,1,1))]
        if (all(box and same_patch(conflict['source_image'],frame.image_path,box) for box in boxes)
            and same_target_badge(conflict['source_image'],frame.image_path,target['badge'],
                c.config.get('target_badge_continuity'),combat_glow=False)):
            return {**target, 'eligibility': 'unknown', 'observation_conflict': dict(conflict)}
    if episode and episode.get('conflict') and 'scope' not in episode['conflict']:
        return {**target, 'eligibility': 'unknown',
            'observation_conflict': dict(episode['conflict'])}
    return target
