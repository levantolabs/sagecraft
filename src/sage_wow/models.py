from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Frame:
    frame_id: str
    captured_at: str
    source: str
    width: int
    height: int
    image_path: str | None = None
    image_data: bytes | None = None

    @classmethod
    def create(cls, source: str, width: int, height: int, **kwargs: Any) -> "Frame":
        return cls(str(uuid4()), utc_now(), source, width, height, **kwargs)


@dataclass(frozen=True)
class Event:
    event_id: str
    occurred_at: str
    event_type: str
    payload: dict[str, Any]

    @classmethod
    def create(cls, event_type: str, payload: dict[str, Any]) -> "Event":
        return cls(str(uuid4()), utc_now(), event_type, payload)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
