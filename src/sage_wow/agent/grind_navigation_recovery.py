"""A bounded hunting question after travel abstention, never new motor authority."""
import asyncio
from copy import deepcopy
from datetime import datetime
from dataclasses import replace
import math
import time

from sage_wow.agent.cycle import ActionCandidate, CycleResult, DispatchValidation
from sage_wow.agent.grind_hunt_entry import idle_acquisition
from sage_wow.agent.grind_observation import digest
from sage_wow.agent.grind_search import normalized_zone, ui_evidence, zone_identity, VECTOR_ERROR, PRECISION_EPSILON
from sage_wow.agent import grind_travel_scout as scout

MAX_ROUNDS = 2
MAX_REQUESTS = 4
MAX_GOALS = 3
KEY = 'recovery_review'


def record(c):
    return c.hunt.travel_policy.get(KEY)


def goal(area):
    point = area.get('coordinate')
    return {'coordinate': list(point) if point else None,
        'zone': normalized_zone(area.get('zone_reference', '')) if point else None}


def current_goal(c):
    return goal((c.hunt.plan or {}).get('area', {}))


def same_goal(a, b):
    if a['zone'] != b['zone']:
        return False
    if a['coordinate'] is None or b['coordinate'] is None:
        return a['coordinate'] is None and b['coordinate'] is None
    return math.dist(a['coordinate'], b['coordinate']) <= VECTOR_ERROR + PRECISION_EPSILON


def note_destination(c, area):
    """Ordinary deliberate planning also participates in the bounded history."""
    state = record(c); key = goal(area)
    if state and len(state['goals']) < MAX_GOALS and not any(
            same_goal(key, item['goal']) for item in state['goals']):
        state['goals'].append({'goal': key, 'menu': None})


def goal_capacity_exhausted(c):
    state = record(c)
    return bool(state and len(state['goals']) >= MAX_GOALS
        and not any(same_goal(current_goal(c), item['goal']) for item in state['goals']))


def ready(c, frame, target):
    from sage_wow.agent.grind_travel_recovery import ready as travel_ready
    return bool(travel_ready(c, frame, target) and idle_acquisition(c, target, c.hunt.pending, frame)
        and (target.get('hud') or {}).get('player_mana') != 0)


def comparison_anchor(frame, measurement):
    return {'frame_id': frame.frame_id, 'captured_at': frame.captured_at, 'source_image': frame.image_path,
        'source_hash': digest(frame), 'frame_source': frame.source,
        'frame_size': [frame.width, frame.height],
        'position': deepcopy(measurement['position']), 'zone': list(zone_identity(measurement))}


def refresh(c, frame, measurement, target):
    """Only a current post-review translated chain can retire this local debt."""
    state = record(c)
    if not state or not scout.readable(frame, measurement) or not c.current() or not c.fresh(frame):
        return False
    anchor = state['anchor']; move = c.hunt.last_completed_action or {}
    if not anchor['position']:
        if scout.travel_owned(c) and ready(c, frame, target):
            state['anchor'] = comparison_anchor(frame, measurement)
            c.event('grind_navigation_review_baseline', {'frame_id': frame.frame_id,
                'reason': 'first_current_readable_comparison', 'requests': state['requests'],
                'progress_credit': False, 'allowance_renewed': False})
        return False  # This frame is an origin, never evidence of prior translation.
    if (anchor['frame_source'] != frame.source
        or anchor['frame_size'] != [frame.width, frame.height]
        or tuple(anchor['zone']) != zone_identity(measurement)
        or math.dist(anchor['position'], measurement['position']) <= VECTOR_ERROR + PRECISION_EPSILON
        or move.get('search_generation', move.get('generation_after')) != c.cycle.input_generation):
        return False
    entry = c.hunt.travel_search_evidence(frame, measurement, c.cycle.session_epoch)
    if not entry or entry['receipt_id'] != move.get('receipt_id'):
        return False
    try:
        if digest_path(anchor['source_image']) != anchor['source_hash']:
            return False
        def after_baseline(item):
            captured = datetime.fromisoformat(item['source_captured_at'])
            baseline = datetime.fromisoformat(anchor['captured_at'])
            return captured > baseline or (captured == baseline
                and item.get('source_frame_id') == anchor.get('frame_id')
                and item.get('source_image') == anchor['source_image']
                and item.get('source_hash') == anchor['source_hash'])
        translated = any(item['receipt_id'] in entry['travel_receipts']
            and item.get('status') == 'observed_displacement'
            and item.get('displacement', 0) > VECTOR_ERROR + PRECISION_EPSILON
            and after_baseline(item)
            for item in c.hunt.recent_moves)
    except (OSError, KeyError, TypeError, ValueError):
        return False
    if not translated:
        return False
    c.hunt.travel_policy.pop(KEY, None)
    c.event('grind_navigation_review_retired', {'reason': 'current_post_review_translation',
        'receipt_id': entry['receipt_id'], 'progress_credit': False, 'allowance_renewed': False})
    return True


