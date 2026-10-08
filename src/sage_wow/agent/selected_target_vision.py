"""Observation-only selected-target HUD contract for a caller-supplied frame.

The selected target is identified from the target HUD, not a nearby world name,
tooltip, corpse, or quest text. Eligibility remains a separate gameplay policy.
"""
from __future__ import annotations

import json
import asyncio
import hashlib
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from sage_wow.sage.client import BatchQuestion, BatchRequestGroup, ImageBatchContent


PRESENCE = ("present", "absent", "unknown")
KIND = ("creature", "player", "friendly_or_self", "unknown")
LIFE = ("alive", "dead", "unknown")


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_EDGE = 8192
MAX_IMAGE_PIXELS = 4096 * 4096
MAX_TIMEOUT_SECONDS = 30.0


class VisionUnavailable(RuntimeError):
    """Sanitized observation failure; never includes request secrets or bodies."""


def _read_image(path: Path) -> tuple[bytes, str]:
    try:
        data = path.read_bytes()
    except (OSError, TypeError, ValueError):
        raise VisionUnavailable("observation image is unavailable") from None
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise VisionUnavailable("observation image size is outside the allowed limit")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    else:
        raise VisionUnavailable("observation image must be JPEG or PNG")
    try:
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise VisionUnavailable("observation image must be JPEG or PNG")
            width, height = image.size
            if (width < 1 or height < 1 or max(width, height) > MAX_IMAGE_EDGE
                    or width * height > MAX_IMAGE_PIXELS):
                raise VisionUnavailable("observation image dimensions are outside the allowed limit")
            image.verify()
    except VisionUnavailable:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise VisionUnavailable("observation image is invalid or unreadable") from None
    return data, mime

PROMPT = (
    "Observe only the selected-target HUD on this exact game screenshot. "
    "Report whether a selected target frame is visibly present, its name and "
    "level if readable, whether it is a creature, player, or friendly/self, "
    "and whether its target health bar indicates alive or dead. "
    "The target HUD is distinct from the player's portrait, a world nameplate, "
    "a tooltip, a corpse in the world, quest text, or a nearby player's combat. "
    "A visible target outside any allowed level band is still present. "
    "Do not infer target eligibility, range, damage credit, or an action. "
    "If the HUD is obscured or text unreadable, use unknown or null. "
    "Do not choose, recommend, or authorize gameplay input."
    " If the supplied image is a composite, use its top/current screenshot and "
    "enlarged target HUD only; any bottom BEFORE screenshot is historical. "
    "For an absent selected HUD return null name/level and unknown kind/life."
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "selected_hud": {"type": "string", "enum": list(PRESENCE)},
        "name": {"type": ["string", "null"]},
        "level": {"type": ["integer", "null"]},
        "target_kind": {"type": "string", "enum": list(KIND)},
        "life_state": {"type": "string", "enum": list(LIFE)},
    },
    "required": ["selected_hud", "name", "level", "target_kind", "life_state"],
    "additionalProperties": False,
}


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


