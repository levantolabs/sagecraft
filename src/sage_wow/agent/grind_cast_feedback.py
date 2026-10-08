"""Unassessed post-cast errors survive interruption as evidence, never input authority."""
from copy import deepcopy
from datetime import datetime
import hashlib
from pathlib import Path
import re
import math

from PIL import Image

from sage_wow.agent.cycle import ActionCandidate, CycleResult


def cue_key(cue):
    return cue['kind'] + ':' + ' '.join(re.sub(r'[^\w\s]', '', cue['text']).casefold().split())


LOCAL_CUES = {'range', 'facing', 'los', 'no_target', 'dead_target', 'resist', 'evade'}
RELEASED_PLAYER_CUES = {'standing', 'cooldown', 'interrupted'}


def _intact_image(path, sha256, dimensions=None):
    try:
        if not path or not sha256 or hashlib.sha256(Path(path).read_bytes()).hexdigest() != sha256:
            return False
        if dimensions:
            with Image.open(path) as image:
                return image.size == tuple(dimensions)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _valid_cues(cues):
    return [cue for cue in cues or [] if isinstance(cue, dict)
            and isinstance(cue.get('kind'), str) and isinstance(cue.get('text'), str)
            and cue['kind'] and cue['text']]


def _later_guard(pending, index, observation):
    """A retained guard is evidence only after proved submission in this burst.

    Missing producer facts are not reconstructed from the expected owner. Old
    logs without this contract remain retrospective evidence.
    """
    from sage_wow.agent.grind_search import target_identity
    receipt = pending.get('receipt') or {}
    execution = receipt.get('execution') or {}
    binding = receipt.get('selected_binding') or {}
    guards = execution.get('guard_observations') or []
    steps = execution.get('completed_steps') or []
    scope = pending.get('source_scope') or {}
    expected = target_identity((pending.get('target') or {}).get('name'))
    if (index <= 0 or execution.get('kind') != 'cast_burst'
            or not receipt.get('completed') or not receipt.get('possible_input')
            or not receipt.get('dispatched') or receipt.get('error') or receipt.get('dispatch_unknown')
            or execution.get('completed') is not True or execution.get('dispatched') is not True
            or not receipt.get('input_steps')
            or any(step.get('status') != 'completed' for step in receipt['input_steps'])
            or binding.get('type') != 'cast_burst' or not expected
            or target_identity(binding.get('expected_target_name')) != expected
            or target_identity(execution.get('expected_target_name')) != expected
            or len(steps) < index or len(guards) <= index
            or not scope or scope.get('session_epoch') != receipt.get('session_epoch')
            or not pending.get('target_history_key')
            or receipt.get('source_frame_id') != pending.get('source_frame_id')
            or not _intact_image(pending.get('source_image'), pending.get('source_hash'),
                (scope.get('width'), scope.get('height')))):
        return False
    try:
        source_at = datetime.fromisoformat(pending['measurement']['captured_at'])
        finished_at = datetime.fromisoformat(receipt['occurred_at'])
        initial = guards[0]
        identity = observation['identity_source']
        reference_keys = ('frame_id', 'captured_at', 'image_path', 'image_sha256', 'source',
            'width', 'height', 'session_epoch', 'controller_revision', 'selection_revision',
            'target_continuity', 'target_history_key', 'encounter_id', 'observed_target_name')
        if any(identity[key] != initial[key] for key in reference_keys):
            return False
        previous_submission = None
        for number, guard in enumerate(guards[:index+1]):
            if (guard['phase'] != 'pre_cast' or type(guard['step_index']) is not int
                    or guard['step_index'] != number or guard['identity_stable'] is not True
                    or {key: guard[key] for key in ('source', 'width', 'height', 'session_epoch')} != scope
                    or guard['controller_revision'] != receipt['scope_objective_revision']
                    or any(guard[key] != pending[key] for key in ('selection_revision',
                        'target_continuity', 'target_history_key'))
                    or guard['encounter_id'] != initial['encounter_id']
                    or target_identity(guard['expected_target_name']) != expected
                    or target_identity(guard['observed_target_name']) != expected
                    or not source_at <= datetime.fromisoformat(guard['captured_at']) <= finished_at
                    or not math.isfinite(guard['captured_at_monotonic'])
                    or not _intact_image(guard['image_path'], guard['image_sha256'],
                        (scope['width'], scope['height']))
                    or number and guard['identity_source'] != identity):
                return False
            if previous_submission is not None and guard['captured_at_monotonic'] <= previous_submission:
                return False
            if number < index:
                step = steps[number]
                if (guard.get('continue') is not True or type(step['index']) is not int
                        or step['index'] != number+1 or step['dispatched'] is not True
                        or step['guard_frame_id'] != guard['frame_id']
                        or not math.isfinite(step['submitted_at_monotonic'])
                        or step['submitted_at_monotonic'] < guard['captured_at_monotonic']):
                    return False
                previous_submission = step['submitted_at_monotonic']
        return True
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


