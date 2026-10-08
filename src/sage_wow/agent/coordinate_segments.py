"""Finite Sage segment permission, independent from reusable measurements."""
from __future__ import annotations

from dataclasses import dataclass
import math

from sage_wow.agent.coordinate_motion import VECTOR_ERROR, completed_hold_seconds


@dataclass
class SegmentGrant:
    receipt_id: str
    request_id: str
    source_frame_id: str
    captured_at: float
    authorization_id: str
    expected_generation: int
    heading: float
    speed_upper: float
    source_position: tuple
    angular_uncertainty: float = 0.
    hold_used: float = 0.
    path_used: float = 0.
    pending: tuple | None = None
    invalid_reason: str | None = None

    @property
    def deadline(self):
        return self.captured_at + 6.

    def cancel(self, reason):
        self.invalid_reason = reason

    def observe(self, position, generation, frame_id):
        if self.invalid_reason:
            return
        if generation != self.expected_generation:
            self.cancel('segment_input_changed')
            return
        if self.pending:
            before, predicted, motor_frame = self.pending
            if frame_id == motor_frame:
                self.cancel('segment_measurement_not_fresh')
                return
            traveled = math.dist(before, position) + VECTOR_ERROR
            self.path_used += traveled
            self.pending = None
            if traveled > predicted + 1e-8:
                self.cancel('segment_motion_exceeded_prediction')
            elif self.path_used >= .6-1e-9:
                self.cancel('segment_distance_exhausted')

    def pulse(self, now, generation, heading, *, angular_uncertainty=0., max_seconds=.6):
        from sage_wow.agent.coordinate_navigation import difference
        if (self.invalid_reason or self.pending or generation != self.expected_generation
                or now >= self.deadline or heading is None
                or abs(difference(heading, self.heading)) + angular_uncertainty > self.angular_uncertainty + 15):
            return None
        # Reserve both predicted movement and the next measurement's rounding
        # uncertainty BEFORE dispatch. Never knowingly exceed cumulative .6.
        distance_seconds = (.6-self.path_used-VECTOR_ERROR)/self.speed_upper
        seconds = min(max_seconds, 2.-self.hold_used, self.deadline-now-.05, distance_seconds)
        return math.floor(seconds*1000)/1000 if seconds >= .1 else None

    def record(self, step, receipt, position, frame_id):
        if not receipt.get('completed') or not receipt.get('dispatched'):
            self.cancel('segment_dispatch_failed')
            return
        duration = completed_hold_seconds(receipt)
        if duration is None:
            self.cancel('segment_motor_duration_unobserved')
            return
        self.hold_used += duration
        if self.hold_used > 2.:
            self.cancel('segment_hold_exhausted')
        self.expected_generation = receipt['generation_after']
        self.pending = (position, self.speed_upper*step.seconds+VECTOR_ERROR, frame_id)

    def context(self):
        return {'receipt_id': self.receipt_id, 'source_frame_id': self.source_frame_id,
            'deadline': self.deadline, 'hold_used': self.hold_used,
            'cumulative_path_including_uncertainty': self.path_used,
            'hold_cap': 2., 'path_cap': .6, 'source_frame_seconds': 6.,
            'approved_coordinate_heading': self.heading,
            'approved_heading_uncertainty_degrees': self.angular_uncertainty,
            'maximum_additional_direction_error_degrees': 15,
            'invalid_reason': self.invalid_reason}
