from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
from typing import Any

import yaml


DEFAULT_PROFILE = Path("profiles/template.yaml")


@dataclass(frozen=True)
class Profile:
    path: Path
    values: dict[str, Any]

    @property
    def edition(self) -> str:
        return str(self.values.get("game", {}).get("edition", ""))

    @property
    def environment(self) -> str:
        return str(self.values.get("game", {}).get("environment", ""))

    @property
    def window_id(self) -> int | None:
        value = self.values.get("client", {}).get("window_id")
        return int(value) if value is not None else None

    @property
    def calibration(self) -> dict[str, Any]:
        return self.values.get("calibration", {})


def load_profile(path: Path = DEFAULT_PROFILE) -> Profile:
    values = yaml.safe_load(path.read_text())
    if not isinstance(values, dict):
        raise ValueError(f"Profile must be a YAML mapping: {path}")
    edition = values.get("game", {}).get("edition")
    character_class = values.get("character", {}).get("class")
    if edition != "wow_forever":
        raise ValueError("Profile game.edition must be 'wow_forever'")
    if character_class != "priest":
        raise ValueError("Initial profile character.class must be 'priest'")
    from sage_wow.agent.ui_layout import validate
    validate(values.get("calibration", {}).get("ui_layout"))
    return Profile(path=path, values=values)


def save_profile(profile: Profile) -> None:
    profile.path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=profile.path.parent, delete=False) as stream:
            temp_path = Path(stream.name)
            yaml.safe_dump(profile.values, stream, sort_keys=False)
        os.replace(temp_path, profile.path)
    except BaseException:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        raise


def session_data_dir(base: Path, profile: Profile) -> Path:
    """Keep Forever environment, phase, region, ruleset and character state isolated."""
    game = profile.values.get("game", {})
    character = profile.values.get("character", {})
    identity = "-".join(str(part) for part in (character.get("name"), character.get("surname")) if part)
    parts = ["wow_forever", game.get("environment") or "environment-unknown",
             game.get("content_phase") or "phase-unknown", game.get("region") or "region-unknown",
             game.get("ruleset") or "ruleset-unknown",
             (game.get("beta_test_realm") or "beta-realm-unknown") if game.get("environment") == "beta" else "no-realm",
             identity or "character-unconfigured"]
    safe_parts = [re.sub(r"[^A-Za-z0-9_.-]+", "_", str(part)).strip("._") or "unknown" for part in parts]
    return base.joinpath(*safe_parts)
