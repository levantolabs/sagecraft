"""Runner status and control files shared by the CLI, dashboard and overlay."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def write_command(data_dir: Path, command: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "control.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"command": command, "requested_at": time.time()}))
    os.replace(temp, path)


def read_status(data_dir: Path) -> dict[str, object]:
    try:
        return json.loads((data_dir / "status.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"state": "STOPPED", "updated_at": None, "pid": None}
