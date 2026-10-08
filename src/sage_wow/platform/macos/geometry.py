from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def image_to_desktop(self, x: float, y: float, image_width: int, image_height: int) -> tuple[int, int]:
        """Map image pixels to desktop event coordinates, preserving Retina scale."""
        if image_width <= 0 or image_height <= 0:
            raise ValueError("Image dimensions must be positive")
        return (
            round(self.x + x * self.width / image_width),
            round(self.y + y * self.height / image_height),
        )
