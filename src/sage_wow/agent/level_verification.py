"""Sage-selected, screenshot-only observations of the player's displayed level.

This module is intentionally standalone. It does not capture a frame, call Sage,
dispatch input, or mutate tactical gameplay state. A caller owns those steps and
may use these helpers to build a focused request and keep its evidence scoped to
the current decision-cycle session epoch.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from sage_wow.agent.ui_layout import UIRegions


# In the currently observed client layout, the player's own unit frame is at the
# lower left. The selected target frame is on the lower right and is deliberately
# excluded from the magnified crop.
# The black level circle hangs below the lower-left unit-frame bars in this
# client layout. The previous bottom edge cut through that circle on 1496x967
# captures, leaving the numeral without its full badge outline.
PLAYER_UNIT_FRAME = (0.145, 0.645, 0.29, 0.75)


def player_level_assessment_image(frame, output_path: Path | None = None,
                                 source_path: Path | None = None, *, ui_layout=None) -> Path:
    """Create a same-frame view with the player's unit frame enlarged.

    The output contains only the lower-left player unit frame, not the original
    full screenshot or selected-target frame. No OCR or level is drawn into the
    image. It is a visual aid, not a verified fact.
    """
    if not frame.image_path:
        raise ValueError("level assessment requires a captured image path")
    source_path = Path(source_path or frame.image_path)
    output_path = output_path or source_path.with_name(f"player-level-{frame.frame_id}.png")
    with Image.open(source_path) as raw:
        source = raw.convert("RGB")
        if source.size != (frame.width, frame.height):
            raise ValueError("frame dimensions do not match its screenshot")
        box = UIRegions(frame,source.size,ui_layout).pixels('player_frame',PLAYER_UNIT_FRAME)
        # Keep a few pixels around the HUD element at unusual window sizes.
        left, top, right, bottom = box
        box = (max(0, left - 4), max(0, top - 4),
               min(frame.width, right + 4), min(frame.height, bottom + 4))
        crop = source.crop(box)
        scale = min(460 / crop.width, 350 / crop.height)
        crop = crop.resize((max(1, round(crop.width * scale)),
                            max(1, round(crop.height * scale))), Image.Resampling.NEAREST)
        canvas = Image.new("RGB", (480, crop.height + 60), "#171d25")
        draw = ImageDraw.Draw(canvas)
        draw.text((10, 10), "PLAYER UNIT FRAME ONLY", fill="white")
        canvas.paste(crop, (10, 42))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path)
    return output_path


PLAYER_LEVEL_TEXT = "Read the supplied image."
PLAYER_LEVEL_QUESTION = (
    "Which numeral is visible inside the dark circular badge beside the portrait? "
    "Choose unreadable if no numeral can be read."
)


def _frame_age_seconds(frame, now: datetime | None = None) -> float:
    captured = datetime.fromisoformat(frame.captured_at)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now.astimezone(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds())


class PlayerLevelVerifier:
    """In-memory cadence and provenance tracker, scoped to a session epoch.

    The caller should activate the current DecisionCycle session epoch on
    startup/resume, call ``note_request_started`` before each Sage request, and
    pass a returned Sage choice to ``record``. Epoch changes discard prior level
    evidence so an old session's level cannot be presented as current.
    """

    def __init__(self, interval_seconds: float = 60.0, max_frame_age_seconds: float = 10.0,
                 *, required_readings: int = 2):
        if interval_seconds <= 0 or max_frame_age_seconds <= 0:
            raise ValueError("level verification intervals must be positive")
        self.interval_seconds = interval_seconds
        self.max_frame_age_seconds = max_frame_age_seconds
        if required_readings not in (1, 2):
            raise ValueError("level verification requires one or two readings")
        self.required_readings = required_readings
        self.session_epoch: str | None = None
        self.last_attempt_at: float | None = None
        self.latest_observation: dict[str, Any] | None = None
        self.last_confirmed_level: int | None = None
        self.pending_level: int | None = None
        self.pending_level_frame_ids: list[str] = []
        self.history: list[dict[str, Any]] = []

    def activate_session(self, session_epoch: str) -> bool:
        """Bind to an epoch; return True and clear evidence when it changes."""
        if not session_epoch:
            raise ValueError("a session epoch is required")
        changed = self.session_epoch is not None and self.session_epoch != session_epoch
        if self.session_epoch != session_epoch:
            self.session_epoch = session_epoch
            self.last_attempt_at = None
            self.latest_observation = None
            self.last_confirmed_level = None
            self.pending_level = None
            self.pending_level_frame_ids = []
            self.history = []
        return changed

    def due(self, now: float, *, force: bool = False) -> bool:
        if self.session_epoch is None:
            return False
        return bool(force or self.last_attempt_at is None
                    or now - self.last_attempt_at >= self.interval_seconds)

    def note_request_started(self, now: float) -> None:
        if self.session_epoch is None:
            raise RuntimeError("activate a session before requesting a level observation")
        self.last_attempt_at = now

    def record(self, *, decision, frame, now: datetime | None = None) -> dict[str, Any] | None:
        """Accept a fresh matching Sage answer, or return None when stale/mismatched."""
        envelope = getattr(decision, "envelope", None)
        session_epoch = getattr(envelope, "session_epoch", None)
        request_id = getattr(envelope, "request_id", None)
        if (session_epoch != self.session_epoch or not request_id
                or getattr(envelope, "captured_frame_id", None) != frame.frame_id):
            return None
        age = _frame_age_seconds(frame, now)
        if age > self.max_frame_age_seconds:
            return None
        chosen = getattr(decision, "chosen", None)
        if chosen == "player_level_unknown":
            level = None
            status = "unknown"
        elif isinstance(chosen, str) and chosen.startswith("player_level_"):
            try:
                level = int(chosen.removeprefix("player_level_"))
            except ValueError:
                return None
            if not 1 <= level <= 10:
                return None
            status = "observed"
        else:
            return None

        previous = self.last_confirmed_level
        level_change = None
        if level is not None:
            if self.required_readings == 1 and (previous is None or level > previous):
                level_change = ("baseline_confirmed_one_fresh_frame" if previous is None
                                else "higher_displayed_level_confirmed_one_fresh_frame")
                self.last_confirmed_level = level
                self.pending_level = None
                self.pending_level_frame_ids = []
            elif previous is None:
                if self.pending_level == level and frame.frame_id not in self.pending_level_frame_ids:
                    self.pending_level_frame_ids.append(frame.frame_id)
                else:
                    self.pending_level = level
                    self.pending_level_frame_ids = [frame.frame_id]
                if len(self.pending_level_frame_ids) >= 2:
                    level_change = "baseline_confirmed_two_fresh_frames"
                    self.last_confirmed_level = level
                    self.pending_level = None
                    self.pending_level_frame_ids = []
                else:
                    level_change = "baseline_candidate_one_fresh_frame"
            elif level > previous:
                if self.pending_level == level and frame.frame_id not in self.pending_level_frame_ids:
                    self.pending_level_frame_ids.append(frame.frame_id)
                else:
                    self.pending_level = level
                    self.pending_level_frame_ids = [frame.frame_id]
                if len(self.pending_level_frame_ids) >= 2:
                    level_change = "higher_displayed_level_confirmed_two_fresh_frames"
                    self.last_confirmed_level = level
                    self.pending_level = None
                    self.pending_level_frame_ids = []
                else:
                    level_change = "higher_displayed_level_candidate_one_fresh_frame"
            elif level < previous:
                self.pending_level = None
                self.pending_level_frame_ids = []
                level_change = "lower_displayed_level_check_identity_or_session"
            else:
                self.pending_level = None
                self.pending_level_frame_ids = []
                level_change = "same_displayed_level"
        else:
            self.pending_level = None
            self.pending_level_frame_ids = []
        record = {
            "status": status,
            "level": level,
            "provider": "sage",
            "level_change_vs_same_session_observation": level_change,
            "previous_level": previous,
            "frame_id": frame.frame_id,
            "captured_at": frame.captured_at,
            "frame_age_seconds": round(age, 3),
            "request_id": request_id,
            "session_epoch": session_epoch,
            "probability": getattr(decision, "probability", None),
            "model": getattr(decision, "model", None),
            "source": "Sage visual estimate of player level badge in the isolated player-frame crop",
            "limitations": [
                "not a numeric XP reading",
                "higher displayed level is not proof of when or how XP was gained",
                "same-frame image and Sage selection remain fallible",
            ],
        }
        self.latest_observation = record
        self.history = (self.history + [record])[-24:]
        return dict(record)
