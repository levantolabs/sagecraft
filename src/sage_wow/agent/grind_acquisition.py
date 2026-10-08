"""One user-authorized opener after Sage's existing targeting action.

Creature-name knowledge is type information, never individual identity, current
life/range, or permission to replay an earlier input. Every opening needs a new
completed targeting receipt and current, unambiguous calibrated HUD evidence.
"""
from copy import deepcopy
import asyncio
from datetime import datetime
import hashlib
from pathlib import Path
import time

from sage_wow.control.target_names import plain_target_name


def name_key(name):
    return ' '.join(str(name or '').casefold().split())


def learn_creature(c, fact):
    """Learn only type labels from attributed native damage/kill events."""
    if (not fact.get('own_source') or fact.get('event') not in
            {'PARTY_KILL', 'SPELL_DAMAGE', 'SWING_DAMAGE', 'RANGE_DAMAGE'}
            or not str(fact.get('dest_guid', '')).startswith('Creature-')
            or not plain_target_name(fact.get('dest_name'))):
        return False
    catalog = getattr(c, 'opening_creatures', None)
    if catalog is None:
        catalog = c.opening_creatures = {}
    name = name_key(fact['dest_name'])
    if name not in catalog and len(catalog) >= 128:
        return False
    catalog[name] = {key: fact.get(key) for key in
        ('dest_name', 'dest_guid', 'event', 'timestamp', 'source_guid', 'source_name')}
    return True


def load_creatures(c):
    from sage_wow.agent.grind_combat_log import parse_line
    c.opening_creatures = {}
    character = c.profile.values['character']
    names = {name_key(character['name']), name_key(' '.join(
        str(character.get(k) or '') for k in ('name', 'surname')))}
    for line in c.config.get('opening_creature_evidence', []):
        fact = parse_line(line)
        if not fact:
            raise ValueError('Opening creature evidence must be native combat log records')
        fact['own_source'] = name_key(fact['source_name'].split('-', 1)[0]) in names
        if not learn_creature(c, fact):
            raise ValueError('Opening creature evidence must attribute own damage/kill to a Creature GUID')


def acquisition_proof(c, frame, pending, linked):
    """A completed, current Tab permits assessment, not a claim of a new mob."""
    if (not pending or not linked or not c.fresh(frame) or c.cycle._scope_error(frame)
            or c.hunt.input_effect_unverified or c.hunt.blocked
            or pending.get('family') != 'target' or pending.get('action') != 'target_enemy'
            or not c.hunt.known_completed_input(pending)):
        return None
    receipt = pending['receipt']
    if (not receipt.get('possible_input')
            or receipt.get('session_epoch') != c.cycle.session_epoch
            or receipt.get('generation_after') != c.cycle.input_generation):
        return None
    return {k: receipt[k] for k in
        ('receipt_id', 'request_id', 'session_epoch', 'generation_after')}


def reassess_acquisition(c, target, proof):
    """Archive old failure state; a new local attempt is not a new creature GUID."""
    h = c.hunt
    if getattr(h, 'last_reassessed_acquisition', None) == proof['receipt_id']:
        return
    old = deepcopy(h.approach_for(target, renew_completed=False))
    if old:
        h.target_history[old['key']]['retired_by_acquisition'] = proof['receipt_id']
    h.renew_attempt(target)
    h.last_reassessed_acquisition = proof['receipt_id']
    c.event('grind_acquisition_reassessed', {**proof,
        'target_name': target['name'], 'prior_attempt': old,
        'new_history_key': h.approach['history_key'],
        'claim': 'Fresh local cast attempt; prior errors retained, physical creature identity unknown'})