def retained_error_inventory(pending):
    """Read every retained capture without mutating cue or receipt history."""
    if not pending or pending.get('family') != 'combat':
        return []
    inventory = []
    cached = pending.get('position_error_evidence') or {}
    if cached and _intact_image((cached.get('frame') or {}).get('image_path'), cached.get('sha256')):
        # Guard evidence is reconstructed below from its authenticated trace;
        # a mutable cache cannot supply different cues for that image.
        if cached.get('source') != 'attributable_later_precast_guard':
            inventory.append(deepcopy(cached))
    execution = (pending.get('receipt') or {}).get('execution') or {}
    for post in execution.get('post_cast_observations', []):
        cues = _valid_cues(post.get('observed_error_cues'))
        path = post.get('image_path')
        if cues and path:
            try:
                sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except (OSError, TypeError):
                continue
            # Preserve legacy postcast collection; when recorded, its hash is binding.
            if post.get('image_sha256') and post['image_sha256'] != sha256:
                continue
            inventory.append({'cues': deepcopy(cues), 'frame': dict(post),
                'sha256': sha256, 'source': 'attributable_post_cast_capture'})
    before = {cue_key(cue) for cue in _valid_cues((pending.get('measurement') or {}).get('error_cues'))}
    guards = execution.get('guard_observations') or []
    if guards:
        before.update(cue_key(cue) for cue in _valid_cues(guards[0].get('observed_error_cues')))
    for index, guard in enumerate(guards):
        cues = [cue for cue in _valid_cues(guard.get('observed_error_cues')) if cue_key(cue) not in before]
        if cues and _later_guard(pending, index, guard):
            inventory.append({'cues': deepcopy(cues), 'frame': dict(guard),
                'sha256': guard['image_sha256'], 'source': 'attributable_later_precast_guard'})
    return inventory


def unresolved_cues(pending, frame=None, measurement=None, *, nonlocal_only=False):
    if not pending or pending.get('family') != 'combat':
        return []
    rejected = pending.setdefault('rejected_error_cues', []) + pending.get('assessed_error_cues', [])
    cached = pending.get('position_error_evidence') or {}
    if cached and not _intact_image((cached.get('frame') or {}).get('image_path'), cached.get('sha256')):
        return []  # Do not silently repair corrupted retained assessment provenance.
    for evidence in retained_error_inventory(pending):
        cues = [cue for cue in evidence['cues'] if cue_key(cue) not in rejected
                and (not nonlocal_only or cue['kind'] not in LOCAL_CUES)]
        if cues:
            pending['position_error_evidence'] = evidence
            return cues
    pending.pop('position_error_evidence', None)
    before = {cue_key(cue) for cue in pending.get('measurement', {}).get('error_cues', [])}
    cues = [cue for cue in (measurement or {}).get('error_cues', [])
            if cue_key(cue) not in before and cue_key(cue) not in rejected
            and (not nonlocal_only or cue['kind'] not in LOCAL_CUES)]
    if cues and frame:
        pending['position_error_evidence'] = {'cues': deepcopy(cues), 'frame': dict(frame.__dict__),
            'sha256': hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
            'source': 'linked_feedback_frame'}
    return cues


def _attributed_source(h, source):
    """Original combat evidence keeps its scope through selection/focus release."""
    from sage_wow.agent.grind_search import target_identity
    receipt = source.get('receipt') or {}; recent = h.recent_combat or {}
    return bool(source.get('family') == 'combat' and h.known_completed_input(source)
        and receipt.get('possible_input') and receipt.get('dispatched') and receipt.get('receipt_id')
        and receipt.get('source_frame_id') == source.get('source_frame_id')
        and receipt.get('receipt_id') == (recent.get('receipt') or {}).get('receipt_id')
        and source.get('target_history_key') == h.combat_history_key
        and h.combat_history_key in h.target_history
        and source.get('encounter_id') == h.encounter
        and target_identity((source.get('target') or {}).get('name')) == h.last_target
        and source.get('source_scope')
        and source['source_scope'].get('session_epoch') == receipt.get('session_epoch')
        and all(source.get(key) == recent.get(key) for key in ('source_frame_id',
            'source_image', 'source_hash', 'source_scope', 'target_continuity',
            'target_history_key', 'encounter_id', 'selection_revision'))
        and _intact_image(source.get('source_image'), source.get('source_hash'),
            (source['source_scope'].get('width'), source['source_scope'].get('height'))))


def unresolved_nonlocal_cues(h):
    """Read-only owner-attributed inventory; arbitrary historical text cannot veto."""
    history = h.target_history.get(h.combat_history_key) or {}
    review = history.get('unassessed_error_feedback') or {}
    sources = [h.recent_combat or {}, (h.cast_error or {}).get('assessment_source') or {}]
    if review.get('status') in {'unassessed', 'assessed', 'dismissed'}:
        sources.append(review.get('source') or {})
    sources = [source for source in sources if _attributed_source(h, source)]
    disposed = {key for source in sources for key in
        (source.get('rejected_error_cues') or []) + (source.get('assessed_error_cues') or [])}
    error = h.cast_error or {}
    if (error.get('cast_receipt_id') == ((h.recent_combat or {}).get('receipt') or {}).get('receipt_id')
            and _attributed_source(h, error.get('assessment_source') or {}) and _valid_cues([error])):
        disposed.add(cue_key(error))  # The active constraint uses its existing resolution path.
    result = []; seen = set()
    for source in sources:
        for evidence in retained_error_inventory(source):
            for cue in _valid_cues(evidence.get('cues')):
                key = cue_key(cue)
                if cue['kind'] not in LOCAL_CUES and key not in disposed and key not in seen:
                    result.append(deepcopy(cue)); seen.add(key)
    return result


