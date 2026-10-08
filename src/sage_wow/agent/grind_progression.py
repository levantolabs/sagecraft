"""Level-scoped hunting destinations and primary pulls, never target facts."""
from copy import deepcopy
import re
import math

from sage_wow.agent.grind_search import target_identity
from sage_wow.control.target_names import plain_target_name


def validate(value, catalog):
    if not isinstance(value, dict):
        raise ValueError('hunting_progression_by_player_level must be a mapping')
    result = {}
    for level, entry in value.items():
        if (type(level) not in (int, str) or not re.fullmatch(r'[1-9]|10', str(level))
                or int(level) in result or not isinstance(entry, dict)
                or not {'primary_targets', 'area_ids'} <= set(entry)
                or set(entry) - {'primary_targets', 'area_ids', 'opportunistic_targets'}):
            raise ValueError('Invalid hunting progression level or fields')
        names, areas = entry['primary_targets'], entry['area_ids']
        if (not isinstance(names, list) or not 1 <= len(names) <= 16
                or any(not isinstance(n, str) or not plain_target_name(n) for n in names)
                or len({target_identity(n) for n in names}) != len(names)
                or not isinstance(areas, list) or not 1 <= len(areas) <= 8
                or any(not isinstance(a, str) or a not in catalog for a in areas)
                or len(set(areas)) != len(areas)):
            raise ValueError('Hunting progression needs literal primary targets and known unique area IDs')
        opportunistic = entry.get('opportunistic_targets', [])
        if (not isinstance(opportunistic, list) or len(opportunistic) > 16
                or any(not isinstance(n, str) or not plain_target_name(n) for n in opportunistic)
                or len({target_identity(n) for n in opportunistic}) != len(opportunistic)
                or {target_identity(n) for n in opportunistic} & {target_identity(n) for n in names}):
            raise ValueError('Opportunistic targets need unique literal names separate from primary targets')
        result[int(level)] = {**deepcopy(entry), 'opportunistic_targets': deepcopy(opportunistic)}
    return result


def policy(hunt, level):
    return getattr(hunt, 'hunting_progression_by_player_level', {}).get(level)


def fixed_region_current(c, frame, measurement):
    """Geographic membership only; supplies no population or target authority."""
    from sage_wow.agent.grind_search import normalized_zone, zone_identity
    area=c.hunt.catalog.get(c.config.get('fixed_hunting_area')) or {}
    m=measurement or {}
    return bool(area.get('coordinate') and c.current() and c.fresh(frame)
        and not c.cycle._scope_error(frame) and m.get('frame_id')==frame.frame_id
        and m.get('captured_at')==frame.captured_at
        and m.get('coordinate_space')=='client_displayed_zone_coordinates'
        and m.get('position_status')=='readable_proposal' and m.get('position')
        and zone_identity(m)==(normalized_zone(area.get('zone_reference','')),)
        and math.dist(m['position'],area['coordinate'])<=area.get('arrival_radius',.6))


def primary(hunt, level, name):
    entry = policy(hunt, level)
    return not entry or target_identity(name) in {target_identity(n) for n in entry['primary_targets']}


def selected_policy(c, target, frame=None, measurement=None):
    """Species strategy and current ground are facts separate from cast eligibility."""
    h = c.hunt; level = c.level.last_confirmed_level or 1
    name = target_identity(target.get('name')); entry = policy(h, level)
    if primary(h, level, target.get('name')):
        return {'kind': 'primary'}
    if name not in {target_identity(n) for n in (entry or {}).get('opportunistic_targets', [])}:
        return {'kind': 'unsupported_species'}
    result = primary_region(c, frame, measurement)
    return {**result, 'kind': 'opportunistic_here'} if result['kind'] == 'inside_primary_region' else result