def digest_path(path):
    from pathlib import Path
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def remember_menu(c):
    state = record(c); menu = c.hunt.travel_policy.get('retry_menu')
    if not state or not menu:
        return
    key = menu['task']
    item = next((item for item in state['goals'] if same_goal(item['goal'], key)), None)
    if item is None and len(state['goals']) < MAX_GOALS:
        item = {'goal': key, 'menu': None}; state['goals'].append(item)
    if item is not None:
        item['menu'] = deepcopy(menu)


def restore_menu(c, key):
    """Restore spent goal debt once; its live copy then records all new changes."""
    state = record(c)
    if not state:
        return
    menu = c.hunt.travel_policy.get('retry_menu')
    item = next((item for item in state['goals'] if same_goal(item['goal'], key)), None)
    if item and item['menu'] and (not menu or not same_goal(menu['task'], key)):
        c.hunt.travel_policy['retry_menu'] = deepcopy(item['menu'])


def rearmed(c, key):
    state = record(c)
    item = next((item for item in state['goals'] if same_goal(item['goal'], key)), None) if state else None
    if item:
        # A linked changed question retires the snapshot as well as the live
        # menu. A later declaration cannot replay the same completed turn.
        item['menu'] = None


def ensure(c, frame, measurement):
    state = record(c)
    if state is None:
        readable = scout.readable(frame, measurement)
        state = {'anchor': {'captured_at': frame.captured_at, 'source_image': frame.image_path,
            'source_hash': digest(frame), 'frame_source': frame.source,
            'frame_size': [frame.width, frame.height],
            'position': deepcopy(measurement['position']) if readable else None,
            'zone': list(zone_identity(measurement)) if readable else []},
            'requests': 0, 'request_ids': [], 'last_request_id': None, 'last_outcome': None,
            'rounds_started': 0, 'round_attempt': 0, 'terminal_null': False,
            'goals': [], 'selection_assessment': False, 'explicit_planning': None}
        c.hunt.travel_policy[KEY] = state
    remember_menu(c)
    return state


def reasoning(state):
    return 'auto' if state['last_outcome'] == 'null' and state['round_attempt'] == 1 else 'off'


def available(state):
    """A genuine first null owns one retry; all other outcomes spend a round."""
    if (state['requests'] >= MAX_REQUESTS or state['terminal_null']
        or state['last_outcome'] == 'started'):
        return False
    return reasoning(state) == 'auto' or state['rounds_started'] < MAX_ROUNDS


def progress(state):
    return {'requests': state['requests'], 'limit': MAX_REQUESTS,
        'rounds_started': state['rounds_started'], 'round_limit': MAX_ROUNDS,
        'round_attempt': state['round_attempt'], 'terminal_null': state['terminal_null']}


def started(c, state, request_id):
    if record(c) is not state or not available(state):
        raise ValueError('Navigation review owner or capacity changed')
    if not request_id or request_id in state['request_ids']:
        raise ValueError('Navigation review request already charged')
    mode = reasoning(state)
    if mode == 'off':
        state['rounds_started'] += 1
        state['round_attempt'] = 1
    else:
        state['round_attempt'] = 2
    state['request_ids'].append(request_id)
    state.update(requests=state['requests'] + 1, last_request_id=request_id, last_outcome='started')
    c.event('grind_navigation_review_started', {'request_id': request_id, 'attempt': state['requests'],
        **progress(state), 'reasoning_mode': mode, 'progress_credit': False, 'input_authority': False})


def abort(c, state, reason):
    """Close an admitted interrupted round; cancellation never grants auto."""
    if record(c) is state and state['last_outcome'] == 'started':
        state['last_outcome'] = reason
        c.event('grind_navigation_review_outcome', {'request_id': state['last_request_id'],
            **progress(state), 'outcome': reason, 'progress_credit': False, 'input_authority': False})


def outcome(c, state, result):
    if record(c) is state and state['last_outcome'] == 'started':
        request_id = getattr(getattr(result.decision, 'envelope', None), 'request_id', None)
        authenticated_null = result.no_input_abstention and request_id == state['last_request_id']
        if authenticated_null:
            state['last_outcome'] = 'null'
            state['terminal_null'] = state['round_attempt'] == 2
            c.event('grind_navigation_review_outcome', {'request_id': state['last_request_id'],
                **progress(state), 'outcome': 'null', 'progress_credit': False, 'input_authority': False})
        else:
            abort(c, state, 'unmatched_null' if result.no_input_abstention else result.status)