def retain_interrupted(hunt, pending):
    owner = (pending or {}).get('target_history_key')
    if (owner not in hunt.target_history or not hunt.known_completed_input(pending)
            or not unresolved_cues(pending)):
        return
    hunt.target_history[owner]['unassessed_error_feedback'] = {
        'status': 'unassessed', 'source': deepcopy(pending)}


def _selection_release(h):
    """Current selection release is separate from historical combat ownership."""
    from sage_wow.agent.grind_relocation import typed_release
    typed=typed_release(h)
    if typed and h.encounter_ended:return typed
    return h.intentional_clear_current() or {}


def _released_player_source(h):
    """A released player-state constraint retains its exact historical owner."""
    error = h.cast_error or {}; source = error.get('assessment_source') or {}
    clear = _selection_release(h); physical = clear.get('clear') or {}
    typed = clear.get('kind') == 'selected_sensing'
    evidence = source.get('position_error_evidence') or {}
    if (not h.encounter_ended or error.get('status') != 'active'
            or error.get('kind') not in RELEASED_PLAYER_CUES
            or not _valid_cues([error])
            or error.get('encounter_id') != h.encounter
            or h.cast_obligation != f'Current linked {error.get("kind")} error requires observed correction'
            or not _attributed_source(h, source)
            or error.get('cast_receipt_id') != source['receipt'].get('receipt_id')
            or clear.get('disposition') != 'closed'
            or not typed and (clear.get('owner') != h.combat_history_key
                or clear.get('encounter_id') != h.encounter
                or clear.get('combat_receipt_id') != error.get('cast_receipt_id'))
            or not h.known_completed_input(physical) or not (physical.get('receipt') or {}).get('possible_input')
            or not _intact_image(physical.get('source_image'), physical.get('source_hash'))
            or not _intact_image((clear.get('assessment') or {}).get('image_path'),
                (clear.get('assessment') or {}).get('sha256'))
            or cue_key(error) not in {cue_key(cue) for cue in _valid_cues(evidence.get('cues'))}
            or not _intact_image((evidence.get('frame') or {}).get('image_path'), evidence.get('sha256'))):
        return None
    return source


def released_player_resolution(h):
    """Authenticate this cue's own-state disposition; other cues remain separate."""
    source = _released_player_source(h)
    error = h.cast_error or {}; proof = error.get('released_resolution') or {}
    clear = _selection_release(h)
    receipt = proof.get('receipt') or {}; scope = proof.get('scope') or {}
    if (not source or proof.get('disposition') not in {'current_ready', 'current_resolved_or_unsupported', 'own_stance_observed'}
            or proof.get('disposition') == 'current_ready' and error.get('kind') not in {'cooldown', 'interrupted'}
            or proof.get('owner') != h.combat_history_key or proof.get('encounter_id') != h.encounter
            or proof.get('cast_receipt_id') != error.get('cast_receipt_id')
            or proof.get('cue_key') != cue_key(error)
            or proof.get('clear_receipt_id') != ((clear.get('clear') or {}).get('receipt') or {}).get('receipt_id')
            or not h.known_completed_input({'receipt': receipt}) or receipt.get('possible_input')
            or (receipt.get('selected_binding') or {}).get('type') != 'observe_only'
            or receipt.get('source_frame_id') != proof.get('frame_id')
            or receipt.get('session_epoch') != scope.get('session_epoch')
            or receipt.get('generation_after') != proof.get('input_generation')
            or any(scope.get(key) != source['source_scope'].get(key) for key in ('source', 'width', 'height'))
            or not _intact_image(proof.get('image_path'), proof.get('sha256'), (scope.get('width'), scope.get('height')))):
        return False
    try:
        captured = datetime.fromisoformat(proof['captured_at'])
        if not datetime.fromisoformat(clear['assessment']['captured_at']) <= captured <= datetime.fromisoformat(receipt['occurred_at']):
            return False
    except (KeyError, ValueError, TypeError):
        return False
    if proof['disposition'] == 'own_stance_observed':
        motion = proof.get('stance_source') or {}
        if error['kind'] != 'standing' or not _stance_marker_matches(h, motion):
            return False
        try:
            if captured <= datetime.fromisoformat(motion['receipt']['occurred_at']):
                return False
        except (KeyError, ValueError, TypeError):
            return False
    return True


