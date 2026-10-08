"""Optional travel targeting, with one shared allowance and assessment owner.

The retained patch is factual history. Its unfinished task flag owns inspection
after receipt archival; neither field restores an input receipt or an opener.
"""
import asyncio
from copy import deepcopy
from datetime import datetime
import math
import time

from sage_wow.agent.cycle import ActionCandidate, DispatchValidation
from sage_wow.agent.grind_hunt_entry import idle_acquisition
from sage_wow.agent.grind_observation import digest
from sage_wow.agent.grind_search import measure, ui_evidence, zone_identity, VECTOR_ERROR, PRECISION_EPSILON


def travel_owned(c):
    return bool(c.hunt.plan and c.hunt.plan.get('phase') == 'travel')


def active(c):
    record = c.hunt.travel_policy.get('last_scout') or {}
    return bool(record.get('unfinished'))


def threat_current(c, frame):
    """Actual current attack owns ordinary targeting, never a recovery label."""
    from sage_wow.agent.grind_encounter_recovery import capture_scope, disengaging
    threat = c.hunt.active_threat or {}
    return bool(threat.get('kind') in {'current_own_health_loss', 'current_incoming_attack'}
        and threat.get('scope') == capture_scope(c, frame)
        and 0 <= time.time() - threat.get('occurred_at', 0) <= 10
        and c.current() and c.fresh(frame) and not c.cycle._scope_error(frame)
        and not disengaging(c))


def transfer(c, reason, *, search=True):
    """An accepted task choice retires return, but retains the consumed patch."""
    record = c.hunt.travel_policy.get('last_scout')
    if record and record.get('unfinished'):
        record.update(unfinished=False, resolution=reason, input_authority=False)
        c.event('grind_travel_scout_transferred', deepcopy(record))
    if search and c.hunt.plan:
        c.hunt.plan['phase'] = 'search'


def restore_phase(c, prior=None, *, fallback='search'):
    if active(c):
        return 'search'
    if prior in {'fight', 'approach', 'recover', 'choose_area'}:
        return prior
    if travel_owned(c):
        return 'travel'
    if prior == 'travel':
        return fallback
    return prior or fallback


def route(c):
    """Keep unfinished inspection ahead of generic planning/travel aliases."""
    if not active(c):
        return False
    h = c.hunt
    record = h.travel_policy['last_scout']
    if (record['plan_request_id'] != (h.plan or {}).get('request_id')
            or not travel_owned(c)):
        transfer(c, 'superseded_task', search=False)
        return False
    h.phase = 'search'; h.suspended = True; h.planning_requested = False
    if h.compact_stage not in {'inspect', 'reinspect'}:
        h.compact_stage = 'inspect'
    return True


def readable(frame, measurement):
    return bool(measurement.get('frame_id') == frame.frame_id
        and measurement.get('captured_at') == frame.captured_at
        and measurement.get('position_status') == 'readable_proposal'
        and measurement.get('position') and len(zone_identity(measurement)) == 1)


def absence_conflicts(target):
    visual = target.get('visual_observation') or {}; hud = target.get('hud') or {}
    return bool(target.get('name') or target.get('levels') or visual.get('selected_hud') == 'present'
        or visual.get('name') or visual.get('level') is not None
        or target.get('self_target') or target.get('invalid_text') or target.get('observation_conflict')
        or (hud.get('target_health') is not None and (hud.get('target_health_confidence') or 0) >= .8))


def allowance(c, frame, measurement):
    h = c.hunt
    if not h.target_selection_available() or active(c) or not readable(frame, measurement):
        return False
    last = h.travel_policy.get('last_scout')
    if not last:
        return True
    if (last['frame_source'] != frame.source or last['frame_size'] != [frame.width, frame.height]
            or tuple(last['zone']) != zone_identity(measurement)
            or math.dist(last['position'], measurement['position']) <= VECTOR_ERROR + PRECISION_EPSILON):
        return False
    entry = h.travel_search_evidence(frame, measurement, c.cycle.session_epoch)
    move = h.last_completed_action or {}
    if (not entry or not entry.get('source_captured_at')
            or entry['receipt_id'] != move.get('receipt_id')
            or move.get('search_generation', move.get('generation_after')) != c.cycle.input_generation):
        return False
    # A fresh chain may start beyond the old patch after a focus/OCR break. The
    # unobserved gap earns no credit: the current chain must itself translate.
    return bool(datetime.fromisoformat(entry['source_captured_at'])
        > datetime.fromisoformat(last['completed_at'])
        and entry['displacement'] > VECTOR_ERROR + PRECISION_EPSILON)


