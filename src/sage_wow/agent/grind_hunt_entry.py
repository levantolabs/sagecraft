"""Observed arrival changes the question, never sends gameplay input."""
from copy import deepcopy


def idle_hunt_state(c, target, pending, frame):
    h = c.hunt
    hud = target.get('hud') or {}
    healthy = (hud.get('frame_id') == frame.frame_id and hud.get('player_health') is not None
        and hud['player_health'] > c.config['critical_health_threshold']
        and (hud.get('health_confidence') or 0) >= .8)
    return bool(healthy and pending is None
        and h.encounter_ended and not h.cast_blocks_acquisition() and not h.loot_request
        and not h.active_threat and not h.input_effect_unverified and not h.blocked
        and not c.heal_pending and not h.no_mana and not h.disengagement)


def idle_acquisition(c, target, pending, frame):
    h = c.hunt
    visual = target.get('visual_observation') or {}
    hud = target.get('hud') or {}
    # Historical presence can outlive its observer's input generation. Search
    # does not need an absence claim: current positive cues still require
    # inspection, while idle guards preserve unfinished combat/input work.
    selection_cue = (visual.get('selected_hud') == 'present' or target.get('name')
        or target.get('levels') or visual.get('name') or visual.get('level') is not None
        or (hud.get('target_health') is not None
            and (hud.get('target_health_confidence') or 0) >= .8))
    return bool(not selection_cue and idle_hunt_state(c, target, pending, frame))


def refresh_arrival(c, measurement):
    """Keep the historical arrival through missing OCR, not conflicting facts.

    This record informs context only. Every input still requires a fresh frame
    and its normal guards; it is never a substitute for travel-entry evidence.
    """
    h = c.hunt
    fact = h.hunt_arrival
    if not fact:
        return
    from sage_wow.agent.grind_search import zone_identity
    zone = zone_identity(measurement)
    geometry = h.current_geometry(measurement)
    reason = None
    if fact['session_epoch'] != c.cycle.session_epoch:
        reason = 'session_or_focus_changed'
    elif fact['destination_request_id'] != (h.plan or {}).get('request_id'):
        reason = 'destination_changed'
    elif len(zone) == 1 and tuple(fact['zone']) != zone:
        reason = 'location_conflict'
    elif geometry.get('available') and not geometry.get('inside_radius'):
        reason = 'observed_departure'
    elif h.input_effect_unverified:
        reason = 'unreconciled_input'
    elif (h.pending and h.pending.get('family') == 'motion'
        and not h.pending.get('action', '').startswith('turn_')
        and h.pending.get('receipt', {}).get('possible_input')
        and not geometry.get('available')):
        reason = 'unobserved_local_translation'
    elif (h.last_completed_action or {}).get('receipt_id') != fact['last_movement_receipt']:
        move = h.last_completed_action or {}
        if not move.get('attributable_pair'):
            reason = 'unobserved_movement'
    if reason:
        h.hunt_arrival = None
        c.event('grind_hunt_arrival_invalidated', {'reason': reason, 'arrival': fact})


def route_arrival(c, frame, target, measurement):
    from sage_wow.agent.grind_travel_scout import active, transfer
    h = c.hunt
    if (active(c) or h.phase != 'travel' or h.pending or h.blocked or h.input_effect_unverified
        or h.active_threat or h.cast_blocks_acquisition() or h.loot_request or not h.encounter_ended
        or h.disengagement or c.heal_pending or h.no_mana
        or not c.current() or not c.fresh(frame) or c.cycle._scope_error(frame)):
        return False
    geometry = h.current_geometry(measurement)
    if not geometry.get('available') or not geometry.get('inside_radius'):
        return False
    entry = h.travel_search_evidence(frame, measurement, c.cycle.session_epoch)
    displaced = bool(entry)
    if displaced:
        # Only independently linked displacement renews the local search ledger.
        h.meaningful_sector_change(entry)
    else:
        # A run can begin inside its chosen region. This establishes where to
        # search, not travel progress or a new allowance for failed methods.
        if (measurement.get('frame_id') != frame.frame_id
            or measurement.get('captured_at') != frame.captured_at):
            return False
        from sage_wow.agent.grind_search import zone_identity
        entry = {'outcome': 'observed_current_hunting_location',
            'frame_id': frame.frame_id, 'session_epoch': c.cycle.session_epoch,
            'position_after': deepcopy(measurement['position']),
            'zone': list(zone_identity(measurement)), 'search_revision': h.search_revision,
            'displacement_observed': False, 'progress_credited': False}
    h.hunt_arrival = {**deepcopy(entry), 'area_id': h.plan['area_id'],
        'destination_request_id': h.plan['request_id'], 'captured_at': frame.captured_at,
        'last_movement_receipt': (h.last_completed_action or {}).get('receipt_id'),
        'active_at': h.active_seconds, 'first_target_receipt': None,
        'input_authorized': False}
    h.phase = 'search'; h.suspended = True; h.planning_requested = False
    transfer(c, 'validated_arrival')
    h.compact_stage = 'inspect' if target.get('name') else 'acquire'
    if displaced:
        c.event('grind_search_sector_changed', entry)
    c.event('grind_hunting_arrival', h.hunt_arrival)
    return True


def acquisition_context(c, level):
    from sage_wow.agent.grind_progression import policy
    h = c.hunt
    low, high = h.target_band(level)
    entry = policy(h, level) or {}
    primary = entry.get('primary_targets')
    targets = ('Primary creatures: ' + ', '.join(primary) + '. ') if primary else ''
    if entry.get('opportunistic_targets'):
        targets += 'Configured selected opportunistic creatures: '+', '.join(entry['opportunistic_targets'])+'; consider only on freshly established primary ground. '
    opener = ('A valid fresh selection immediately receives one opening Smite from the harness; '
        'you choose subsequent combat actions. ' if c.config.get('target_opening_cast') else
        'After selection, assess the fresh target before attacking. ')
    return (f'World of Warcraft. Our priest is level {level}. Goal: reach level {c.config["goal_level"]} by hunting. '
        f'{targets}Allowed creature levels: {low} through {high}. '
        'Find and select a hunting candidate in the CURRENT world view. '
        'Select a nearby enemy, or change the viewpoint to find one. '
        + opener + 'The harness checks the selected creature and player state before any opening attack. '
        'Hunt suitable creatures in this patch; relocate after local search finds no suitable candidates. '
        'Passive chat and the normal game HUD do not block actions.')