def _stance_marker_matches(h, pending):
    error = h.cast_error or {}; clear = _selection_release(h)
    marker = pending.get('released_error_stance')
    if not isinstance(marker, dict) or not _released_player_source(h):
        return False
    expected = {'owner': h.combat_history_key, 'encounter_id': h.encounter,
        'cast_receipt_id': error.get('cast_receipt_id'), 'cue_key': cue_key(error),
        'clear_receipt_id': ((clear.get('clear') or {}).get('receipt') or {}).get('receipt_id')}
    receipt = pending.get('receipt') or {}; scope = pending.get('source_scope') or {}
    return bool(marker == expected and error.get('kind') == 'standing'
        and pending.get('family') == 'motion' and pending.get('purpose') == 'standing'
        and pending.get('action') == 'forward' and pending.get('target_history_key') == expected['owner']
        and pending.get('encounter_id') == expected['encounter_id']
        and pending.get('correction_error_receipt_id') == expected['cast_receipt_id']
        and pending.get('correction_error_cue') == expected['cue_key']
        and h.known_completed_input(pending) and receipt.get('possible_input') and receipt.get('dispatched')
        and receipt.get('source_frame_id') == pending.get('source_frame_id')
        and receipt.get('session_epoch') == scope.get('session_epoch')
        and _intact_image(pending.get('source_image'), pending.get('source_hash'),
            (scope.get('width'), scope.get('height'))))


def _typed_review_generation(h, release):
    proof=release['assessment'];transport=release.get('own_state_transport')
    if not transport:return proof.get('input_generation')
    motion=transport.get('stance_source') or {};marker=motion.get('released_error_stance') or {}
    receipt=motion.get('receipt') or {};assessment=transport.get('assessment') or {}
    observed=assessment.get('receipt') or {};scope=proof.get('scope') or {};source=h.recent_combat or {}
    standing={cue_key(cue) for item in retained_error_inventory(source) for cue in item.get('cues',[]) if cue.get('kind')=='standing'}
    if (not _attributed_source(h,source) or not isinstance(marker,dict)
        or marker.get('owner')!=h.combat_history_key or marker.get('encounter_id')!=h.encounter
        or marker.get('cast_receipt_id')!=source.get('receipt',{}).get('receipt_id') or marker.get('cue_key') not in standing
        or marker.get('clear_receipt_id')!=release['clear'].get('receipt',{}).get('receipt_id')
        or motion.get('family')!='motion' or motion.get('purpose')!='standing' or motion.get('action')!='forward'
        or motion.get('target_history_key')!=marker.get('owner') or motion.get('source_scope')!=scope
        or not h.known_completed_input(motion) or not receipt.get('possible_input')
        or receipt.get('session_epoch')!=scope.get('session_epoch') or receipt.get('source_frame_id')!=motion.get('source_frame_id')
        or receipt.get('generation_before')!=transport.get('previous_generation')
        or not _intact_image(motion.get('source_image'),motion.get('source_hash'),(scope.get('width'),scope.get('height')))
        or transport.get('outcome') not in {'motion_useful','motion_no_useful_effect','error_not_supported'}
        or assessment.get('scope')!=scope or assessment.get('input_generation')!=receipt.get('generation_after')
        or not h.known_completed_input({'receipt':observed}) or observed.get('possible_input')
        or observed.get('source_frame_id')!=assessment.get('frame_id') or observed.get('session_epoch')!=scope.get('session_epoch')
        or observed.get('generation_after')!=assessment.get('input_generation')
        or not _intact_image(assessment.get('image_path'),assessment.get('sha256'),(scope.get('width'),scope.get('height')))):
        return None
    try:
        if not datetime.fromisoformat(receipt['occurred_at'])<datetime.fromisoformat(assessment['captured_at'])<=datetime.fromisoformat(observed['occurred_at']):return None
    except (KeyError,ValueError,TypeError):return None
    return receipt.get('generation_after')


def _released_selection_current(c, frame, target):
    """Current release prerequisite for own-state review, never absence authority."""
    from sage_wow.agent.grind_relocation import typed_release, current_target, _absence
    h=c.hunt;target=current_target(c,frame,target)
    if h.attributed_selection_absent(target,frame):return True
    release=typed_release(h)
    if not release or not h.encounter_ended or not c.current() or not c.fresh(frame) or c.cycle._scope_error(frame):return False
    proof=release['assessment'];scope=proof.get('scope') or {}
    if (scope!={'session_epoch':c.cycle.session_epoch,'source':frame.source,'width':frame.width,'height':frame.height}
        or not _absence(target)):return False
    generation=_typed_review_generation(h,release)
    if generation==c.cycle.input_generation:return True
    pending=h.pending
    # Only this authenticated linked own-stance pulse transports the review
    # prerequisite past the release generation; arbitrary input cannot do so.
    return bool(pending and _stance_marker_matches(h,pending)
        and pending.get('source_scope')==scope and h.linked(frame,c.cycle.input_generation)
        and (pending.get('receipt') or {}).get('generation_before')==generation)


