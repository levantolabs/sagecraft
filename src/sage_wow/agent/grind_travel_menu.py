"""Finite concrete retry menus; no input choice, motor grant or progress credit."""
from datetime import datetime

from sage_wow.agent.grind_search import normalized_zone, VECTOR_ERROR, PRECISION_EPSILON

MAX_RETRY_PAGES = 4
PAGE_SIZE = 6
PREFERENCE = ('probe_forward', 'advance_forward', 'detour_forward', 'target_enemy',
    'detour_strafe_left', 'detour_strafe_right', 'detour_backward',
    'turn_left', 'turn_right', 'detour_forward_left', 'detour_forward_right',
    'detour_backward_left', 'detour_backward_right', 'begin_hunt', 'change_destination')


def question(candidates):
    """A narrowed page cannot instruct Sage to select an absent action."""
    return ('Choose the next action for this hunting task from this recovery page: '
            + ', '.join(candidate.option for candidate in candidates) + '.')


def task_key(c):
    area = (c.hunt.plan or {}).get('area', {})
    coordinate = area.get('coordinate')
    # A label/request/visual redeclaration is not a different physical task.
    return {'coordinate': list(coordinate) if coordinate else None,
            'zone': normalized_zone(area.get('zone_reference', '')) if coordinate else None}


def changed_question(c, frame, state):
    move = c.hunt.last_completed_action or {}
    if (not move.get('receipt_id') or move['receipt_id'] == state.get('question_receipt')
            or move.get('receipt_id') != c.hunt.last_gameplay_receipt_id
            or move.get('session_epoch') != c.cycle.session_epoch):
        return False
    try:
        if not (datetime.fromisoformat(state['opened_at']) < datetime.fromisoformat(move['after_captured_at'])
                <= datetime.fromisoformat(frame.captured_at)):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    # turn_completed requires a linked completed receipt in resolve_travel,
    # even if numeric position is unreadable. A keypress alone is insufficient.
    return bool((move.get('status') == 'turn_completed' and move.get('action', '').startswith('turn_'))
        or (move.get('attributable_pair') and move.get('status') == 'observed_displacement'
            and move.get('displacement', 0) > VECTOR_ERROR + PRECISION_EPSILON))


def state_for(c, frame):
    from sage_wow.agent import grind_navigation_recovery as recovery
    recovery.remember_menu(c)
    current_task = task_key(c)
    recovery.restore_menu(c, current_task)
    state = c.hunt.travel_policy.get('retry_menu')
    # Keep the last concrete goal through visual declarations. A→visual→A is
    # the same task; an initially visual episode can choose its first real goal.
    new_destination = bool(state and current_task['coordinate'] is not None
        and not recovery.same_goal(current_task, state['task']) and (c.hunt.plan or {}).get('request_id')
        and (c.hunt.plan or {}).get('request_id') != state.get('plan_request_id'))
    changed = bool(state and changed_question(c, frame, state))
    if state and (new_destination or changed):
        c.event('grind_travel_retry_menu_rearmed', {'prior': state,
            'reason': 'different_destination' if new_destination else 'linked_changed_question',
            'receipt_id': (c.hunt.last_completed_action or {}).get('receipt_id'),
            'progress_credit': False, 'allowance_renewed': False})
        if changed:
            recovery.rearmed(c, state['task'])
        c.hunt.travel_policy.pop('retry_menu', None)
        return None
    return state


def resume_question(c, frame, target, pending, phase):
    """Fresh task routing after interruptions; stored pages carry no authority."""
    from sage_wow.agent.grind_travel_recovery import ready
    from sage_wow.agent.grind_navigation_recovery import goal_capacity_exhausted
    if (phase != 'travel' or pending is not None or (c.hunt.plan or {}).get('phase') != 'travel'
            or not (c.hunt.travel_policy.get('retry_menu') or goal_capacity_exhausted(c))
            or not ready(c, frame, target)):
        return False
    c.hunt.compact_stage = 'acquire'
    return True