def readiness_diagnostic(c, frame, target):
    """Explain admission without spending, restoring or manufacturing an action."""
    h = c.hunt; state = record(c); hud = target.get('hud') or {}
    prerequisites = []
    checks = (
        ('unfinished_encounter', not h.encounter_ended),
        ('retained_cast_constraint', h.cast_blocks_acquisition()),
        ('pending_action_assessment', h.pending is not None),
        ('current_selected_target', scout.absence_conflicts(target)),
        ('current_threat', bool(h.active_threat)),
        ('resource_recovery', bool(c.heal_pending or h.no_mana or hud.get('player_mana') == 0)),
        ('loot_task', bool(h.loot_request)),
        ('disengagement', bool(h.disengagement)),
        ('input_effect_unverified', bool(h.input_effect_unverified)),
        ('world_reassessment', bool(c.require_world)),
        ('blocked_task', bool(h.blocked)),
        ('current_authority_unavailable', not c.current() or not c.fresh(frame) or bool(c.cycle._scope_error(frame))),
    )
    prerequisites.extend(name for name, blocked in checks if blocked)
    if not ready(c, frame, target) and not prerequisites:
        prerequisites.append('current_travel_or_idle_prerequisite')
    reason = ('task_prerequisite' if prerequisites else
        'review_capacity_spent' if state and not available(state) else 'no_eligible_hunting_choices')
    return {'reason': reason, 'prerequisites': prerequisites,
        'admitted_requests': state['requests'] if state else 0,
        'encounter_id': h.encounter, 'owner': h.combat_history_key,
        'frame_id': frame.frame_id, 'input_authority': False, 'progress_credit': False}


def wait(c, reason, frame=None, target=None):
    diagnostic = readiness_diagnostic(c, frame, target) if frame is not None and target is not None else None
    c.navigation_wait = {'state': 'waiting', 'reason': reason,
        **(progress(record(c)) if record(c) else {'requests': 0, 'limit': MAX_REQUESTS,
            'rounds_started': 0, 'round_limit': MAX_ROUNDS, 'round_attempt': 0, 'terminal_null': False})}
    if diagnostic:
        c.navigation_wait['readiness'] = diagnostic
        # process() rebuilds the visible status each observation. Keep only a
        # bounded event-deduplication key outside that transient status.
        key = (c.cycle.session_epoch, ((record(c) or {}).get('anchor') or {}).get('captured_at'),
            diagnostic['reason'], tuple(diagnostic['prerequisites']), diagnostic['admitted_requests'],
            diagnostic['encounter_id'], diagnostic['owner'])
        if getattr(c, '_navigation_wait_diagnostic_key', None) != key:
            c.event('grind_navigation_wait_readiness', diagnostic)
            c._navigation_wait_diagnostic_key = key
        c.store.save_checkpoint('grind_only', {'mode': 'grind_only',
            'level': c.level.last_confirmed_level, 'baseline_verified': c.baseline,
            'history': c.history, 'hunt': c.hunt.context(), 'navigation_wait': c.navigation_wait,
            'observation_transaction': c.observation_transaction, 'stopped': c.stopped,
            'reason': c.reason, 'session_epoch': c.cycle.session_epoch, 'deadline': c.deadline})
    return CycleResult('grind_navigation_wait', detail='No hunting action selected; navigation recovery retains its debt')