def _player_review_current(c, frame, target):
    from sage_wow.agent.grind_encounter_recovery import capture_scope
    from sage_wow.agent.grind_resources import hud_resources
    h = c.hunt; source = _released_player_source(h)
    if (not source or not c.current() or not c.fresh(frame) or c.cycle._scope_error(frame)
            or h.blocked or h.input_effect_unverified or h.active_threat or h.disengagement
            or h.loot_request or c.heal_pending or h.no_mana or c.require_world
            or any(source['source_scope'].get(key) != capture_scope(c, frame).get(key)
                for key in ('source', 'width', 'height'))):
        return False
    hud = target.get('hud') or {}
    if hud.get('frame_id') != frame.frame_id:
        hud = hud_resources(c, frame)
    return bool(_released_selection_current(c, frame, {**target, 'hud':hud})
        and hud.get('player_health') is not None and (hud.get('health_confidence') or 0) >= .8
        and hud['player_health'] > c.config['critical_health_threshold'] and hud.get('player_mana') != 0)


async def reassess_released_player(c, frame, target, measurement):
    """Current own-player state, using the original receipt/cue question ledger."""
    from sage_wow.agent.grind_only import composed_evidence_image
    from sage_wow.agent.grind_encounter_recovery import capture_scope
    from sage_wow.agent.cycle import DispatchValidation
    h = c.hunt; pending = h.pending; error = h.cast_error or {}
    marked = pending is not None and 'released_error_stance' in pending
    if marked and (not _stance_marker_matches(h, pending) or not _player_review_current(c, frame, target)
            or not h.linked(frame, c.cycle.input_generation)
            or pending.get('source_scope') != capture_scope(c, frame)):
        h.archive_pending('released_stance_owner_or_current_scope_unverified')
        c.flush_outcomes()
        return CycleResult('grind_reobserve', detail='Released stance result retained without positive correction credit')
    if (not _player_review_current(c, frame, target) or pending is not None and not marked
            or released_player_resolution(h)):
        return None
    source = error['assessment_source']; evidence = source['position_error_evidence']
    authority = (c.revision, c.cycle.session_epoch, c.cycle.input_generation, h.selection_revision, h.target_continuity)
    valid = lambda: (h.cast_error is error and h.pending is pending
        and authority == (c.revision, c.cycle.session_epoch, c.cycle.input_generation, h.selection_revision, h.target_continuity)
        and _player_review_current(c, frame, target)
        and (not marked or _stance_marker_matches(h, pending) and h.linked(frame, c.cycle.input_generation)))
    identity = str(('interrupted_cast_error', source['receipt']['receipt_id'], cue_key(error)))
    if not c.select_question(identity, 'feedback'):
        return CycleResult('grind_blocked', detail='Released player-state assessment capacity retained')
    before = pending if marked else source
    image = await c.prepare_image(frame, composed_evidence_image, frame, target['box'],
        Path(frame.image_path).with_name(f'grind-view-{frame.frame_id}.png'),
        history=((before['source_image'], before['source_hash'], 'BEFORE LINKED STANCE ACTION' if marked else 'BEFORE CAST — HISTORICAL', 36),
            (evidence['frame']['image_path'], evidence['sha256'], 'RETAINED PLAYER-STATE ERROR — HISTORICAL', 36)),
        player_badge=tuple(c.config['player_level_box']))
    if isinstance(image, CycleResult):
        return image
    options = [ActionCandidate('error_not_supported',
        'CURRENT own-player stance/readiness positively shows this specific cue resolved or unsupported. Faded text, elapsed time and input submission alone are insufficient. Resolve only this cue for scouting; no cast or retry credit.',
        {'type':'observe_only'}, precondition=valid)]
    if marked:
        options.extend([ActionCandidate('motion_useful',
            'The linked stand pulse completed and CURRENT own-player pose positively shows we are standing. This resolves only own stance; no target relation, useful translation, method renewal, navigation progress or cast retry.',
            {'type':'observe_only'}, precondition=valid), ActionCandidate('motion_no_useful_effect',
            'CURRENT own-player pose still supports the standing error or the stand pulse did not help. Charge the existing standing method; retain the error and every old target constraint.',
            {'type':'observe_only'}, precondition=valid)])
    else:
        options.append(ActionCandidate('retain_cast_error',
            'CURRENT own-player evidence still supports this cue, or readiness/stance cannot be resolved. Retain the exact constraint; no attack, fresh target attempt or reset.',
            {'type':'observe_only'}, precondition=valid))
        if error['kind'] in {'cooldown', 'interrupted'}:
            options.append(ActionCandidate('cast_ready',
                'CURRENT own Smite control is visibly ready and our player is no longer casting/interrupted, without conflicting current evidence. Resolve this cue for scouting only; no cast, probe or retry grant.',
                {'type':'observe_only'}, precondition=valid))
        binding = (c.profile.values['controls']['bindings'].get('forward') or {})
        if error['kind'] == 'standing' and binding.get('verified_from') and type(binding.get('keycode')) is int and h.motion_count('forward', 'standing', h.combat_history_key) < 2:
            movement_guard = c.travel_guard(frame, target, selected=False)
            async def guard(binding):
                checked = await movement_guard(binding)
                if not checked.approved:
                    return checked
                fresh = checked.dispatch_frame
                actual = await c.target_proposal(fresh)
                if not valid() or not _player_review_current(c, fresh, actual):
                    return DispatchValidation(False, detail='Released standing owner, absence or safety changed before dispatch')
                return checked
            options.append(ActionCandidate('stand_pulse',
                'CURRENT own player is visibly sitting/kneeling and this standing cue is supported. One safe0.08-second forward pulse to stand; then assess own pose. No target approach or cast grant.',
                {'type':'keypress', 'keycode':binding['keycode'], 'hold_seconds':.08}, precondition=valid, dispatch_guard=guard))
    result = await c.decide(frame, image,
        f'An authenticated clear released the selected creature; its life and prior damage remain unknown. This task is only our CURRENT own-player {error["kind"]} state, from retained cue {error["text"]!r}. No selected-target presence or life decision is needed. Historical message disappearance is not resolution. Standing motion debt: {h.motion_count("forward", "standing", h.combat_history_key)}/2; old failed methods remain unchanged.',
        'Choose the supported own-player stance/readiness action or assessment. Resolve only this exact cue for nonoffensive scouting; never claim target correction or grant a cast.',
        options, travel_budget=c.travel_budget, compact=True)
    receipt = result.receipt or {}; choice = getattr(result.decision, 'chosen', None)
    accepted = (result.status == 'dispatched' and receipt == c.cycle.last_receipt
        and h.known_completed_input({'receipt':receipt}))
    if (receipt.get('possible_input') or receipt.get('dispatch_unknown')) and not accepted:
        c.stop('partial_or_unknown_grind_input')
    elif accepted and choice == 'stand_pulse':
        if (h.cast_error is error and h.pending is pending and c.current()
                and (c.revision, c.cycle.session_epoch) == authority[:2]
                and receipt.get('generation_before') == authority[2]
                and receipt.get('generation_after') == c.cycle.input_generation):
            await c.apply_choice(frame, target, measurement, pending, False, result,
                {'family':'motion', 'purpose':'standing', 'action':'forward', 'scoped_question':True}, choice, c.level.last_confirmed_level)
            h.pending['released_error_stance'] = {'owner':h.combat_history_key, 'encounter_id':h.encounter,
                'cast_receipt_id':error['cast_receipt_id'], 'cue_key':cue_key(error),
                'clear_receipt_id':_selection_release(h)['clear']['receipt']['receipt_id']}
    elif accepted and not receipt.get('possible_input') and valid():
        disposition = {'cast_ready':'current_ready', 'error_not_supported':'current_resolved_or_unsupported', 'motion_useful':'own_stance_observed'}.get(choice)
        if marked and choice in {'motion_useful', 'motion_no_useful_effect', 'error_not_supported'}:
            release=_selection_release(h)
            if release.get('kind')=='selected_sensing':
                h.strategy_required['selected_exit']['own_state_transport']={'stance_source':deepcopy(pending),
                    'previous_generation':_typed_review_generation(h,release),
                    'outcome':choice,'assessment':{'frame_id':frame.frame_id,'captured_at':frame.captured_at,
                        'image_path':frame.image_path,'sha256':hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
                        'scope':capture_scope(c,frame),'input_generation':c.cycle.input_generation,'receipt':deepcopy(receipt)}}
            h.resolve(choice if choice.startswith('motion_') else 'unknown', frame, measurement, pending=pending, linked=True)
        if disposition:
            error['released_resolution'] = {'disposition':disposition, 'owner':h.combat_history_key,
                'encounter_id':h.encounter, 'cast_receipt_id':error['cast_receipt_id'], 'cue_key':cue_key(error),
                'clear_receipt_id':_selection_release(h)['clear']['receipt']['receipt_id'],
                'frame_id':frame.frame_id, 'captured_at':frame.captured_at, 'image_path':frame.image_path,
                'sha256':hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest(),
                'scope':capture_scope(c, frame), 'input_generation':c.cycle.input_generation,
                'receipt':deepcopy(receipt), 'stance_source':deepcopy(pending) if marked else None}
            c.event('grind_released_player_cue_resolved', {**error['released_resolution'], 'input_authority':False, 'cast_retry_granted':False})
            h.remember_approach()
    c.finish_question(result, {}); c.flush_outcomes()
    c.store.save_checkpoint('grind_only', {'hunt':h.context(), 'deadline':c.deadline, 'baseline_verified':c.baseline, 'level':c.level.last_confirmed_level})
    return result