def local_target(c, frame, target, hud, *, diagnostics=None):
    """Positive local facts, with explicit rejection reasons for opener timing."""
    known = getattr(c, 'opening_creatures', {}).get(name_key(target.get('name')))
    row = target.get('name_row'); box = c.config.get('target_name_box')
    visual = target.get('visual_observation') or {}
    levels = target.get('levels', [])
    level = levels[0] if len(levels) == 1 else None
    low, high = c.hunt.target_band(c.level.last_confirmed_level or 1)
    allowed_names = c.config.get('target_names', [])
    contained = False
    if row and box:
        b = row.bounds; overlay = target['box']
        contained = (overlay[0] <= b['x'] and overlay[1] <= b['y']
            and b['x'] + b['width'] <= overlay[2]
            and b['y'] + b['height'] <= overlay[3]
            and box[0] <= b['x'] + b['width']/2 <= box[2]
            and box[1] <= b['y'] + b['height']/2 <= box[3])
    predicates = {
        'opener_enabled': bool(c.config.get('target_opening_cast', False)),
        'no_observation_conflict': not target.get('observation_conflict'),
        'known_creature_type': bool(known),
        'name_ocr_confident': bool(row and row.confidence >= .85),
        'name_region': bool(contained),
        'literal_name': bool(plain_target_name(target.get('name'))),
        'not_self': not target.get('self_target'),
        'valid_target_text': not target.get('invalid_text'),
        'one_numeric_level': level is not None,
        'allowed_level': level is not None and low <= level <= high,
        'allowed_name': not allowed_names or name_key(target.get('name')) in {name_key(n) for n in allowed_names},
        'not_player_or_friendly': visual.get('target_kind') not in {'player', 'friendly_or_self'},
        'not_observed_absent': visual.get('selected_hud') != 'absent',
        'not_observed_dead': visual.get('life_state') != 'dead',
        'current_hud': hud.get('frame_id') == frame.frame_id,
        'living_bar': hud.get('target_health') is not None and hud['target_health'] > 0,
        'health_confident': (hud.get('target_health_confidence') or 0) >= .8,
    }
    if diagnostics is not None:
        diagnostics.update(predicates=predicates,
            failed_predicates=[name for name, passed in predicates.items() if not passed])
    if not all(predicates.values()):
        return None
    return {**target, 'eligibility': 'eligible', 'hud': hud,
        'visual_observation': {'selected_hud': 'present', 'name': target['name'],
            'level': level, 'target_kind': 'creature', 'life_state': 'alive'},
        'visual_provenance': {'backend': 'native_creature_type_and_current_HUD',
            'frame_id': frame.frame_id, 'type_evidence': dict(known),
            'life_and_level_source': 'current calibrated HUD; historical GUID is not current identity'}}


def arm(c, result):
    if not c.config.get('target_opening_cast', False):
        return
    receipt = result.receipt or {}
    if (result.status != 'dispatched' or not result.decision
            or result.decision.chosen != 'target_enemy' or receipt != c.cycle.last_receipt
            or not c.hunt.known_completed_input({'receipt': receipt})
            or not receipt.get('possible_input')):
        return
    arrival = c.hunt.hunt_arrival
    if arrival and not arrival['first_target_receipt']:
        arrival['first_target_receipt'] = receipt['receipt_id']
        c.event('grind_arrival_targeted', {'arrival_frame_id': arrival['frame_id'],
            'target_receipt_id': receipt['receipt_id'], 'area_id': arrival['area_id'],
            'arrival_to_target_seconds': datetime.fromisoformat(receipt['occurred_at']).timestamp()
                - datetime.fromisoformat(arrival['captured_at']).timestamp()})
    c.target_opener = {'receipt': deepcopy(receipt), 'revision': c.revision,
        'deadline': datetime.fromisoformat(receipt['occurred_at']).timestamp() + 10}
    c.event('grind_target_opener_armed', {'target_receipt_id': receipt['receipt_id'],
        'request_id': receipt['request_id'], 'authorization': 'user_requested_target_then_one_cast'})


