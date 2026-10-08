"""Bounded coordinate motor control; waypoint and probe authority come from Sage.

Geometry is measured in displayed coordinate space. Positive unequal map-axis
scales preserve the cross-product turn sign. Turn rates below are observations
in that same coordinate space, never assumed physical degrees per second.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


def position(value):
    if (not isinstance(value, (tuple, list)) or len(value) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       and math.isfinite(v) and 0 <= v <= 100 for v in value)):
        return None
    return tuple(float(v) for v in value)


def angle(vector):
    return math.degrees(math.atan2(vector[0], -vector[1])) % 360


def difference(target, current):
    return (target - current + 180) % 360 - 180


@dataclass(frozen=True)
class NavigationAuthorization:
    authorization_id: str
    request_id: str
    source_frame_id: str
    task_id: str
    objective_revision: int
    session_epoch: str
    input_generation: int
    issued_at: float
    deadline: float
    waypoint: tuple[float, float]
    zone: str
    provenance: str
    forward_key: int
    left_key: int
    right_key: int
    tolerance: float = .2
    max_steps: int = 12
    policy: str = "per_pulse"
    calibration_scope: str = ""


@dataclass(frozen=True)
class NavigationObservation:
    frame_id: str
    captured_at: float
    input_generation: int
    position: tuple[float, float] | None
    position_valid: bool
    zone: str | None
    forward_clear: bool | None
    safe: bool
    vision_evidence_id: str | None


@dataclass(frozen=True)
class NavigationStep:
    action: str
    seconds: float
    purpose: str


class CoordinateNavigator:
    """One observation and at most one pulse per call; no obstacle detours."""

    def __init__(self, authorization: NavigationAuthorization):
        self.authorization = authorization
        self.expected_generation = authorization.input_generation
        self.steps = 0
        self.ineffective = 0
        self.heading = None
        self.turn_rate = None
        self.last_position = None
        self.heading_origin = None
        self.acquisition_probes = 0
        self.initial_distance = None
        self.last_frame_id = authorization.source_frame_id
        self.last_captured_at = 0.0
        self.last_completed_at = authorization.issued_at
        self.previous_step = None
        self.turn_measurement = None
        self.stop_reason = None
        self.last_turn = None
        self.oscillations = 0
        from sage_wow.agent.coordinate_motion import MotionModel
        self.motion = MotionModel()
        self.segment = None
        self.step_permission = None
        self.last_motor_receipt = None
        self.last_motion_measurement = None
        self._prepared = None

    def stop(self, reason):
        self.stop_reason = reason
        return None

    def prepare(self, observation: NavigationObservation, now: float):
        """Measure a fresh physical result without granting movement permission."""
        a = self.authorization
        if self.stop_reason:
            return None
        if now >= a.deadline:
            return self.stop('navigation_deadline')
        if observation.input_generation != self.expected_generation:
            return self.stop('navigation_input_generation_changed')
        if (observation.frame_id == self.last_frame_id or not 0 <= now-observation.captured_at <= 10
                or observation.captured_at <= max(self.last_captured_at, self.last_completed_at)):
            return self.stop('navigation_observation_not_fresh')
        current = position(observation.position)
        if not observation.position_valid or current is None:
            return self.stop('navigation_coordinates_missing_or_conflicting')
        if not observation.zone or observation.zone.casefold() != a.zone.casefold():
            return self.stop('navigation_zone_changed_or_unknown')
        # Reject discontinuous sensing before calibration or travel diagnosis.
        if self.last_position is not None and math.dist(current, self.last_position) > 2:
            return self.stop('navigation_coordinate_jump')
        receipt = self.last_motor_receipt or {}
        if (self.previous_step and self.previous_step.action == 'forward'
                and self.last_position is not None and math.dist(current, self.last_position) <= 2
                and receipt.get('completed') and receipt.get('dispatched')
                and receipt.get('generation_after') == observation.input_generation):
            # Retain raw post-input evidence before prepare consumes previous_step.
            # A later rejected permission must not erase the preceding motor result.
            self.last_motion_measurement = {
                'before': tuple(self.last_position), 'after': tuple(current),
                'displacement': math.dist(current, self.last_position),
                'distance_improvement': math.dist(self.last_position, a.waypoint)-math.dist(current, a.waypoint),
                'frame_id': observation.frame_id, 'captured_at_epoch': observation.captured_at,
                'before_frame_id': receipt.get('source_frame_id'), 'receipt_id': receipt.get('receipt_id'),
                'input_generation': observation.input_generation, 'authorization_id': a.authorization_id,
                'completed_forward_input': True,
                'source': 'fresh post-forward coordinates at exact tool generation; quantized fallible measurement, not heading or route clearance',
                'progress_claimed': False}
        self.last_frame_id = observation.frame_id
        self.last_captured_at = observation.captured_at
        distance = math.dist(current, a.waypoint)
        if self.initial_distance is None:
            self.initial_distance = distance
        elif distance-self.initial_distance > 1.0:
            return self.stop('navigation_calibration_drift_limit')
        if distance <= a.tolerance:
            return self.stop('waypoint_tolerance_reached_final_approach_requires_sage')
        previous = self.previous_step
        if self.authorization.policy == 'segments' and previous:
            if previous.action == 'forward' and self.last_motor_receipt:
                receipt = self.last_motor_receipt
                from sage_wow.agent.coordinate_motion import completed_hold_seconds
                duration = completed_hold_seconds(receipt)
                if duration is not None:
                    self.motion.observe(self.last_position, current, duration,
                        receipt_id=receipt.get('receipt_id'), frame_id=observation.frame_id,
                        captured_at=observation.captured_at)
                    if self.motion.invalid_reason:
                        return self.stop(self.motion.invalid_reason)
                if self.segment:
                    self.segment.observe(current, observation.input_generation, observation.frame_id)
            else:
                self.motion.reset_heading()
                if self.segment:
                    self.segment.cancel('segment_direction_changed')
        if previous and previous.action == 'forward':
            delta = tuple(current[i]-self.heading_origin[i] for i in range(2))
            displacement = math.hypot(*delta)
            resolved_heading = displacement >= .2-1e-9
            if resolved_heading:
                self.heading = angle(delta)
                self.heading_origin = current
            improvement = math.dist(self.last_position, a.waypoint)-distance
            if previous.purpose == 'authorized_heading_calibration_probe':
                self.acquisition_probes += 1
                if math.dist(current, self.last_position) < .05:
                    self.ineffective += 1
                if self.acquisition_probes >= 3 and not resolved_heading:
                    return self.stop('navigation_three_ineffective_steps' if self.ineffective >= 3
                                     else 'navigation_heading_unresolved_after_three_probes')
                if resolved_heading:
                    self.acquisition_probes = 0
            else:
                self.ineffective = self.ineffective+1 if improvement <= .06 else 0
            if improvement < -.5:
                return self.stop('navigation_distance_increasing')
            if self.turn_measurement and resolved_heading:
                before, direction, seconds = self.turn_measurement
                change = difference(self.heading, before)
                if 5 <= abs(change) <= 100 and change*direction > 0:
                    self.turn_rate = abs(change)/seconds
                    self.ineffective = 0
                else:
                    self.turn_rate = None
                    self.ineffective += 1
                self.turn_measurement = None
        elif previous and previous.action != 'forward':
            # Stationary turns reveal no heading from coordinates. Only the
            # next separately cleared short probe can measure their response.
            if math.dist(current, self.last_position) > .15:
                return self.stop('navigation_unexpected_translation_during_turn')
            self.heading = None
            self.heading_origin = current
        elif self.last_position and math.dist(current, self.last_position) > .15:
            return self.stop('navigation_unexplained_displacement')
        self.previous_step = None
        self.last_position = current
        if self.heading_origin is None:
            self.heading_origin = current
        if self.ineffective >= 3:
            return self.stop('navigation_three_ineffective_steps')
        if self.steps >= a.max_steps:
            return self.stop('navigation_step_budget')
        self._prepared = (observation.frame_id, observation.captured_at, observation.input_generation)
        return True

    def plan(self):
        """Preview geometry only; mutations belong to accepted physical dispatch."""
        if self.stop_reason or self.last_position is None:
            return None
        a, current = self.authorization, self.last_position
        if self.heading is None:
            step = NavigationStep('forward', .8, 'authorized_heading_calibration_probe')
        else:
            bearing = angle(tuple(a.waypoint[i]-current[i] for i in range(2)))
            error = difference(bearing, self.heading)
            if abs(error) <= 20:
                step = NavigationStep('forward', .6, 'coordinate_approach')
            else:
                direction = 1 if error > 0 else -1
                reversals = self.oscillations + int(self.last_turn is not None and self.last_turn != direction)
                if reversals >= 3:
                    return self.stop('navigation_turn_oscillation')
                seconds = (.15 if self.turn_rate is None else
                           min(.3, max(.03, abs(error)/self.turn_rate*.6)))
                step = NavigationStep('turn_right' if direction > 0 else 'turn_left',
                                      seconds, 'measured_turn_response' if self.turn_rate else 'bounded_turn_calibration')
        return step

    def propose(self, observation: NavigationObservation, now: float) -> NavigationStep | None:
        if not observation.safe or not observation.vision_evidence_id:
            return self.stop('navigation_unsafe_or_unobserved_scene')
        key = (observation.frame_id, observation.captured_at, observation.input_generation)
        if self._prepared != key and not self.prepare(observation, now):
            return None
        if now >= self.authorization.deadline or observation.input_generation != self.expected_generation:
            return self.stop('navigation_deadline_or_input_changed')
        step = self.plan()
        if step is None:
            return None
        if step.action == 'forward' and observation.forward_clear is not True:
            return self.stop('navigation_forward_path_not_clear')
        return step

    def record_dispatch(self, step: NavigationStep, receipt: dict[str, Any], completed_at: float):
        if not receipt.get('completed') or not receipt.get('dispatched'):
            self.stop('navigation_dispatch_failed')
            return
        self._prepared = None
        self.last_motor_receipt = receipt
        if step.action != 'forward':
            direction = 1 if step.action == 'turn_right' else -1
            if self.last_turn is not None and self.last_turn != direction:
                self.oscillations += 1
            self.turn_measurement = (self.heading, direction, step.seconds)
            self.last_turn = direction
        self.expected_generation = receipt['generation_after']
        self.last_completed_at = completed_at
        self.previous_step = step
        self.steps += 1

    def binding(self, step):
        key = {'forward': self.authorization.forward_key,
               'turn_left': self.authorization.left_key,
               'turn_right': self.authorization.right_key}[step.action]
        return {'type': 'keypress', 'keycode': key, 'hold_seconds': step.seconds}