def ended_target_error_allows_scouting(h):
    """A former target's error is not an idle hunting task or attack permission."""
    from sage_wow.agent.grind_search import target_identity
    if released_player_resolution(h) and not unresolved_nonlocal_cues(h):
        return True
    local=LOCAL_CUES
    error=h.cast_error or {};kind=error.get('kind')
    recent=h.recent_combat or {};source=error.get('assessment_source') or {}
    receipt=source.get('receipt') or {};history=h.target_history.get(h.combat_history_key) or {}
    evidence=source.get('position_error_evidence') or {}
    if (not h.encounter_ended or error.get('status')!='active' or kind not in local
            or h.cast_obligation!=f'Current linked {kind} error requires observed correction'
            or h.input_effect_unverified or not h.known_completed_input(source) or not receipt.get('possible_input')
            or not h.known_completed_input(recent) or not (recent.get('receipt') or {}).get('possible_input')
            or error.get('encounter_id')!=h.encounter or source.get('encounter_id')!=h.encounter
            or history.get('encounter_id')!=h.encounter
            or source.get('target_history_key')!=h.combat_history_key
            or error.get('cast_receipt_id')!=receipt.get('receipt_id')
            or receipt.get('receipt_id')!=(recent.get('receipt') or {}).get('receipt_id')
            or target_identity((source.get('target') or {}).get('name'))!=h.last_target
            or not source.get('source_scope')
            or source['source_scope'].get('session_epoch')!=receipt.get('session_epoch')
            or any(source.get(k)!=recent.get(k) for k in
                ('source_frame_id','source_image','source_hash','source_scope','target_continuity','target_history_key'))
            or cue_key(error) not in {cue_key(cue) for cue in evidence.get('cues',[])}):
        return False
    if unresolved_nonlocal_cues(h):
        return False
    try:
        return (hashlib.sha256(Path(source['source_image']).read_bytes()).hexdigest()==source['source_hash']
            and hashlib.sha256(Path(evidence['frame']['image_path']).read_bytes()).hexdigest()==evidence['sha256'])
    except (KeyError,OSError,TypeError):
        return False


