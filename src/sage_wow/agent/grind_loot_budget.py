"""Optional campaign loot work limits. Expiry is never evidence of an outcome."""
from copy import deepcopy
import json
from time import monotonic


def validate(profile, config):
    value = config.get('optional_loot_budget')
    if value is None:
        return None
    from sage_wow.agent.grind_inventory import character
    fields = {'character', 'goal_level', 'selection_failures', 'selection_seconds', 'total_seconds'}
    if (not isinstance(value, dict) or set(value) != fields
        or value['character'] != character(profile) or value['goal_level'] != config['goal_level']
        or type(value['goal_level']) is not int
        or type(value['selection_failures']) is not int or not 1 <= value['selection_failures'] <= 4
        or type(value['selection_seconds']) not in (int, float) or not 5 <= value['selection_seconds'] <= 30
        or type(value['total_seconds']) not in (int, float)
        or not value['selection_seconds'] <= value['total_seconds'] <= 60
        or not config['loot_enabled']):
        raise ValueError('optional_loot_budget requires matching character/goal and bounded loot limits')
    return deepcopy(value)


def enabled(c):
    return bool(c.config.get('optional_loot_budget'))


def state(c):
    if not hasattr(c, 'loot_budget_state'):
        c.loot_budget_state = c.store.load_checkpoint('grind_loot_budget') or {'sources': {}, 'handoff': None}
    return c.loot_budget_state


def save(c):
    c.store.save_checkpoint('grind_loot_budget', state(c))


def source_key(c, source):
    encounter = source.get('encounter_id')
    receipt = source.get('receipt_id') or (source.get('receipt') or {}).get('receipt_id')
    # One encounter can have many casts and later death screenshots. Neither
    # those views nor the creature's species define a new collection budget.
    identity = ['encounter', encounter] if type(encounter) is int and encounter > 0 else (
        ['receipt', receipt] if receipt else ['unattributed_frame', source.get('frame_id')])
    return json.dumps([c.config['session_id'], *identity])


def request_allowed(c, source):
    return not enabled(c) or state(c)['sources'].get(source_key(c, source), {}).get('status', 'pending') == 'pending'


def register(c, source):
    if not enabled(c):
        return
    key = source_key(c, source)
    source['budget_key'] = key
    state(c)['sources'].setdefault(key, {'status': 'pending', 'spent': 0., 'failures': 0,
        'requests': [], 'awaiting_result': None, 'confirmed_dead': False})
    state(c)['handoff'] = None
    save(c)


def keys(sweep):
    return tuple(source['budget_key'] for source in sweep.data.get('death_sources', []) if source.get('budget_key'))


def focused_sources(c, sweep):
    sources = sweep.data.get('death_sources', [])
    return sources[:1] if enabled(c) else sources


def begin(c, sweep):
    turn = getattr(c, 'loot_budget_turn', None)
    current = keys(sweep)[:1]
    if turn and turn['keys'] == current and turn['started'] is not None:
        turn['scope'] = deepcopy(sweep.data.get('death_sources', []))
        return
    suspend(c)
    c.loot_budget_turn = {'keys': current, 'scope': deepcopy(sweep.data.get('death_sources', [])), 'started': monotonic()}


def spent(c, key):
    turn = getattr(c, 'loot_budget_turn', None)
    extra = max(0., monotonic() - turn['started']) if turn and key in turn['keys'] and turn['started'] is not None else 0.
    return state(c)['sources'][key]['spent'] + extra


def suspend(c):
    """Settle the current active loot interval, including waits between frames.

    Focus loss settles the running slice immediately. Combat/healing/UI time
    is excluded by the controller's routing; resumption never refills time.
    """
    turn = getattr(c, 'loot_budget_turn', None)
    if turn and turn['started'] is not None:
        now = monotonic()
        for key in turn['keys']:
            state(c)['sources'][key]['spent'] += max(0., now - turn['started'])
        turn['started'] = None
        save(c)


def checkpoint_turn(c, sweep):
    suspend(c)
    if (c.current() and sweep.pending and not sweep.data.get('interrupted')
        and not sweep.data.get('resume_after_recovery') and c.hunt.phase not in {'recover','ui_recover'}):
        begin(c, sweep)


def switch_source(sweep):
    # An interaction with the old corpse grants no verification for the next.
    sweep.data.update(attempts=0, confirmations=0, selected_corpse=None,
        click_loot_authority=None, click_verification_current=False,
        search_steps=0, uncertain_frames=0, stage='inspect')


def permits(c, sweep, *, acquisition=False):
    if not enabled(c):
        return True
    turn = getattr(c, 'loot_budget_turn', None)
    if (not turn or turn['keys'] != keys(sweep)[:1] or turn['scope'] != sweep.data.get('death_sources', [])
        or c.hunt.loot_request):
        return False
    limits = c.config['optional_loot_budget']
    for key in turn['keys']:
        entry = state(c)['sources'][key]
        if entry['status'] != 'pending' or spent(c, key) >= limits['total_seconds']:
            return False
        if acquisition and (spent(c, key) >= limits['selection_seconds'] or entry['failures'] >= limits['selection_failures']):
            return False
    return True


