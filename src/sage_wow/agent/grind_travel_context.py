"""Bounded factual travel questions; rendering never changes navigation state."""
from copy import deepcopy

from sage_wow.agent.grind_search import goal_relation, movement_result, normalized_zone, zone_identity

CORE_LIMIT = 2200
HISTORY_LIMIT = 700
NORMAL_LIMIT = 5200
RETRY_LIMIT = 4000


class TravelContextOverflow(ValueError):
    """Protected evidence does not fit; never silently strip its qualifications."""


def short(value, limit=64):
    text = ' '.join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def point(value):
    return 'unavailable' if value is None else f'[{value[0]:.1f}, {value[1]:.1f}]'


def historical_result(record, geometry, destination_zone):
    """Reproject original endpoints; never use a cached old-goal narrative."""
    if (not record or not record.get('attributable_pair')
            or record.get('action', '').startswith('turn_')
            or not record.get('position_before') or not record.get('position_after')):
        return None
    destination = geometry.get('destination') if (geometry.get('available')
        and tuple(record.get('zone_before', ())) == (destination_zone,)) else None
    return movement_result(record['position_before'], record['position_after'], destination)


def history_row(record, geometry, destination_zone):
    action = record['action']
    if action.startswith('turn_'):
        return f'{action}: {record.get("status", "unknown")}; no translation objective'
    result = historical_result(record, geometry, destination_zone)
    if result is None:
        return f'{action}: unknown/unlinked coordinate pair; no destination-progress claim'
    text = (f'{action}: {point(record["position_before"])}→{point(record["position_after"])}; '
        f'd={result["displacement"]:.2f}±{result["vector_error_bound"]:.2f}')
    if 'progress' in result:
        text += f'; current-goal Δ={result["progress"]:+.2f}±{result["progress_error_bound"]:.2f} {result["progress_status"]}'
    else:
        text += '; goal relation unavailable'
    return text