def parse_observation(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (TypeError, ValueError):
        raise VisionUnavailable("selected-target response was not valid structured JSON") from None
    if not isinstance(value, dict) or set(value) != set(SCHEMA["required"]):
        raise VisionUnavailable("selected-target response had missing or extra fields")
    if (value["selected_hud"] not in PRESENCE or value["target_kind"] not in KIND
            or value["life_state"] not in LIFE):
        raise VisionUnavailable("selected-target response contained an invalid field value")
    name, level = value["name"], value["level"]
    if name is not None and (not isinstance(name, str) or not name.strip()
                             or len(name) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise VisionUnavailable("selected-target response contained an invalid name")
    if level is not None and (isinstance(level, bool) or not isinstance(level, int)
                              or not 1 <= level <= 100):
        raise VisionUnavailable("selected-target response contained an invalid level")
    if value["selected_hud"] == "absent" and (name is not None or level is not None
                                                or value["target_kind"] != "unknown"
                                                or value["life_state"] != "unknown"):
        raise VisionUnavailable("absent selected-target response contradicted its details")
    return value


async def observe(image_path: Path, *, expected_name: str = "unknown",
                  model: str = "levanto-sage", timeout_seconds: float = 12,
                  backend: str = "sage", sage_client=None,
                  frame_id: str | None = None, captured_at: str | None = None,
                  scope_id: str | None = None, latency_mode: str = "quality") -> dict[str, Any]:
    """Return attributed facts, never an action or target-eligibility verdict.

    The caller must bind this result to the source frame and check freshness
    before using it in a decision. This one-shot call has no retry loop.
    """
    started = time.perf_counter()
    if (isinstance(timeout_seconds,bool) or not isinstance(timeout_seconds,(int,float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS):
        raise VisionUnavailable("selected-target observation timeout is outside the allowed limit")
    data, _ = _read_image(Path(image_path))
    digest = hashlib.sha256(data).hexdigest()
    if backend == "sage":
        if sage_client is None:
            raise VisionUnavailable("selected-target Sage client is unavailable")
        async with asyncio.timeout(timeout_seconds):
            response = await sage_client.decide_batch((BatchRequestGroup(
                ImageBatchContent(Path(image_path), PROMPT), sage_questions()),),
                reasoning="off", latency_mode=latency_mode)
        answers = {answer.question_id: answer.chosen if answer.ok else None
                   for answer in response.answers}
        value = {"selected_hud": answers.get("selected_hud") or "unknown",
            "name": NAME_CHOICES.get(answers.get("name")),
            "level": int(answers["level"]) if answers.get("level") in LEVEL_CHOICES else None,
            "target_kind": answers.get("target_kind") or "unknown",
            "life_state": answers.get("life_state") or "unknown"}
        # Independent questions may disagree. Disagreement is uncertainty,
        # never an invented absence or a silently discarded visible target.
        if value["selected_hud"] == "absent" and any(
            (value["name"], value["level"], value["target_kind"] != "unknown",
             value["life_state"] != "unknown")):
            value = {"selected_hud":"unknown", "name":None, "level":None,
                     "target_kind":"unknown", "life_state":"unknown"}
        result = {"observation": parse_observation(json.dumps(value)),
            "configured_model": "levanto-sage", "actual_model": response.meta.get("model"),
            "model_attribution": "server_echoed" if response.meta.get("model") else "not_reported",
            "provider_request_id": None, "usage": response.meta.get("usage"),
            "sage_answers": response.raw, "latency_mode": latency_mode}
    else:
        raise VisionUnavailable("selected-target backend is unavailable")
    if hashlib.sha256(Path(image_path).read_bytes()).hexdigest() != digest:
        raise VisionUnavailable("selected-target source image changed during observation")
    return dict(result, backend=backend, frame_id=frame_id, captured_at=captured_at,
        scope_id=scope_id, image_sha256=digest, total_ms=(time.perf_counter()-started)*1000)


NAME_CHOICES = {"ragged_young_wolf":"Ragged Young Wolf", "ragged_timber_wolf":"Ragged Timber Wolf"}
LEVEL_CHOICES = tuple(str(i) for i in range(1, 6))


def prepare_target_image(frame_path: Path, box, output: Path) -> Path:
    """Retain the evaluated image contract: calibrated target crop enlarged 3x.

    The box is pixel geometry in the caller's source frame, not model output.
    No capture, coordinate inference, or native operation is performed here.
    """
    if (not isinstance(box,(tuple,list)) or len(box) != 4
            or any(isinstance(v,bool) or not isinstance(v,int) for v in box)):
        raise ValueError("target crop must contain four integer pixel coordinates")
    _read_image(Path(frame_path))
    with Image.open(frame_path) as image:
        left,top,right,bottom=box
        if not 0 <= left < right <= image.width or not 0 <= top < bottom <= image.height:
            raise ValueError("target crop is outside the source frame")
        if (max((right-left)*3,(bottom-top)*3) > MAX_IMAGE_EDGE
                or (right-left)*(bottom-top)*9 > MAX_IMAGE_PIXELS):
            raise ValueError("enlarged target crop exceeds image limits")
        cropped=image.convert("RGB").crop(tuple(box))
        output=Path(output)
        cropped.resize((cropped.width*3,cropped.height*3),Image.Resampling.LANCZOS).save(output,format="PNG")
    return output


def sage_questions() -> tuple[BatchQuestion, ...]:
    """Focused, independent facts. No level band or gameplay objective is supplied."""
    definitions = (
        ("selected_hud", "Is the SELECTED TARGET portrait with its attached name/health HUD present? Ignore the player's own portrait, world labels, tooltips and any BEFORE image.",
         (("present","A selected-target portrait and attached unit HUD are visibly present."),
          ("absent","No selected-target HUD is present in the current screenshot."),
          ("unknown","Cannot judge selected-target HUD presence."))),
        ("name", "Read ONLY the selected-target HUD name, not a world label. Which exact name is visible?",
         tuple(NAME_CHOICES.items()) + (("other_or_unknown","Other name, unreadable name, or no selected HUD."),)),
        ("level", "Read ONLY the numeral in the selected target's level badge, beside its portrait. Do not use the player's own badge or the BEFORE image.",
         tuple((v, f"The selected target level badge visibly reads {v}.") for v in LEVEL_CHOICES)
         + (("unknown","No readable selected target level from 1 through 5."),)),
        ("target_kind", "What kind of unit is in the selected-target HUD? Use unknown if no HUD or identity is unclear.",
         (("creature","A non-player creature such as a wolf."),("player","Another player character."),
          ("friendly_or_self","The controlled player or a friendly NPC."),("unknown","No selected HUD or kind unclear."))),
        ("life_state", "Read the selected-target health bar or explicit Dead label. Is that selected unit alive or dead? A world corpse alone cannot answer this question.",
         (("alive","Selected HUD has remaining health; the selected unit is alive."),
          ("dead","Selected HUD explicitly says Dead or clearly shows death."),
          ("unknown","No selected HUD, or life state unclear."))),
    )
    return tuple(BatchQuestion(key, prompt, tuple({"option": option, "description": description}
        for option, description in options)) for key, prompt, options in definitions)


def accept_observation(result: dict[str, Any], *, frame_id: str, scope_id: str,
                       now_epoch: float, max_age_seconds: float,
                       image_sha256: str | None = None) -> dict[str, Any] | None:
    """Accept same-frame facts only; caller still owns fresh dispatch guards."""
    try:
        if (not frame_id or not scope_id or result.get("frame_id") != frame_id
                or result.get("scope_id") != scope_id):
            return None
        captured = datetime.fromisoformat(result["captured_at"])
        if captured.tzinfo is None or not math.isfinite(now_epoch) or not 0 < max_age_seconds < 300:
            return None
        age = now_epoch - captured.timestamp()
        if not 0 <= age <= max_age_seconds:
            return None
        digest = result.get("image_sha256")
        if (not isinstance(digest,str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
                or image_sha256 is not None and digest != image_sha256):
            return None
        if result.get("backend") != "sage" or not result.get("configured_model"):
            return None
        return parse_observation(json.dumps(result["observation"]))
    except (KeyError, TypeError, ValueError, VisionUnavailable):
        return None


def eligibility(observation: dict[str, Any], *, min_level: int, max_level: int,
                allowed_names: tuple[str, ...] = ()) -> str:
    """Policy verdict kept separate from visible presence and numeral reading."""
    value = parse_observation(json.dumps(observation))
    if value["selected_hud"] == "absent":
        return "absent"
    if value["selected_hud"] != "present":
        return "unknown"
    if value["target_kind"] in {"player","friendly_or_self"} or value["life_state"] == "dead":
        return "ineligible"
    if value["level"] is not None and not min_level <= value["level"] <= max_level:
        return "ineligible"
    if value["name"] is not None and allowed_names and value["name"] not in allowed_names:
        return "ineligible"
    if (value["target_kind"] != "creature" or value["life_state"] != "alive"
            or value["level"] is None or value["name"] is None):
        return "unknown"
    return "eligible"