def primary_region(c, frame=None, measurement=None):
    """Current attributed geometry, independent of any selected creature."""
    from sage_wow.agent.grind_search import normalized_zone, zone_identity
    h = c.hunt; level = c.level.last_confirmed_level or 1
    entry = policy(h, level)
    m = measurement or {}
    provenance = {k: deepcopy(m.get(k)) for k in
        ('frame_id', 'captured_at', 'position', 'position_status', 'zone_proposals', 'coordinate_space')}
    result = {'kind': 'ground_unknown', 'measurement': provenance}
    if (not entry or frame is None or not c.current() or not c.fresh(frame) or c.cycle._scope_error(frame)
            or m.get('frame_id') != frame.frame_id or m.get('captured_at') != frame.captured_at
            or m.get('coordinate_space') != 'client_displayed_zone_coordinates'
            or m.get('position_status') != 'readable_proposal' or not m.get('position')
            or len(zone_identity(m)) != 1):
        return result
    regions = [h.catalog[ident] for ident in entry['area_ids'] if ident in h.catalog]
    # Only primary encounters published by encounter_seen may establish learned ground.
    regions += [area for area in h.learned.values()
        if area.get('source', {}).get('kind') == 'current_sage_combined_attack_choice'
        and area.get('source', {}).get('source_frame_ids')
        and any(primary(h, level, n) for n in area.get('observed_target_names', []))]
    for area in regions:
        if h.area_results.get(area['id'], {}).get('status') == 'depleted_or_unsupported_hypothesis':
            continue
        if (area.get('coordinate') and area.get('coordinate_space') == m['coordinate_space']
                and zone_identity(m) == (normalized_zone(area.get('zone_reference', '')),)
                and math.dist(m['position'], area['coordinate']) <= area.get('arrival_radius', .6)):
            return {**result, 'kind': 'inside_primary_region', 'region_id': area['id'],
                'region_source': deepcopy(area.get('source', {}))}
    return {**result, 'kind': 'outside_primary_region'}


def region_context(c, frame=None, measurement=None):
    if not policy(c.hunt, c.level.last_confirmed_level or 1):
        return ''
    result = primary_region(c, frame, measurement)
    if result['kind'] == 'inside_primary_region':
        area = c.hunt.catalog.get(result['region_id']) or c.hunt.learned.get(result['region_id']) or {}
        return (f' Current position is inside attributed primary hunting region '
            f'{area.get("label", result["region_id"])!r}. Inspect nearby candidates using the offered actions; '
            'region membership supplies no target identity, life or level evidence.')
    if result['kind'] == 'outside_primary_region':
        return ' Current position is outside the attributed primary hunting regions.'
    return ' Current primary hunting-region membership is unverified.'


def strategy_permitted(c, target, frame=None, measurement=None):
    result = selected_policy(c, target, frame, measurement)
    if result['kind'] in {'primary', 'opportunistic_here'}:
        return True
    h = c.hunt
    if not h.encounter_ended and target_identity(target.get('name')) == h.last_target:
        return True
    if frame is not None:
        from sage_wow.agent.grind_travel_scout import threat_current
        return threat_current(c, frame)
    return False


def deferred(c, target, frame=None, measurement=None):
    """Compatibility predicate; decisions use selected_policy for truthful reasons."""
    return not strategy_permitted(c, target, frame, measurement)


def selected_inspection(c, frame, target, pending, measurement=None):
    """Current selected work preempts mechanical placement, never an accepted exit."""
    visual = target.get('visual_observation') or {}
    selected_exit = (c.hunt.strategy_required or {}).get('selected_exit') or {}
    exit_active = bool(selected_exit and not (selected_exit.get('kind') == 'selected_sensing'
        and (selected_exit.get('superseded') or selected_exit.get('disposition') in {'closed','assessment_exhausted'})))
    return bool(policy(c.hunt,c.level.last_confirmed_level or 1)
        and not exit_active
        and not c.hunt.disengagement and not c.hunt.input_effect_unverified
        and not c.hunt.blocked and c.current() and c.fresh(frame) and not c.cycle._scope_error(frame)
        and not (pending and pending.get('family') != 'target')
        and target.get('eligibility') == 'eligible' and visual.get('selected_hud') == 'present'
        and visual.get('life_state') == 'alive'
        and selected_policy(c, target, frame, measurement)['kind'] in
            {'primary', 'opportunistic_here', 'ground_unknown'})


def selected_context(result):
    return {'primary': 'Selected species is a primary hunting target. ',
        'opportunistic_here': 'Configured selected creature is on established primary hunting ground. ',
        'ground_unknown': 'Current primary-ground membership is unknown because coordinates or zone are unreadable or ambiguous; a new discretionary attack is withheld. This is not an unsuitable-creature finding. ',
        'outside_primary_region': 'Current selected opportunistic creature is outside established primary hunting regions; a new discretionary attack is withheld. ',
        'unsupported_species': 'Selected species is outside the configured hunting strategy. '}[result['kind']]