def build(c, anchor, measurement, geometry, situation, current_forward, candidates,
          withheld, *, question, retry=False, ordinary_retry=False, snapshot=None, relocation_context=''):
    """Return the exact bounded request and full audit provenance, without IO."""
    h = c.hunt
    area = (h.plan or {}).get('area', {})
    level = c.level.last_confirmed_level
    band = h.target_band(level)
    zone = normalized_zone(area.get('zone_reference') or '')
    ids = [candidate.option for candidate in candidates]
    last = h.last_completed_action
    result = historical_result(last, geometry, zone)
    policy = h.hunting_progression_by_player_level.get(level, {})
    examples = ', '.join(short(name, 48) for name in policy.get('primary_targets', [])[:3])
    preferred = h.preferred_target_bands_by_player_level.get(level)
    goal = (f'Goal: level {c.config["goal_level"]} by eligible kills; own level: {level}; '
        f'eligible levels: {band[0]}–{band[1]}. ')
    if examples:
        goal += f'Primary examples (not exhaustive): {examples}; full primary policy applies. '
    opportunistic = ', '.join(short(name,48) for name in policy.get('opportunistic_targets', [])[:3])
    if opportunistic:
        goal += f'Configured opportunistic targets: {opportunistic}; consider a selected one only on freshly established primary hunting ground under normal target checks. '
    if preferred:
        goal += f'Preferred levels: {preferred[0]}–{preferred[1]}; other allowed levels valid. '
    goal += (f'Destination: {short(area.get("label", "visible exploration"))}; '
        f'source kind: {short((area.get("source") or {}).get("kind", "unavailable"))}; search hypothesis only.')
    introduction = 'World of Warcraft. Current task: Reach the selected hunting area.'
    if relocation_context:
        introduction += (' Accepted task: Leave the unresolved selected encounter for this hunting destination. '
            'Its old selected HUD alone does not reopen the fight or establish a direction. '
            'Choose offered travel actions; an actual attacker or survival emergency still interrupts.')
    if retry:
        introduction = ('World of Warcraft. Current task: Reach the selected hunting area. '
            + ('Continue the hunting destination you already chose.' if ordinary_retry else
               'You chose to leave the unresolved selected encounter. Continue that relocation; its old portrait is not a direction marker.'))

    current = bool(current_forward)
    direction = current_forward['label'] if current else 'unavailable'
    relation = (goal_relation(current_forward['response'], geometry['vector'])
                if current and geometry.get('available') else 'unavailable')
    unavailable = h.travel_policy.get('mapping_unavailable_reason', 'no completed applicable forward measurement')
    forward = (f'Current measured forward direction: {direction}; Direction still current: {"yes" if current else "no"}; '
        f'destination relation: {relation}; '
        + (f'measured at {current_forward["response"]["captured_at"]}' if current else f'reason: {unavailable}; earlier heading is history'))
    location = (f'Current position: {point(geometry.get("position"))}; destination: {point(geometry.get("destination"))}; '
        f'actual zone: {short(", ".join(measurement.get("zone_proposals", [])) or "unavailable")}; '
        f'goal zone: {short(area.get("zone_reference") or "unavailable")}')
    actual_zones = zone_identity(measurement)
    same_zone = 'unresolved' if len(actual_zones) != 1 or not zone else 'yes' if actual_zones == (zone,) else 'no'
    location += f'; same-zone: {same_zone}'
    if geometry.get('available'):
        location += (f'; remaining vector: {point(geometry["vector"])}; distance: {geometry["distance"]:.2f}; '
                     f'Inside destination search radius: {"yes" if geometry["inside_radius"] else "no"}')
    else:
        location += '; same-zone geometry unavailable; no arrival claim'
    location += '; precision: coords 0.1, delta axes ±0.1, distance change ±0.14'
    movement = ('Last completed action: none' if not last else
        f'Last completed action: {last["action"]}; purpose: {last.get("purpose", "unknown")}; '
        f'{point(last.get("position_before"))}→{point(last.get("position_after"))}; '
        f'pair: {"attributable" if last.get("attributable_pair") else "unknown/unlinked"}; '
        f'result: {last.get("status", "unknown")}')
    progress = 'Historical destination progress: unavailable; no cached old-goal claim'
    if result and 'progress' in result:
        progress = (f'Historical action evaluated against current destination {point(geometry["destination"])}: '
            f'{result["progress"]:+.2f} ±{result["progress_error_bound"]:.2f} ({result["progress_status"]}); '
            f'displacement {result["displacement"]:.2f} ±{result["vector_error_bound"]:.2f}. No new progress credit.')
    elif last and last.get('action', '').startswith('turn_'):
        progress = 'Turn: no translation objective; no destination-progress claim'
    window = current_forward['response'].get('progress_window', []) if current else []
    accumulated = 'Linked forward movement: unavailable'
    if window and geometry.get('available'):
        net = movement_result(window[0]['position_before'], geometry['position'], geometry['destination'])
        accumulated = (f'Linked forward movement: {len(window)} completed actions; current-goal net '
            f'{net["progress"]:+.2f} ±{net["progress_error_bound"]:.2f} ({net["progress_status"]}); not a longer input grant')
    detour = h.detour or {}
    bypass = (f'Active detour: {detour.get("option", "none")}; current side: {h.travel_policy.get("side") or "none"}; '
        f'recorded side: {detour.get("side", "none")}; '
        f'original rationale: {short(detour.get("reason", "none"), 96)}; '
        + ('offered detour_forward tests one continuation after measured room-making; progress unproven'
           if 'detour_forward' in ids else 'no offered forward continuation'))
    debt = (f'Inconclusive probes: {h.inconclusive_probes(measurement, min(1., c.config["move_seconds"]))}; '
        f'local revisit flag: {bool(last and last.get("revisitation_cycle_suspected"))}; '
        f'strategy change pending: {bool(h.strategy_required)}; prior failed methods retained; no global loop claim')
    # Reason codes are generated from known withholding sites, not prose slicing.
    unavailable_ids = []
    for item in withheld:
        ident = item.split(':', 1)[0]
        reason = ('failed_approach' if 'repeated ineffective' in item else
            'unchanged_replan' if ident == 'change_destination' else
            'entry_gate' if ident == 'begin_hunt' else 'current_direction_gate' if ident == 'forward' else 'current_gate')
        unavailable_ids.append(f'{ident}={reason}')
    menu = f'Offered actions: {", ".join(ids) or "none"}; withheld: {", ".join(unavailable_ids) or "none"}'
    core = [goal, location, f'Decision needed: {situation}', forward, movement, progress,
            accumulated, bypass, debt, menu]
    if len('\n'.join(core)) > CORE_LIMIT:
        raise TravelContextOverflow('Protected travel core exceeds 2200 characters')
    history = []
    omitted = 0
    # Last action already has its own protected fields. Only optional history is dropped.
    for record in reversed(h.recent_moves[-5:]):
        if last and record.get('receipt_id') == last.get('receipt_id'):
            continue
        row = history_row(record, geometry, zone)
        if len(row) > 140 or sum(map(len, history)) + len(row) + len(history) > HISTORY_LIMIT:
            omitted += 1
        else:
            history.insert(0, row)
    lines = [introduction, *core, 'Recent movement history (historical endpoints; current-goal projection only):', *(history or ['none'])]
    if omitted:
        lines.append(f'Optional historical rows omitted: {omitted}; full records in audit.')
    if snapshot:
        lines.append(f'World view at {snapshot.hidden.captured_at}; restored current HUD at {anchor.captured_at}.')
    instructions = (question + '\n\nChoose one offered action from the current image and measured movement. '
        'Probes measure direction, not progress. Turns need new measurements; strafes do not establish forward. '
        'Use visible safe ground. Apparent obstruction is not proven blockage. Detours may temporarily increase distance. '
        'A tree or wall alone is not an emergency. Actual emergencies use offered recovery.')
    prompt = '\n'.join(lines)
    if len(introduction) > (220 if retry else 1000) or len(instructions) > 650 or len(prompt) + len(instructions) > (RETRY_LIMIT if retry else NORMAL_LIMIT):
        raise TravelContextOverflow('Bounded travel request exceeds its field or total limit')
    return {'context': prompt, 'instructions': instructions, 'candidate_ids': ids,
        'core': core, 'history': history, 'omitted_history_rows': omitted,
        'context_chars': len(prompt), 'instruction_chars': len(instructions),
        'core_chars': len('\n'.join(core)), 'retry': retry,
        'anchor': {'frame_id': anchor.frame_id, 'captured_at': anchor.captured_at},
        'audit': deepcopy({'measurement': measurement, 'geometry': geometry, 'current_forward': current_forward,
            'last_action': last, 'recent_moves': h.recent_moves, 'area': area,
            'target_preference': h.target_preference(level), 'detour': h.detour, 'withheld': withheld})}