def retained_error_candidate(c, frame, target):
    """Same-owner unresolved evidence, even during another pending receipt."""
    from sage_wow.agent.grind_encounter_recovery import capture_scope
    from sage_wow.agent.grind_search import target_identity
    h = c.hunt
    history = h.target_history.get(h.combat_history_key) or {}
    review = history.get('unassessed_error_feedback') or {}
    source = review.get('source') or {}
    receipt = source.get('receipt') or {}
    visual = target.get('visual_observation') or {}
    release = _selection_release(h)
    released = bool(release and release.get('disposition') in {'verified', 'closed'}
        and (release.get('kind') == 'selected_sensing' or release.get('combat_receipt_id') == receipt.get('receipt_id'))
        and _released_selection_current(c, frame, target))
    if (review.get('status') != 'unassessed'
            or h.blocked or h.input_effect_unverified or not c.fresh(frame) or c.cycle._scope_error(frame)
            or h.encounter_ended and not released or history.get('completed') or history.get('retired_by_acquisition')
            or source.get('encounter_id') != h.encounter
            or source.get('target_history_key') != h.combat_history_key
            or h.combat_history_key != (h.approach or {}).get('history_key')
            or not released and source.get('target_continuity') != h.target_continuity
            or not released and source.get('source_scope') != capture_scope(c, frame)
            or not released and receipt.get('session_epoch') != c.cycle.session_epoch
            or released and (not _attributed_source(h, source)
                or any(source['source_scope'].get(key) != capture_scope(c, frame).get(key)
                    for key in ('source', 'width', 'height')))
            or not receipt.get('possible_input') or not h.known_completed_input(source)
            or not released and target_identity(target.get('name')) != target_identity((source.get('target') or {}).get('name'))
            or target.get('self_target') or target.get('invalid_text')
            or not released and (visual.get('selected_hud') != 'present' or visual.get('life_state') != 'alive'
                or target.get('eligibility') != 'eligible')):
        return None
    cues = unresolved_cues(source, nonlocal_only=released)
    if not cues:
        return None
    evidence = source.get('position_error_evidence') or {}
    try:
        if (hashlib.sha256(Path(source['source_image']).read_bytes()).hexdigest() != source['source_hash']
                or hashlib.sha256(Path(evidence['frame']['image_path']).read_bytes()).hexdigest() != evidence['sha256']
                or datetime.fromisoformat(frame.captured_at) <= datetime.fromisoformat(receipt['occurred_at'])):
            return None
    except (OSError, KeyError, ValueError, TypeError):
        return None
    return review


def restore_released_acquisition(c, frame, target):
    """Restore only the existing menu stage after a closed owner's constraints resolve."""
    from sage_wow.agent.grind_hunt_entry import idle_acquisition
    from sage_wow.agent.grind_resources import hud_resources
    from sage_wow.agent.grind_encounter_recovery import capture_scope
    h = c.hunt; clear = h.intentional_clear_current() or {}; recent = h.recent_combat or {}
    physical = clear.get('clear') or {}; source_scope = recent.get('source_scope') or {}
    if (h.phase != 'search' or h.planning_requested or (h.plan or {}).get('phase') == 'travel'
            or (h.strategy_required or {}).get('selected_exit') or not h.encounter_ended
            or h.pending is not None or clear.get('disposition') != 'closed'
            or clear.get('owner') != h.combat_history_key or clear.get('encounter_id') != h.encounter
            or (h.approach or {}).get('history_key') != clear.get('owner')
            or not h.intentional_clear_explains(recent) or not _attributed_source(h, recent)
            or not h.known_completed_input(physical) or not (physical.get('receipt') or {}).get('possible_input')
            or not _intact_image(physical.get('source_image'), physical.get('source_hash'))
            or not _intact_image((clear.get('assessment') or {}).get('image_path'),
                (clear.get('assessment') or {}).get('sha256'))
            or not c.current() or not c.fresh(frame) or c.cycle._scope_error(frame) or c.require_world
            or any(source_scope.get(key) != capture_scope(c, frame).get(key)
                for key in ('source', 'width', 'height'))):
        return False
    hud = target.get('hud') or {}
    if hud.get('frame_id') != frame.frame_id:hud = hud_resources(c, frame)
    current = {**target, 'hud':hud}
    if not h.attributed_selection_absent(current, frame) or not idle_acquisition(c, current, h.pending, frame):
        return False
    h.compact_stage = 'acquire'
    return True