def dispatch_basis(c):
    h = c.hunt
    return deepcopy({'configured_policy': c.config.get('hunting_progression_by_player_level'),
        'configured_bands': c.config.get('target_bands_by_player_level'),
        'configured_targets': c.config.get('target_names'),
        'policy': h.hunting_progression_by_player_level, 'level': c.level.last_confirmed_level,
        'bands': h.target_bands_by_player_level, 'band': h.target_band(c.level.last_confirmed_level or 1),
        'plan': h.plan, 'strategy': h.strategy_required, 'encounter': h.encounter,
        'encounter_ended': h.encounter_ended, 'last_target': h.last_target,
        'selection_revision': h.selection_revision, 'search_revision': h.search_revision,
        'combat_history_key': h.combat_history_key})


def accepted_placement(c, target, receipt, proof, *, authenticate=False):
    """Authenticate the actual dispatch guard result, never an offer-frame region."""
    from sage_wow.agent.grind_observation import digest
    accepted = bool(proof and proof.get('result', {}).get('kind') == 'opportunistic_here'
        and proof.get('name') == target_identity(target.get('name'))
        and (proof.get('basis') == dispatch_basis(c) if authenticate
            else proof.get('accepted_receipt_id') == receipt.get('receipt_id'))
        and proof.get('revision') == c.revision
        and proof.get('epoch') == c.cycle.session_epoch == receipt.get('session_epoch')
        and proof.get('generation') == receipt.get('generation_before')
        and receipt.get('generation_after') == c.cycle.input_generation
        and receipt.get('source_frame_id') == proof.get('source_frame_id')
        and receipt.get('dispatch_frame_id') == proof['frame'].frame_id
        and digest(proof['frame']) == proof.get('hash')
        and receipt == c.cycle.last_receipt and receipt.get('possible_input')
        and c.hunt.known_completed_input({'receipt': receipt}))
    if accepted and authenticate:
        proof['accepted_receipt_id'] = receipt['receipt_id']
    return accepted


def request(h, level, frame):
    h.phase = 'choose_area'; h.planning_requested = True
    h.strategy_required = {'reason': 'level_progression', 'level': level,
        'frame_id': frame.frame_id, 'captured_at': frame.captured_at,
        'search_revision': h.search_revision}


def route(c, frame, target, pending, measurement=None):
    h = c.hunt; level = c.level.last_confirmed_level
    if (selected_inspection(c, frame, target, pending, measurement) or not h.progression_pending or not policy(h, level) or pending or h.blocked
            or h.input_effect_unverified or h.active_threat or not h.encounter_ended
            or h.phase in {'recover', 'ui_recover'}):
        return False
    request(h, level, frame)
    h.progression_pending = False
    c.event('grind_level_progression_requested', {**h.strategy_required, **policy(h, level)})
    return True


def context(h, level):
    entry = policy(h, level)
    if not entry:
        return ''
    local = h.phase in {'search', 'fight', 'approach'} and not h.strategy_required
    direction = ('Hunt suitable creatures in this patch; move to another region if local search finds none. '
        if local else 'Move to a configured hunting subregion or explore toward these creatures. ')
    return (f'Level {level} hunting strategy: primary targets are {", ".join(entry["primary_targets"])}. '
        + direction +
        (f'Configured selected opportunistic creatures: {", ".join(entry.get("opportunistic_targets", []))}; consider them only on freshly established primary ground. ' if entry.get('opportunistic_targets') else 'Do not pull unrelated creatures merely because Tab selects them. ') +
        'Finish an ongoing fight and handle an actual attacker before relocating. '
        'Catalog locations/populations are scouting hypotheses; verify safe ground and current creature identity, '
        'life and permitted level before attacking. If this patch lacks primary targets, try another destination. ')


def area_allowed(h, level, area):
    entry = policy(h, level)
    return not entry or area['id'] in entry['area_ids'] or any(
        primary(h, level, name) for name in area.get('observed_target_names', []))


def search_entry_allowed(c, target, geometry, search_entry):
    h = c.hunt; level = c.level.last_confirmed_level
    if not policy(h, level):
        return True
    visual = target.get('visual_observation') or {}
    if (primary(h, level, target.get('name')) and target.get('eligibility') == 'eligible'
            and visual.get('selected_hud') == 'present' and visual.get('life_state') == 'alive'):
        return True
    if geometry.get('available'):
        return bool(geometry.get('inside_radius'))
    # Explicit visual exploration still requires independently observed movement.
    return bool((h.plan or {}).get('area', {}).get('coordinate') is None and search_entry)
