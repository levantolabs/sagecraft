"""Focused quest/dialog evidence and fail-closed V2 click continuity checks.

Dialog layout bounds are profile-calibrated normalized pixels. A montage crop
helps Sage inspect the source screenshot, but is not itself authorization to
click and does not map click coordinates. The guard compares pixels only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any




@dataclass(frozen=True)
class DialogPanelBounds:
    left: float
    top: float
    right: float
    bottom: float

    @classmethod
    def parse(cls, value: Any) -> "DialogPanelBounds | None":
        if not isinstance(value, dict):
            return None
        coordinates = [value.get(key) for key in ("left", "top", "right", "bottom")]
        if any(not isinstance(item, (int, float)) or isinstance(item, bool)
               or not math.isfinite(float(item)) for item in coordinates):
            return None
        left, top, right, bottom = map(float, coordinates)
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            return None
        if right - left < .08 or bottom - top < .06:
            return None
        return cls(left, top, right, bottom)

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (round(self.left * width), round(self.top * height),
                round(self.right * width), round(self.bottom * height))


