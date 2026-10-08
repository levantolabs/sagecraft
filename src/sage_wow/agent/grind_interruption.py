"""Reconcile released movement input without claiming its gameplay effect."""
import math


def released_movement(profile, receipt, recovery, guard, release):
    """Only a guarded movement cancelled by an attributed identity pause.

    The original incomplete receipt remains incomplete. This proves that its
    single owned key was released, not where it moved or whether it succeeded.
    """
    binding = receipt.get('selected_binding') or {}
    steps = receipt.get('input_steps') or []
    controls = profile.values['controls']['bindings']
    actions = [name for name in ('forward', 'backward', 'turn_left', 'turn_right', 'strafe_left', 'strafe_right')
               if controls.get(name, {}).get('verified_from')
               and controls[name].get('keycode') == binding.get('keycode')]
    duration = binding.get('hold_seconds')
    if (len(actions) != 1 or binding.get('type') != 'keypress'
            or set(binding) != {'type', 'keycode', 'hold_seconds'}
            or type(binding.get('keycode')) is not int
            or type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0
            or receipt.get('authorization_type') != 'sage_decision'
            or receipt.get('completed') is not False or receipt.get('error') != 'CancelledError'
            or receipt.get('dispatch_unknown') is not False or not receipt.get('possible_input')
            or len(steps) != 2 or [s.get('kind') for s in steps] != ['key_down', 'key_up']):
        return None
    rid = receipt.get('receipt_id')
    if not rid or any(s.get('status') != 'completed' or not s.get('attempted')
            or s.get('execution_id') != rid or s.get('details') != {'keycode': binding['keycode']}
            for s in steps):
        return None
    if (not recovery or recovery.get('release_error')
            or not guard.get('approved') or guard.get('request_id') != receipt.get('request_id')
            or guard.get('source_frame_id') != receipt.get('source_frame_id')
            or guard.get('dispatch_frame_id') != receipt.get('dispatch_frame_id')
            or guard.get('chosen') != receipt.get('chosen_option')
            or guard.get('session_epoch') != receipt.get('session_epoch')
            or guard.get('input_generation') != receipt.get('generation_before')):
        return None
    evidence = guard.get('evidence') or {}
    if (evidence.get('selected_target_required') is not False
            or evidence.get('predicates', {}).get('player_badge') is not True
            or evidence.get('predicates', {}).get('ui_clear') is not True
            or evidence.get('failed_predicates') != []):
        return None
    if (release.get('execution_id') != rid or release.get('release_error', 'missing') is not None
            or release.get('source') != 'identity_unavailable'
            or release.get('reason') != 'focus_or_calibration_invalid'):
        return None
    held = release.get('held_inputs')
    if (not isinstance(held, list) or len(held) != 1 or held[0].get('kind') != 'key'
            or held[0].get('code') != binding['keycode']):
        return None
    times = [steps[0].get('dispatch_started_at_monotonic'), steps[0].get('dispatch_completed_at_monotonic'),
             recovery.get('observation', {}).get('monotonic_at'), release.get('release_started_at_monotonic'),
             release.get('release_attempt_completed_at_monotonic'),
             steps[1].get('dispatch_started_at_monotonic'), steps[1].get('dispatch_completed_at_monotonic')]
    elapsed = held[0].get('held_seconds')
    if (any(type(t) not in (int, float) or not math.isfinite(t) for t in times)
            or times != sorted(times) or type(elapsed) not in (int, float)
            or not math.isfinite(elapsed) or not 0 <= elapsed <= duration):
        return None
    return actions[0]
