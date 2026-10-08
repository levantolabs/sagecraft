from dataclasses import replace
import math

import pytest

from sage_wow.agent.coordinate_navigation import (
    CoordinateNavigator, NavigationAuthorization, NavigationObservation, angle, difference,
)


def navigator(waypoint=(51., 50.)):
    return CoordinateNavigator(NavigationAuthorization('auth', 'sage-request', 'source',
        'task', 1, 'epoch', 0, 100., 120., waypoint, 'Local Label',
        'offline matched quest reference', 13, 123, 124))


def observation(nav, point, moment, **changes):
    return replace(NavigationObservation(str(moment), moment, nav.expected_generation,
        point, True, 'Local Label', True, True, 'fresh-sage-observation'), **changes)


def step(nav, point, moment):
    action = nav.propose(observation(nav, point, moment), moment+.01)
    if action:
        nav.record_dispatch(action, {'completed': True, 'dispatched': True,
            'generation_after': nav.expected_generation+2}, moment+.1)
    return action


def test_quantized_heading_accumulates_same_facing_probes_then_calibrates_turn():
    nav = navigator()
    assert step(nav, (50., 50.), 101).purpose == 'authorized_heading_calibration_probe'
    # One .1-coordinate tick cannot establish heading; retain the same origin.
    assert step(nav, (50., 49.9), 102).purpose == 'authorized_heading_calibration_probe'
    assert nav.heading is None and nav.turn_rate is None
    turn = step(nav, (50., 49.8), 103)
    assert turn.action == 'turn_right' and turn.purpose == 'bounded_turn_calibration'
    assert nav.turn_rate is None  # A command duration is never a measured angle.
    assert step(nav, (50., 49.8), 104).purpose == 'authorized_heading_calibration_probe'
    next_step = step(nav, (50.3, 49.7), 105)
    assert nav.turn_rate == pytest.approx(angle((.3, -.1))/.15)
    assert next_step.action == 'turn_right' and next_step.purpose == 'measured_turn_response'
    assert next_step.seconds <= .3


def test_three_quantized_no_motion_probes_hand_back_without_more_input():
    nav = navigator()
    assert all(step(nav, (50., 50.), t) for t in (101, 102, 103))
    assert step(nav, (50., 50.), 104) is None
    assert nav.stop_reason == 'navigation_three_ineffective_steps'
    assert nav.steps == 3


def test_opposite_facing_calibration_can_learn_without_claiming_goal_progress():
    nav = navigator()
    assert step(nav, (50., 50.), 101).action == 'forward'
    assert step(nav, (49.9, 50.), 102).action == 'forward'
    assert step(nav, (49.8, 50.), 103).action == 'turn_left'
    assert step(nav, (49.8, 50.), 104).purpose == 'authorized_heading_calibration_probe'
    correction = step(nav, (49.6, 50.1), 105)
    assert correction.action == 'turn_left'
    assert correction.purpose == 'measured_turn_response'
    assert nav.ineffective == 0  # Calibration produced evidence, not gameplay progress.
    assert math.dist(nav.last_position, nav.authorization.waypoint) > nav.initial_distance
    assert nav.propose(observation(nav, nav.last_position, 120), 120.01) is None
    assert nav.stop_reason == 'navigation_deadline'


@pytest.mark.parametrize('change, reason', [
    ({'position_valid': False}, 'coordinates'),
    ({'position': (math.nan, 50)}, 'coordinates'),
    ({'zone': 'Different Local Label'}, 'zone'),
    ({'safe': False}, 'unsafe'),
    ({'forward_clear': False}, 'not_clear'),
    ({'input_generation': 2}, 'generation'),
])
def test_uncertainty_and_changed_scope_stop(change, reason):
    nav = navigator()
    assert nav.propose(observation(nav, (50, 50), 101, **change), 101.01) is None
    assert reason in nav.stop_reason
    assert nav.steps == 0


def test_frame_captured_during_input_and_reused_frame_are_rejected():
    nav = navigator()
    assert step(nav, (50, 50), 101)
    during_input = observation(nav, (50.2, 50), 101.05)
    assert nav.propose(during_input, 102) is None
    assert nav.stop_reason == 'navigation_observation_not_fresh'
    nav = navigator()
    assert step(nav, (50, 50), 101)
    assert nav.propose(observation(nav, (50.2, 50), 102, frame_id='101'), 102.01) is None


def test_deadline_and_arrival_never_claim_npc_interaction():
    nav = navigator()
    assert nav.propose(observation(nav, (50, 50), 120), 120.01) is None
    assert nav.stop_reason == 'navigation_deadline'
    nav = navigator()
    assert step(nav, (50.9, 50), 101) is None
    assert 'final_approach_requires_sage' in nav.stop_reason
    assert nav.steps == 0


@pytest.mark.parametrize('scale', [(1, 1), (3, .5), (.25, 4)])
def test_turn_side_remains_correct_with_positive_unequal_coordinate_axes(scale):
    heading = (.1*scale[0], -.3*scale[1])
    destination = (.6*scale[0], .1*scale[1])
    assert difference(angle(destination), angle(heading)) > 0