def reconsider(c, frame, target, candidates, *, hunting_unavailable=False):
    """Pace fresh Sage questions after abstention instead of waiting forever.

    Only currently eligible motor candidates survive. Existing physical failure
    limits, dispatch guards and spent review/menu history remain authoritative.
    Elapsed time supplies no input permission or progress credit.
    """
    state = record(c)
    hunting_ready = ready(c, frame, target)
    # Unspent request capacity is not an actionable hunting menu. A completed
    # scout plus a fixed destination can leave only urgent_state eligible.
    if not state or available(state) and hunting_ready and not hunting_unavailable:
        return [], None
    if not hunting_ready:
        # Hunting admission includes old cast-effect obligations. Pure movement
        # may still assess Sage's chosen route after that encounter has ended;
        # keep the cast history and expose no targeting or attack authority.
        from sage_wow.agent.grind_travel_recovery import ready as travel_ready
        from sage_wow.agent.grind_cast_feedback import unresolved_nonlocal_cues
        error = c.hunt.cast_error or {}
        if (not c.hunt.encounter_ended or not travel_ready(c, frame, target)
                or scout.absence_conflicts(target) or unresolved_nonlocal_cues(c.hunt)
                or error.get('status') == 'active' and error.get('kind') not in {'range', 'facing', 'los'}):
            return [], None
    now = time.monotonic()
    if now < state.setdefault('reconsider_at', now + 30.):
        return [], None
    motion = [item for item in candidates
              if item.binding.get('type') in {'keypress', 'keypress_chord'}]
    urgent = next((item for item in candidates if item.option == 'urgent_state'), None)
    if not motion or urgent is None:
        return [], None
    count = state.get('reconsiderations', 0)
    offset = (count * 5) % len(motion)
    ordered = motion[offset:] + motion[:offset]
    choices = ordered[:5] + [urgent]
    state.update(reconsider_at=now + 30., reconsiderations=count + 1)
    page = {'paced_reconsideration': True, 'attempt': count + 1,
            'ids': [item.option for item in choices], 'progress_credit': False}
    c.event('grind_navigation_reconsideration', page)
    return choices, page


def route(c, frame, target, pending):
    """Actual selected-target work wins; mechanical aliases retain this owner."""
    state = record(c); h = c.hunt
    if not state or not scout.travel_owned(c) or pending or scout.active(c):
        return False
    if h.phase in {'fight', 'approach', 'recover', 'ui_recover'} or h.active_threat:
        return False
    from sage_wow.agent.grind_relocation import selected_disposition
    disposed=selected_disposition(c,frame,target,pending)
    if disposed and disposed['status'] in {'matching','temporarily_unproven'}:
        # Accepted sensing disposition wins over the same HUD, but never
        # supplies focused-Tab admission or input permission.
        if h.phase=='choose_area' and h.planning_requested:return False
        intent=(h.strategy_required or {}).get('selected_exit') or {}
        if (h.plan or {}).get('relocation_request_id')==intent.get('request_id'):
            h.phase='travel';h.compact_stage='acquire';h.planning_requested=False
            return True
        return False
    if disposed and disposed['status']=='superseded':
        intent=(h.strategy_required or {}).get('selected_exit') or {}
        if intent.get('disposition')!='closed':intent['superseded']=deepcopy(disposed)
    if scout.absence_conflicts(target):
        state['selection_assessment'] = True
        h.phase = 'search'; h.compact_stage = 'inspect'; h.planning_requested = False
        return True
    if state['selection_assessment']:
        return False  # Current absence must be assessed, never inferred from OCR.
    if h.phase == 'choose_area' and h.planning_requested and not state.get('explicit_planning'):
        h.phase = 'travel'; h.planning_requested = False; h.compact_stage = 'acquire'
        return True
    return False


def finish_assessment(c, frame, target, data):
    state = record(c); h = c.hunt
    if (not state or not state['selection_assessment'] or not scout.travel_owned(c)
        or not (data.get('absence') or data.get('cleared')) or scout.absence_conflicts(target)
        or not idle_acquisition(c, target, h.pending, frame) or h.active_threat):
        return
    state['selection_assessment'] = False
    h.phase = 'travel'; h.compact_stage = 'acquire'; h.planning_requested = False