async def attempt(c, frame, target, measurement, pending, linked):
    token = getattr(c, 'target_opener', None)
    if token is None:
        return None
    # Consume before any await. A pause, error, or reprocessed frame cannot replay it.
    c.target_opener = None
    proof = acquisition_proof(c, frame, pending, linked)
    receipt = token['receipt']
    source_hash = hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest()
    def valid():
        return (c.current() and c.revision == token['revision'] and time.time() < token['deadline']
            and c.hunt.pending is pending and pending is not None
            and pending['receipt'] == receipt
            and acquisition_proof(c, frame, pending, c.hunt.linked(frame, c.cycle.input_generation)) == proof
            and proof is not None and proof['receipt_id'] == receipt['receipt_id']
            and hashlib.sha256(Path(frame.image_path).read_bytes()).hexdigest() == source_hash)
    def fallback(reason):
        c.event('grind_target_opener_result', {'target_receipt_id': receipt['receipt_id'],
            'outcome': 'returned_to_sage', 'reason': reason, 'frame_id': frame.frame_id})
        return None
    if not valid():
        return fallback('targeting authority expired or changed')
    from sage_wow.agent.grind_progression import deferred, selected_policy, selected_context, dispatch_basis
    if deferred(c,target,frame,measurement):
        return fallback('primary hunting strategy: '+selected_context(selected_policy(c,target,frame,measurement)))
    from sage_wow.agent.grind_resources import hud_resources
    hud = hud_resources(c, frame)
    diagnostics = {}
    detected = local_target(c, frame, target, hud, diagnostics=diagnostics)
    c.event('grind_target_opener_local_check', {'target_receipt_id': receipt['receipt_id'],
        'frame_id': frame.frame_id, 'target_name': target.get('name'), **diagnostics})
    if detected is None:
        return fallback('current eligible creature not established locally')
    if (hud.get('player_health') is None or (hud.get('health_confidence') or 0) < .8
            or hud['player_health'] <= c.config['critical_health_threshold']):
        return fallback('own resources need Sage assessment')
    dispatch_policy = {}
    guard = await c.guard(frame, detected, exact_level=detected['levels'][0], strategy_state=dispatch_policy)
    checked = await guard(frame)
    c.event('grind_target_opener_guard', {'target_receipt_id': receipt['receipt_id'],
        'approved': checked.approved, 'detail': checked.detail, 'evidence': checked.evidence})
    if not checked.approved or not checked.dispatch_frame or not valid():
        return fallback('fresh attack guard rejected or authority changed')
    fresh = checked.dispatch_frame
    actual = await c.target_proposal(fresh)
    fresh_hud = hud_resources(c, fresh)
    final_diagnostics = {}
    final = local_target(c, fresh, actual, fresh_hud, diagnostics=final_diagnostics)
    c.event('grind_target_opener_final_check', {'target_receipt_id': receipt['receipt_id'],
        'frame_id': fresh.frame_id, 'target_name': actual.get('name'), **final_diagnostics})
    if (not valid() or final is None or final['levels'] != detected['levels']
            or name_key(final['name']) != name_key(detected['name'])
            or fresh_hud.get('player_health') is None
            or fresh_hud['player_health'] <= c.config['critical_health_threshold']
            or (fresh_hud.get('health_confidence') or 0) < .8):
        return fallback('current target or resources changed during validation')
    # The opener is a cast from the dispatch guard's newer frame, not from
    # the targeting observation. Retain its own baseline for all subsequent
    # resource/error comparisons; never relabel the earlier measurement.
    from sage_wow.agent.grind_search import measure
    fresh_rows = await c.rows(fresh)
    cast_measurement = await asyncio.to_thread(measure, c.profile, fresh, fresh_rows, c.ocr)
    cast_measurement['combat_hud'] = dict(fresh_hud)
    if (not valid() or not c.fresh(fresh) or c.cycle._scope_error(fresh)
            or dispatch_policy.get('basis') != dispatch_basis(c)
            or deferred(c,final,fresh,cast_measurement)
            or hashlib.sha256(Path(fresh.image_path).read_bytes()).hexdigest()!=dispatch_policy.get('hash')):
        return fallback('opening baseline preparation expired or authority changed')
    dispatch_policy['result']=selected_policy(c,final,fresh,cast_measurement)
    dispatch_policy['source_frame_id']=fresh.frame_id
    dispatch_started = time.time()
    result = await c.cycle.execute_target_opener(frame=fresh, target_name=final['name'],
        target_receipt=receipt, authorization_id=receipt['receipt_id'])
    cast = result.receipt or {}
    accepted = (result.status == 'dispatched' and cast == c.cycle.last_receipt
        and c.hunt.known_completed_input({'receipt': cast}) and cast.get('possible_input'))
    if cast.get('possible_input') and not accepted:
        c.stop('partial_or_unknown_grind_input')
    if accepted:
        h = c.hunt
        from sage_wow.agent.grind_progression import accepted_placement
        accepted_placement(c,final,cast,dispatch_policy,authenticate=True)
        acquired = h.resolve('target_acquired', frame, measurement, pending=pending, linked=True)
        if acquired:c.event('grind_action_outcome', {**acquired, 'request_id': receipt['request_id']})
        reassess_acquisition(c, final, proof)
        h.selected_presence = True
        h.compact_stage = 'inspect'
        h.eligible_inspection = {'frame_id': fresh.frame_id, 'image_path': fresh.image_path,
            'name_proposal': final['name'], 'level_proposals': final['levels'],
            'request_id': receipt['request_id'], 'authorization': 'user_requested_target_opener'}
        await c.apply_choice(fresh, final, cast_measurement, None, False, result,
            {'dispatch_policy':dispatch_policy,'action': 'cast', 'family': 'combat', 'purpose': 'cast', 'mob_level': final['levels'][0],
             'new_encounter': True, 'scoped_question': True}, 'target_opening_smite', c.level.last_confirmed_level)
        h.retain_burst_guard_evidence(cast)
        h.compact_last_result = 'target_opening_smite'
        c.target_inspection_episode = None
        c.flush_outcomes()
        c.store.save_checkpoint('grind_only', {'hunt': h.context(), 'deadline': c.deadline,
            'baseline_verified': c.baseline, 'level': c.level.last_confirmed_level})
    target_steps = receipt.get('input_steps') or []
    cast_text = next((step for step in cast.get('input_steps', []) if step.get('kind') == 'text'), None)
    command_latency = (cast_text['monotonic_at'] - target_steps[-1]['monotonic_at']
        if cast_text and target_steps and 'monotonic_at' in cast_text and 'monotonic_at' in target_steps[-1] else None)
    c.event('grind_target_opener_result', {'target_receipt_id': receipt['receipt_id'],
        'cast_receipt_id': cast.get('receipt_id'), 'outcome': result.status,
        'frame_id': fresh.frame_id, 'target_name': final['name'],
        'seconds_since_targeting': time.time() - datetime.fromisoformat(receipt['occurred_at']).timestamp(),
        'dispatch_started_at': dispatch_started, 'target_to_cast_command_seconds': command_latency,
        'claim': 'One opening command attempt; cast execution, damage and kill require observation'})
    return result
