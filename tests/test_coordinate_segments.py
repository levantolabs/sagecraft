"""Safety of finite observed-ground permission, independent of geometry."""
from dataclasses import replace
import math

import pytest

from sage_wow.agent.coordinate_motion import MotionModel, VECTOR_ERROR
from sage_wow.agent.coordinate_segments import SegmentGrant
from sage_wow.agent.coordinate_navigation import NavigationStep


def grant(**changes):
    return replace(SegmentGrant('receipt', 'sage', 'source', 100., 'tool', 4,
        90., .4, (50., 50.), 28.), **changes)


def test_cumulative_path_and_uncertainty_reserved_before_every_pulse():
    g = grant()
    seconds = g.pulse(101, 4, 90, angular_uncertainty=28)
    assert .1 <= seconds <= .6
    assert .4*seconds+VECTOR_ERROR <= .6
    g.record(NavigationStep('forward', seconds, 'segment'),
        {'completed': True, 'dispatched': True, 'generation_after': 6, 'input_steps': [
            {'kind':'key_down','status':'completed','monotonic_at':100.},
            {'kind':'key_up','status':'completed','monotonic_at':100.+seconds}]}, (50., 50.), 'motor')
    assert g.pulse(102, 6, 90) is None  # No further input before measurement.
    g.observe((50.2, 50.), 6, 'after')
    assert g.path_used == pytest.approx(.2+VECTOR_ERROR)
    next_seconds = g.pulse(102, 6, 90)
    assert next_seconds and g.path_used+next_seconds*g.speed_upper+VECTOR_ERROR <= .6
    g.path_used = .5
    assert g.pulse(102, 6, 90) is None  # Cannot fit next measurement uncertainty.
    # Opposite displacement would not refund traveled distance.
    g = grant(speed_upper=1.)
    g.pending = ((50., 50.), .35, 'motor')
    g.observe((49.9, 50.), 4, 'after')
    assert g.path_used == pytest.approx(.1+VECTOR_ERROR)


def test_late_answer_does_not_restart_image_clock_and_direction_cone_is_bounded():
    g = grant()
    assert g.deadline == 106
    assert g.pulse(105.9, 4, 90) is None
    assert g.pulse(106, 4, 90) is None
    assert g.pulse(101, 4, 106, angular_uncertainty=28) is None
    assert g.pulse(101, 4, 90, angular_uncertainty=50) is None
    assert g.pulse(101, 5, 90) is None
    assert grant(hold_used=1.95).pulse(101, 4, 90) is None


def test_motion_above_observed_prediction_revokes_permission():
    g = grant()
    g.pending = ((50., 50.), .3, 'motor')
    g.observe((50.3, 50.), 4, 'after')
    assert g.invalid_reason == 'segment_motion_exceeded_prediction'
    assert g.pulse(102, 4, 90) is None


def test_quantized_motion_requires_receipts_and_adequate_baseline():
    model = MotionModel()
    model.observe((50, 50), (50.1, 50), .8, receipt_id='r1', frame_id='f1', captured_at=101)
    assert not model.eligible(101)
    model.observe((50.1, 50), (50.3, 50), .8, receipt_id='r2', frame_id='f2', captured_at=102)
    assert model.eligible(102)
    assert model.heading == 90
    assert model.angular_uncertainty == pytest.approx(math.degrees(math.asin(VECTOR_ERROR/.3)))
    assert model.speed_upper > (.3+VECTOR_ERROR)/1.6
    assert not model.eligible(113)
    model.reset_heading()
    assert model.speed_upper and model.heading is None and not model.eligible(102)


@pytest.mark.parametrize('after,reason', [((51., 50.), 'navigation_motion_exceeded_prediction'),
    ((50.3, 49.7), 'navigation_recent_motion_changed_direction')])
def test_previously_measured_motion_rejects_jump_or_sideways_residual(after, reason):
    model = MotionModel()
    model.observe((50, 50), (50.1, 50), .8, receipt_id='r1', frame_id='f1', captured_at=101)
    model.observe((50.1, 50), (50.3, 50), .8, receipt_id='r2', frame_id='f2', captured_at=102)
    assert model.eligible(102)
    model.observe((50.3, 50), after, .8, receipt_id='r3', frame_id='f3', captured_at=103)
    assert model.invalid_reason == reason
    assert not model.eligible(103) and model.speed_upper is None