def retire_expired(c, sweep, frame, *, combat=False):
    h = c.hunt
    if (not enabled(c) or combat or not c.current() or not c.fresh(frame) or c.cycle._scope_error(frame)
        or h.pending or h.input_effect_unverified or h.blocked
        or not h.encounter_ended or h.active_threat or c.heal_pending or h.loot_request):
        return False
    receipt = c.cycle.last_receipt or {}
    if receipt.get('possible_input') and (not receipt.get('completed') or receipt.get('dispatch_unknown') or receipt.get('error')):
        return False
    limits = c.config['optional_loot_budget']; expired = []
    for source in focused_sources(c, sweep):
        key = source.get('budget_key')
        if not key:
            continue
        entry = state(c)['sources'][key]; elapsed = spent(c, key)
        selection_done = elapsed >= limits['selection_seconds'] or entry['failures'] >= limits['selection_failures']
        # An accepted click may auto-loot. Preserve its fresh result assessment
        # (and a confirmed corpse's F path) within the same total ceiling.
        if elapsed >= limits['total_seconds'] or selection_done and not (entry['awaiting_result'] or entry['confirmed_dead']):
            entry.update(status='skipped_unverified', reason='total_time' if elapsed >= limits['total_seconds'] else 'selection_budget')
            expired.append(source)
    if not expired:
        return False
    expired_keys = {s['budget_key'] for s in expired}
    sweep.data.setdefault('retired_sources', []).extend(deepcopy(expired))
    sweep.data['death_sources'] = [s for s in sweep.data['death_sources'] if s.get('budget_key') not in expired_keys]
    switch_source(sweep)
    sweep.data['pending'] = bool(sweep.data['death_sources'])
    if not sweep.pending:
        sweep.data['outcome'] = 'skipped_unverified'
        state(c)['handoff'] = {'sources': sorted(expired_keys), 'frame_id': frame.frame_id}
        h.phase = 'search'; h.compact_stage = 'recovery' if h.cast_obligation else 'acquire'
    c.event('grind_loot_budget_retired', {'sources': deepcopy(expired),
        'outcome': 'skipped_unverified', 'verified_looted': False, 'pending': sweep.pending,
        'budgets': {k: {**state(c)['sources'][k], 'spent': spent(c, k)} for k in expired_keys}})
    save(c)
    c.store.save_checkpoint('grind_loot', c.loot_state)
    return True


def record(c, sweep, result, data, accepted):
    if not enabled(c):
        return
    decision = result.decision
    request_id = decision.envelope.request_id if decision else None
    if not request_id:
        return
    receipt = result.receipt or {}
    if receipt.get('possible_input') and not accepted:
        return  # Input reconciliation/stop takes precedence over every limit.
    for key in (getattr(c, 'loot_budget_turn', None) or {}).get('keys', ()):
        entry = state(c)['sources'][key]
        if request_id in entry['requests']:
            continue
        entry['requests'].append(request_id)
        if data.get('select') and not accepted:
            entry['failures'] += 1
        if accepted and data.get('select'):
            if entry['awaiting_result']:
                entry['failures'] += 1  # Prior selection was not resolved before retry.
            entry['awaiting_result'] = request_id
        elif accepted and data.get('interact'):
            entry['awaiting_result'] = request_id
        elif accepted and data.get('corpse'):
            entry.update(confirmed_dead=True, awaiting_result=None)
        elif entry['awaiting_result'] and accepted and decision.chosen in {'loot_inspect', 'loot_remaining'}:
            entry['failures'] += 1
            entry['awaiting_result'] = None
        if not sweep.pending:
            entry.update(status=sweep.data.get('outcome', 'closed_unverified'), awaiting_result=None)
    if not sweep.pending:
        closed = set((getattr(c, 'loot_budget_turn', None) or {}).get('keys', ()))
        current = [s for s in sweep.data.get('death_sources', []) if s.get('budget_key') in closed]
        sweep.data.setdefault('completed_sources', []).extend(deepcopy(current))
        sweep.data['death_sources'] = [s for s in sweep.data.get('death_sources', []) if s.get('budget_key') not in closed]
        if sweep.data['death_sources']:
            sweep.data['pending'] = True
            sweep.data.pop('outcome', None)
            switch_source(sweep)
    save(c)


def handoff_pending(c):
    return enabled(c) and bool(state(c).get('handoff'))


def finish_handoff(c):
    if handoff_pending(c):
        state(c)['handoff'] = None
        save(c)


def acquisition_ready(c, target, pending, frame):
    if not handoff_pending(c):
        return False
    # Replacing a retired corpse selection with Tab needs no Escape. Keep the
    # actual target facts intact; reuse only the idle resources/obligation gate.
    from sage_wow.agent.grind_hunt_entry import idle_hunt_state
    return idle_hunt_state(c, target, pending, frame)