def select(c, frame, candidates):
    """Return the original objects, a page descriptor, and exhaustion status."""
    from sage_wow.agent import grind_navigation_recovery as recovery
    recovery.remember_menu(c)
    if recovery.goal_capacity_exhausted(c):
        # A later ordinary strategy exit may select another goal. With no room
        # to retain its debt, do not give it repeatable initial presentations.
        # This also covers a prior successful full menu leaving retry_menu=None.
        state = c.hunt.travel_policy.get('retry_menu') or {}
        return [], {'pages_asked': state.get('pages', 0),
            'presented': list(state.get('presented', [])),
            'remaining_ids': [item.option for item in candidates if item.option != 'urgent_state'],
            'exhaustion_reason': 'goal_history_capacity'}, True
    state = state_for(c, frame)
    if not state:
        return candidates, None, False
    seen = set(state['presented'])
    available = {candidate.option: candidate for candidate in candidates}
    remaining = [ident for ident in PREFERENCE if ident in available and ident not in seen]
    remaining += [candidate.option for candidate in candidates
                  if candidate.option not in PREFERENCE and candidate.option not in seen
                  and candidate.option != 'urgent_state']
    if state['pages'] >= MAX_RETRY_PAGES or not remaining:
        state['exhausted'] = True
        return [], {'pages_asked': state['pages'], 'presented': list(state['presented']),
            'remaining_ids': remaining,
            'exhaustion_reason': 'page_limit' if state['pages'] >= MAX_RETRY_PAGES else 'eligible_coverage'}, True
    if state.get('exhausted'):
        # New candidates/frames by themselves cannot retire declared exhaustion.
        return [], {'pages_asked': state['pages'], 'presented': list(state['presented']),
            'remaining_ids': remaining, 'exhaustion_reason': 'retained_exhaustion'}, True
    urgent = available.get('urgent_state')
    ids = remaining[:PAGE_SIZE - bool(urgent)]
    choices = [available[ident] for ident in ids]
    if urgent:
        choices.append(urgent)
    return choices, {'page': state['pages'] + 1, 'max_pages': MAX_RETRY_PAGES,
        'ids': [choice.option for choice in choices], 'remaining_ids': remaining[len(ids):]}, False


def record(c, frame, candidates, result, page):
    rejected = (result.decision is not None and result.status != 'dispatched'
                and result.execution is None and not (result.receipt or {}).get('possible_input')
                and not (result.receipt or {}).get('dispatch_unknown'))
    if not (result.no_input_abstention or rejected or result.status == 'dispatched'):
        return
    receipt = result.receipt or {}
    # Match _travel's acceptance boundary: status alone is not completed input.
    accepted = (result.status == 'dispatched' and receipt == c.cycle.last_receipt
                and receipt.get('completed') and not receipt.get('dispatch_unknown')
                and not receipt.get('error'))
    state = c.hunt.travel_policy.get('retry_menu')
    if page is None:
        if state or result.status == 'dispatched':
            return
        c.hunt.travel_policy['retry_menu'] = {'task': task_key(c), 'opened_at': frame.captured_at,
            'plan_request_id': (c.hunt.plan or {}).get('request_id'),
            'question_receipt': (c.hunt.last_completed_action or {}).get('receipt_id'),
            'presented': [], 'pages': 0, 'exhausted': False, 'last_request_id': None}
        return
    if not state:
        return
    request = getattr(getattr(result.decision, 'envelope', None), 'request_id', None)
    if not request or request == state.get('last_request_id'):
        return
    state['last_request_id'] = request
    state['pages'] += 1
    chosen = getattr(result.decision, 'chosen', None)
    # `presented` retains consumed-option debt for checkpoint compatibility.
    # A concrete choice spends this question and its selected option, not the
    # unchosen alternatives. No-input null/rejection still covers the whole page.
    # Every request counts toward the same cap; unresolved movement refunds none.
    consumed = [candidate.option for candidate in candidates if candidate.option != 'urgent_state'
                and (not accepted or candidate.option == chosen)]
    state['presented'] = list(dict.fromkeys([*state['presented'],
        *consumed]))
    if state['pages'] >= MAX_RETRY_PAGES:
        state['exhausted'] = True
    c.event('grind_travel_retry_menu_attempt', {**page, 'request_id': request, 'status': result.status,
        'chosen': chosen, 'consumed_ids': consumed,
        'coverage_kind': 'selected_option' if accepted else 'no_input_page' if result.status != 'dispatched' else 'unaccepted_dispatch_page',
        'exhausted': state['exhausted'],
        'progress_credit': False})
    from sage_wow.agent.grind_navigation_recovery import remember_menu
    remember_menu(c)