def offered(c, frame, target, measurement):
    h = c.hunt; hud = target.get('hud') or {}
    from sage_wow.agent.grind_relocation import typed_release
    intent=(h.strategy_required or {}).get('selected_exit') or {}
    # An exhausted unknown question supplies no absence proof, but cannot own
    # routing forever. Fresh idle/positive-cue and physical guards still apply.
    exit_released = not intent or typed_release(h) or intent.get('disposition')=='assessment_exhausted'
    return bool(travel_owned(c) and h.phase in {'travel', 'search'}
        and c.current() and c.fresh(frame, travel=c.travel_budget is not None)
        and not c.cycle._scope_error(frame) and not c.require_world
        and not target.get('self_target') and not target.get('invalid_text')
        and not target.get('observation_conflict')
        and exit_released
        and idle_acquisition(c, target, h.pending, frame)
        and hud['player_health'] > max(c.config['critical_health_threshold'], c.config['heal_health_fraction'])
        and hud.get('player_mana') != 0
        and allowance(c, frame, measurement))


def candidate(c, frame, target, measurement, valid=lambda: True):
    """Return the existing Tab candidate and its final-dispatch snapshot.

    Both travel and generic acquisition use this function. The precondition
    switches to the guard's fresh snapshot after validation, so the cycle's
    final synchronous precondition checks current ownership and allowance too.
    """
    if not offered(c, frame, target, measurement):
        return None
    binding = c.profile.values['controls']['bindings'].get('target_enemy') or {}
    if not binding.get('verified_from') or type(binding.get('keycode')) is not int:
        return None
    state = {'offer_frame': frame, 'offer_target': target, 'offer_measurement': measurement}
    authority = (c.revision, c.cycle.session_epoch, c.cycle.input_generation)
    task_basis = (deepcopy(c.hunt.plan), c.hunt.search_revision, deepcopy(c.hunt.strategy_required))
    source_hash = digest(frame)
    def precondition():
        current_frame = state.get('frame', frame)
        return bool(valid() and authority == (c.revision, c.cycle.session_epoch, c.cycle.input_generation)
            and task_basis == (c.hunt.plan, c.hunt.search_revision, c.hunt.strategy_required)
            and digest(frame) == source_hash
            and ('frame' not in state or digest(current_frame) == state['hash'])
            and offered(c, current_frame, state.get('target', target), state.get('measurement', measurement)))
    continuity = c.travel_guard(frame, target, selected=False)
    async def guard(source):
        checked = await continuity(source)
        if not checked.approved or not checked.dispatch_frame:
            return checked
        fresh = checked.dispatch_frame
        rows = await c.rows(fresh)
        actual = await c.target_proposal(fresh)
        from sage_wow.agent.grind_resources import hud_resources
        hud = await asyncio.to_thread(hud_resources, c, fresh)
        actual = {**actual, 'hud': hud}
        observation = await asyncio.to_thread(measure, c.profile, fresh, rows, c.ocr)
        from sage_wow.agent.grind_encounter_recovery import observe_threat
        observe_threat(c, fresh, actual)
        state.update(frame=fresh, hash=digest(fresh), target=actual, measurement=observation)
        predicates = {'ui_clear': not ui_evidence(c.profile, fresh, rows, {})['positive'],
            'current_scout': precondition(),
            'lease_current': c.travel_budget is not None and c.travel_budget.remaining() > 0}
        approved = all(predicates.values())
        return DispatchValidation(approved, fresh if approved else None,
            'Fresh travel scouting predicates verified' if approved else 'Travel scouting predicates changed',
            {**checked.evidence, 'scout_predicates': predicates, 'scout_position': observation.get('position'),
                'scout_zone': list(zone_identity(observation)), 'scout_frame_sha256': state['hash']})
    opening = (' If eligible, the harness attempts one opening Smite.'
        if c.config.get('target_opening_cast') else ' Assess the fresh selection before attacking.')
    return (ActionCandidate('target_enemy', 'Select a nearby hunting candidate with Tab while traveling.' + opening,
        {'type': 'keypress', 'keycode': binding['keycode'], 'hold_seconds': .08},
        precondition=precondition, dispatch_guard=guard), state)