async def prepare(c, frame, target, measurement, full_choices, valid, guard):
    """Build an executable hunting question, preserving the actual scout object."""
    state = ensure(c, frame, measurement)
    if not available(state) or not ready(c, frame, target):
        return [], {}, state
    before = (state['requests'], state['rounds_started'], state['round_attempt'],
        state['last_request_id'], state['last_outcome'])
    automatic = reasoning(state) == 'auto'
    expected = (state['requests'] + 1, state['rounds_started'] + (not automatic),
        2 if automatic else 1)
    def admission_current():
        if record(c) is not state or state['terminal_null']:
            return False
        actual = (state['requests'], state['rounds_started'], state['round_attempt'],
            state['last_request_id'], state['last_outcome'])
        # Cycle checks candidates both before and after the synchronous marker.
        return (actual == before and available(state) or actual[:3] == expected
            and actual[4] == 'started' and actual[3] != before[3]
            and state['request_ids'][-1:] == [actual[3]])
    choices = [item for item in full_choices if item.option == 'target_enemy']
    metadata = {}
    authority = (c.revision, c.cycle.session_epoch, c.cycle.input_generation)
    owner = (deepcopy(c.hunt.plan), deepcopy(c.hunt.strategy_required), c.hunt.search_revision)
    source_hash = digest(frame); snapshot = {}
    def current():
        actual_frame = snapshot.get('frame', frame)
        return bool(valid() and admission_current()
            and authority == (c.revision, c.cycle.session_epoch, c.cycle.input_generation)
            and owner == (c.hunt.plan, c.hunt.strategy_required, c.hunt.search_revision)
            and digest(frame) == source_hash
            and (not snapshot or digest(actual_frame) == snapshot['hash'])
            and ready(c, actual_frame, snapshot.get('target', target)))
    async def check(source):
        checked = await guard(source)
        if not checked.approved or not checked.dispatch_frame:
            return checked
        fresh = checked.dispatch_frame
        rows = await c.rows(fresh); actual = await c.target_proposal(fresh)
        from sage_wow.agent.grind_resources import hud_resources
        actual = {**actual, 'hud': await asyncio.to_thread(hud_resources, c, fresh)}
        from sage_wow.agent.grind_encounter_recovery import observe_threat
        observe_threat(c, fresh, actual)
        snapshot.update(frame=fresh, hash=digest(fresh), target=actual)
        approved = current() and not ui_evidence(c.profile, fresh, rows, {})['positive']
        return DispatchValidation(approved, fresh if approved else None,
            'Current hunting recovery owner and world', checked.evidence)
    seen = [item['goal'] for item in state['goals']]
    if len(seen) < MAX_GOALS:
        for area in c.hunt.candidates(c.level.last_confirmed_level):
            key = goal(area)
            if key['coordinate'] is None or any(same_goal(key, prior) for prior in seen):
                continue
            option = 'choose_area:' + area['id']
            def area_current(area=deepcopy(area), key=key):
                return (current() and len(state['goals']) < MAX_GOALS
                    and not any(same_goal(key, item['goal']) for item in state['goals'])
                    and any(candidate == area for candidate in c.hunt.candidates(c.level.last_confirmed_level)))
            choices.append(ActionCandidate(option,
                f'Try attributed hunting hypothesis {area["label"]!r} at {area.get("coordinate")}; '
                f'expected creatures {area.get("primary_targets", [])}. Source: {area.get("source", {})}. '
                'Route and current population remain unverified.', {'type': 'observe_only'},
                precondition=area_current, dispatch_guard=check))
            metadata[option] = {'area': deepcopy(area)}
    choices += [item for item in full_choices if item.option == 'urgent_state']
    choices = [replace(item, precondition=lambda original=item.precondition:
        original() and admission_current()) for item in choices]
    return choices if len(choices) >= 2 else [], metadata, state


def selected_destination(c, state, area):
    key = goal(area)
    if (record(c) is not state or len(state['goals']) >= MAX_GOALS
        or any(same_goal(key, item['goal']) for item in state['goals'])):
        return False
    state['goals'].append({'goal': key, 'menu': None})
    return True


def question(c, choices, frame=None, measurement=None):
    level = c.level.last_confirmed_level
    low, high = c.hunt.target_band(level)
    policy = c.hunt.hunting_progression_by_player_level.get(level) or {}
    opportunistic = policy.get('opportunistic_targets', [])
    actions = []
    if any(item.option == 'target_enemy' for item in choices):
        actions.append('Inspect a nearby hunting candidate with the offered Tab action.')
    if any(item.option.startswith('choose_area:') for item in choices):
        actions.append('Choose an offered different hunting destination.')
    action = ' '.join(actions) + ' '
    context = (f'World of Warcraft. Our priest is level {level}; goal level {c.config["goal_level"]} '
        f'by hunting suitable creatures at levels {low}–{high}. Current destination: '
        f'{(c.hunt.plan or {}).get("area", {}).get("label", "unknown")}. '
        f'Primary targets: {policy.get("primary_targets", "any eligible creature")}. '
        + (f'Configured opportunistic targets: {", ".join(opportunistic)}; consider a selected one only '
            'on freshly established primary hunting ground under the normal target checks. ' if opportunistic else '')
        +
        'The recent navigation questions did not establish a next action; this does not establish '
        'no enemies or an impassable route. No target is currently established. '
        + action + 'Selection is followed by fresh identity, life, level and '
        'normal opening checks. A new destination does not move us or renew failed methods. '
        'Ordinary terrain is not an emergency. Passive chat and normal HUD do not block actions.')
    from sage_wow.agent.grind_progression import region_context
    context += region_context(c, frame, measurement)
    instructions = ('Choose the next hunting action from this current image and the offered choices: '
        + ', '.join(item.option for item in choices) + '. '
        'Use urgent_state only for an actual attacker, own death, resource emergency or blocking UI.')
    return context, instructions
