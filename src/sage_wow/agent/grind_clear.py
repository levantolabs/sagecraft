"""One freshly guarded second Escape within an actual Sage clear attempt."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

from sage_wow.agent.grind_relocation import safe_current
from sage_wow.agent.grind_search import target_identity


DESCRIPTION = (' If the same selection remains on the next fresh view, one further guarded Escape '
               'is authorized; stop after two total and verify current absence.')


def configuration(c):
    return hashlib.sha256(json.dumps({'settings': c.config,
        'controls': c.profile.values.get('controls'),
        'calibration': c.profile.values.get('calibration')}, sort_keys=True, default=str).encode()).hexdigest()


def arm(c, result):
    pending = c.hunt.pending; receipt = result.receipt or {}
    escape = c.profile.values['controls']['bindings'].get('escape') or {}
    binding = receipt.get('selected_binding') or {}
    if (not pending or pending.get('family') != 'clear' or pending.get('receipt') != receipt
        or receipt.get('authorization_type') != 'sage_decision' or not result.decision
        or result.status != 'dispatched' or not receipt.get('possible_input')
        or not c.hunt.known_completed_input(pending) or binding.get('type') != 'keypress'
        or binding.get('keycode') != escape.get('keycode') or not escape.get('verified_from')):
        return
    c.clear_continuation = {'pending': deepcopy(pending), 'revision': c.revision,
        'configuration': configuration(c), 'epoch': c.cycle.session_epoch,
        'task_id': c.task_id, 'generation': c.cycle.input_generation,
        'selection': c.hunt.selection_revision, 'continuity': c.hunt.target_continuity,
        'deadline': min(c.deadline, time.time()+10)}


async def attempt(c, frame, target, measurement, *, ui_present=False):
    token = getattr(c, 'clear_continuation', None)
    if not token:
        return None
    c.clear_continuation = None  # Consume before the first await, including refusals.
    h = c.hunt; pending = h.pending; original = token['pending']; receipt = original['receipt']
    def current():
        scope = original.get('source_scope') or {}
        try:
            intact = hashlib.sha256(Path(original['source_image']).read_bytes()).hexdigest() == original['source_hash']
        except (OSError, KeyError):
            intact = False
        return bool(h.pending is pending and pending == original and intact
            and not pending.get('outcome_consumed') and c.current() and c.fresh(frame)
            and not c.cycle._scope_error(frame) and time.time() <= token['deadline']
            and token['revision'] == c.revision and token['task_id'] == c.task_id
            and token['configuration'] == configuration(c) and token['epoch'] == c.cycle.session_epoch
            and token['generation'] == receipt.get('generation_after') == c.cycle.input_generation
            and token['selection'] == h.selection_revision and token['continuity'] == h.target_continuity
            and scope == {'session_epoch': c.cycle.session_epoch, 'source': frame.source,
                          'width': frame.width, 'height': frame.height})
    if (ui_present or not current() or not safe_current(c, frame, target, pending=pending)
        or not h.linked(frame, c.cycle.input_generation) or not target.get('name')
        or target_identity(target['name']) != target_identity((original.get('target') or {}).get('name'))
        or target.get('self_target') or target.get('invalid_text') or target.get('observation_conflict')
        or (target.get('visual_observation') or {}).get('selected_hud') == 'absent'
        or not h.positive_selection(target)):
        c.event('grind_clear_continuation_cancelled', {'receipt_id': receipt['receipt_id'],
            'frame_id': frame.frame_id, 'reason': 'current clear ownership, UI, safety or selection unproved'})
        return None
    guard = await c.clear_guard(frame, target)
    checked = await guard(frame)
    fresh = checked.dispatch_frame
    actual=await c.target_proposal(fresh) if checked.approved and fresh is not None else None
    if (not checked.approved or fresh is None or not current() or not c.fresh(fresh) or c.cycle._scope_error(fresh)
        or not safe_current(c,fresh,actual,pending=pending) or not h.positive_selection(actual)):
        c.event('grind_clear_continuation_cancelled', {'receipt_id': receipt['receipt_id'],
            'frame_id': frame.frame_id, 'reason': checked.detail, 'evidence': checked.evidence})
        return None
    c.event('grind_clear_continuation_guarded', {'receipt_id': receipt['receipt_id'],
        'frame_id': fresh.frame_id, 'evidence': checked.evidence, 'total_escape_limit': 2})
    result = await c.cycle.execute_authorized(deepcopy(receipt['selected_binding']), frame=fresh,
        chosen_option='clear_selection_second_escape', authorization_id=receipt['receipt_id'],
        request_id=receipt['request_id'], candidate_set_version='guarded-clear-pair-v1',
        source_frame_id=fresh.frame_id, session_epoch=token['epoch'], max_frame_age_seconds=3)
    second = result.receipt
    if second and second.get('possible_input'):
        if h.pending is not pending or pending!=original:
            h.input_effect_unverified=True
            c.event('grind_clear_continuation_owner_changed',{'original_receipt_id':receipt['receipt_id'],
                'receipt':deepcopy(second),'replacement_consumed':False,'clear_proved':False})
            return result
        # This is one clear attempt, not another failed action or a new sensing
        # allowance. Retain both receipts and link assessment to the last input.
        pending.update(source_frame_id=fresh.frame_id, source_image=fresh.image_path,
            source_hash=hashlib.sha256(Path(fresh.image_path).read_bytes()).hexdigest(),
            receipt=deepcopy(second), measurement=measurement,
            clear_pair={'original': original, 'continuation_receipt_id': second['receipt_id'],
                        'total_escape_limit': 2})
        if 'selected_task_abandon' not in pending:h.install_intentional_clear(pending)
        if not h.known_completed_input(pending):
            h.input_effect_unverified = True
        c.last_input_at = datetime.fromisoformat(second['occurred_at']).timestamp()
        c.wait_until = time.time()+.15
        c.event('grind_clear_continuation_completed', {'original_receipt_id': receipt['receipt_id'],
            'receipt_id': second['receipt_id'], 'completed': second.get('completed'),
            'clear_proved': False, 'allowance_renewed': False})
    return result