async def accept(c, result, state, level):
    """Use ordinary target installation, then atomically hand off to inspection."""
    receipt = result.receipt or {}
    if ('frame' not in state or result.status != 'dispatched' or receipt != c.cycle.last_receipt
            or not receipt.get('possible_input') or not c.hunt.known_completed_input({'receipt': receipt})
            or receipt.get('dispatch_frame_id') != state['frame'].frame_id
            or receipt.get('source_frame_id') != state['offer_frame'].frame_id):
        raise RuntimeError('Completed travel scout lacks its final validated dispatch snapshot')
    h = c.hunt; frame = state['frame']; measurement = state['measurement']
    record = {'receipt_id': receipt['receipt_id'], 'completed_at': receipt['occurred_at'],
        'source_frame_id': frame.frame_id, 'source_image': frame.image_path, 'source_hash': state['hash'],
        'source_captured_at': frame.captured_at, 'frame_source': frame.source,
        'frame_size': [frame.width, frame.height], 'position': deepcopy(measurement['position']),
        'zone': list(zone_identity(measurement)), 'session_epoch': receipt['session_epoch'],
        'search_revision': h.search_revision, 'plan_request_id': h.plan['request_id'],
        'strategy': deepcopy(h.strategy_required), 'unfinished': True, 'input_authority': False}
    await c.apply_choice(state['offer_frame'], state['offer_target'], state['offer_measurement'], None, False, result,
        {'family': 'target', 'action': 'target_enemy', 'purpose': 'target'}, 'target_enemy', level)
    h.travel_policy['last_scout'] = record
    h.phase = 'search'; h.compact_stage = 'inspect'; h.suspended = True; h.planning_requested = False
    if h.pending:
        h.pending['travel_scout_receipt_id'] = receipt['receipt_id']
    c.event('grind_travel_scout_started', deepcopy(record))


def finish_assessment(c, frame, target, data):
    """Current absence ends responsibility, never rewrites an archived outcome."""
    if not active(c) or not (data.get('absence') or data.get('cleared')):
        return False
    h = c.hunt; record = h.travel_policy['last_scout']
    hud = target.get('hud') or {}
    if (h.pending or not c.current() or not c.fresh(frame) or c.cycle._scope_error(frame)
            or c.require_world or h.blocked or h.input_effect_unverified
            or c.heal_pending or h.no_mana or h.loot_request or h.disengagement
            or not h.encounter_ended or h.cast_blocks_acquisition()
            or hud.get('frame_id') != frame.frame_id or hud.get('player_health') is None
            or hud['player_health'] <= max(c.config['critical_health_threshold'], c.config['heal_health_fraction'])
            or (hud.get('health_confidence') or 0) < .8 or hud.get('player_mana') == 0
            or absence_conflicts(target)):
        return False
    record.update(unfinished=False, resolution='current_absence', assessment_frame_id=frame.frame_id,
        assessment_captured_at=frame.captured_at)
    if (not h.active_threat and travel_owned(c) and record['plan_request_id'] == h.plan['request_id']
            and record['search_revision'] == h.search_revision and record['strategy'] == h.strategy_required):
        h.phase = 'travel'; h.suspended = False; h.planning_requested = False; h.compact_stage = 'acquire'
        # Do not restore a historical recovery basis invalidated by Tab/clear.
        if h.recovery_requested:
            h.recovery_requested.pop('travel_task', None)
        c.event('grind_travel_scout_resumed', deepcopy(record))
        return True
    c.event('grind_travel_scout_resolved', deepcopy(record))
    return False
