"""Ephemeral measured response, never input authority or route clearance."""
from __future__ import annotations

from dataclasses import dataclass
import math

# Difference of two coordinates rounded to .1: each axis can err by .1.
VECTOR_ERROR = math.sqrt(2) * .1


def completed_hold_seconds(receipt):
    timings = {s['kind']: s.get('monotonic_at') for s in receipt.get('input_steps', [])
               if s.get('status') == 'completed' and s.get('kind') in {'key_down', 'key_up'}}
    if not receipt.get('completed') or not timings.get('key_down') or not timings.get('key_up'):
        return None
    return timings['key_up']-timings['key_down']


@dataclass
class MotionModel:
    origin: tuple | None = None
    seconds: float = 0.
    samples: int = 0
    heading: float | None = None
    angular_uncertainty: float | None = None
    speed_upper: float | None = None
    speed_estimate: float | None = None
    baseline: float = 0.
    measured_at: float = 0.
    receipt_id: str | None = None
    frame_id: str | None = None
    invalid_reason: str | None = None

    def reset_heading(self):
        self.origin = None
        self.seconds = 0.
        self.samples = 0
        self.heading = self.angular_uncertainty = None
        self.baseline = 0.

    def observe(self, before, after, seconds, *, receipt_id, frame_id, captured_at):
        """Caller has validated exact generation and post-completion frame."""
        from sage_wow.agent.coordinate_navigation import angle, difference
        if not receipt_id or not 0 < seconds <= 1.1 or math.dist(before, after) > 2:
            self.reset_heading()
            self.speed_upper = self.speed_estimate = None
            return
        recent = tuple(after[i]-before[i] for i in range(2))
        recent_distance = math.hypot(*recent)
        if self.speed_upper and recent_distance > self.speed_upper*seconds+VECTOR_ERROR:
            self.invalid_reason = 'navigation_motion_exceeded_prediction'
        if (self.heading is not None and self.angular_uncertainty is not None
                and recent_distance > VECTOR_ERROR):
            recent_uncertainty = math.degrees(math.asin(VECTOR_ERROR/recent_distance))
            if abs(difference(angle(recent), self.heading)) > self.angular_uncertainty+recent_uncertainty+15:
                self.invalid_reason = 'navigation_recent_motion_changed_direction'
        if self.invalid_reason:
            self.reset_heading()
            self.speed_upper = self.speed_estimate = None
            return
        if self.origin is None:
            self.origin = before
        self.seconds += seconds
        self.samples += 1
        delta = tuple(after[i]-self.origin[i] for i in range(2))
        self.baseline = math.hypot(*delta)
        self.measured_at, self.receipt_id, self.frame_id = captured_at, receipt_id, frame_id
        if self.baseline <= VECTOR_ERROR:
            return
        self.heading = angle(delta)
        self.angular_uncertainty = math.degrees(math.asin(min(1., VECTOR_ERROR/self.baseline)))
        self.speed_estimate = self.baseline/self.seconds
        # A bound on observed straight-ground response, not a universal speed
        # guarantee. Margin plus quantization; anomalies invalidate continuation.
        estimate_upper = 1.25*(self.baseline+VECTOR_ERROR)/self.seconds
        self.speed_upper = max(self.speed_upper or 0., estimate_upper)

    def eligible(self, now):
        return bool(not self.invalid_reason and self.samples >= 2 and self.baseline >= .3-1e-9
                    and self.angular_uncertainty is not None and self.angular_uncertainty <= 30
                    and self.speed_upper and 0 <= now-self.measured_at <= 10)

    def context(self):
        return {'heading_coordinate_degrees': self.heading,
            'angular_uncertainty_degrees': self.angular_uncertainty,
            'baseline_map_points': round(self.baseline, 3),
            'speed_map_points_per_hold_second': self.speed_estimate,
            'observed_speed_upper_with_margin': self.speed_upper,
            'coordinate_resolution': .1, 'vector_error_bound': VECTOR_ERROR,
            'samples': self.samples, 'invalid_reason': self.invalid_reason, 'source_receipt_id': self.receipt_id,
            'source_frame_id': self.frame_id,
            'provenance': 'quantized screenshot displacement and completed motor duration; x east/y south map percentage points, not physical compass calibration; undetected external stationary rotation remains possible'}
