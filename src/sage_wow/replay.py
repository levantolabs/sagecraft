from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from sage_wow.models import Event, Frame


class ReplaySession:
    def __init__(self, path: Path):
        self.path = path

    def events(self) -> Iterator[Event]:
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("type") == "frame":
                continue
            yield Event(row["event_id"], row["occurred_at"], row["event_type"], row.get("payload", {}))

    def frames(self) -> Iterator[Frame]:
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("type") != "frame":
                continue
            image_path = (self.path.parent / row["image_path"]).resolve() if row.get("image_path") else None
            if image_path and not image_path.is_file():
                raise FileNotFoundError(f"Replay frame image missing: {image_path}")
            yield Frame(row["frame_id"], row["captured_at"], "replay", row["width"], row["height"], str(image_path) if image_path else None)