def interrupted_candidate(c, frame, target):
    """Only assess retained evidence after the current action is reconciled."""
    release = _selection_release(c.hunt)
    released = bool(release and release.get('disposition') in {'verified', 'closed'}
        and _released_selection_current(c, frame, target))
    if c.hunt.pending is not None or (c.hunt.cast_error or {}).get('status') == 'active' and not released:
        return None
    return retained_error_candidate(c, frame, target)


async def reassess_interrupted(c, frame, target, measurement=None):
    """Observation-only review; never relink or reinstall the retired physical receipt."""
    from sage_wow.agent.grind_only import composed_evidence_image
    restore_released_acquisition(c, frame, target)
    player_review = await reassess_released_player(c, frame, target, measurement or c.hunt.last_measurement)
    if player_review is not None:
        return player_review
    review = interrupted_candidate(c, frame, target)
    if not review:
        return None
    source = review['source']
    release = _selection_release(c.hunt)
    released = bool(release and release.get('disposition') in {'verified', 'closed'}
        and _released_selection_current(c, frame, target))
    cue = unresolved_cues(source, nonlocal_only=released)[0]
    epoch_generation = (c.cycle.session_epoch, c.cycle.input_generation)
    valid = lambda: (interrupted_candidate(c, frame, target) is review
        and epoch_generation == (c.cycle.session_epoch, c.cycle.input_generation))
    identity = str(('interrupted_cast_error', source['receipt']['receipt_id'], cue_key(cue)))
    if not c.select_question(identity, 'feedback'):
        return CycleResult('grind_blocked', detail='Interrupted error assessment capacity retained')
    evidence = source['position_error_evidence']
    image = await c.prepare_image(frame, composed_evidence_image, frame, target['box'],
        Path(frame.image_path).with_name(f'grind-view-{frame.frame_id}.png'),
        history=((source['source_image'], source['source_hash'], 'BEFORE INTERRUPTED CAST — HISTORICAL', 36),
            (evidence['frame']['image_path'], evidence['sha256'], 'RETAINED POSTCAST ERROR — HISTORICAL', 36)),
        player_badge=tuple(c.config['player_level_box']))
    if isinstance(image, CycleResult):
        return image
    options = [ActionCandidate('retain_cast_error',
        f'The retained {cue["kind"]} cue {cue["text"]!r} still needs correction or reassessment for this combat owner. Damage may also have occurred. Retain it; no input or damage claim.',
        {'type': 'observe_only'}, precondition=valid),
        ActionCandidate('error_not_supported',
        f'This retained {cue["kind"]} cue {cue["text"]!r} is unsupported, unrelated, or the current evidence shows it resolved. Dispose only this cue; no retry credit or damage claim.',
        {'type': 'observe_only'}, precondition=valid)]
    result = await c.decide(frame, image,
        f'A UI or recovery interruption ended cast feedback before this error was assessed. The original receipt is historical. Current selected creature: {target.get("name")!r}; current HUD: {target.get("hud",{})}. A faded current message alone does not invalidate its retained image. '
        + ('An authenticated deliberate clear released selection; this global cue still needs its ordinary assessment/resource/capability resolution.' if released else ''),
        'Assess the retained cast error for this combat owner. Choose retain_cast_error or error_not_supported.',
        options, travel_budget=c.travel_budget, compact=True)
    receipt = result.receipt or {}; chosen = getattr(result.decision, 'chosen', None)
    if (result.status == 'dispatched' and receipt == c.cycle.last_receipt
            and receipt.get('completed') and not receipt.get('possible_input')
            and not receipt.get('error') and not receipt.get('dispatch_unknown') and valid()):
        if chosen == 'retain_cast_error':
            await c.apply_cast_error(cue, source, frame, result)
            review['status'] = 'assessed'
            if not c.hunt.no_mana and not released:c.hunt.phase = 'approach'
            c.hunt.compact_stage = 'inspect'
            c.hunt.planning_requested = False
            c.hunt.remember_approach()
        elif chosen == 'error_not_supported':
            source.setdefault('rejected_error_cues', []).append(cue_key(cue))
            if not unresolved_cues(source):
                review['status'] = 'dismissed'
        c.event('grind_interrupted_cast_error_assessed', {'choice': chosen,
            'original_receipt_id': source['receipt']['receipt_id'], 'cue': cue,
            'frame_id': frame.frame_id, 'input_authority': False})
    c.finish_question(result, {}); c.flush_outcomes()
    return result
