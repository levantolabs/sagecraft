"""Allowlisted one-step camera zoom commands; no game-state reads or scripts."""

CAMERA_ZOOM_COMMANDS = {
    ("in", 1): "/run CameraZoomIn(1)",
    ("out", 1): "/run CameraZoomOut(1)",
}


def camera_zoom_command(binding: dict) -> str | None:
    """Resolve only a single bounded zoom pulse with no extra parameters."""
    if set(binding) != {"type", "direction", "steps"} or binding.get("type") != "camera_zoom":
        return None
    direction, steps = binding.get("direction"), binding.get("steps")
    if isinstance(steps, bool) or not isinstance(steps, int):
        return None
    return CAMERA_ZOOM_COMMANDS.get((direction, steps))
