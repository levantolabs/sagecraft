from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sage_wow.models import Event


class EventStore:
    """Small durable event store; migrations are numbered and applied atomically."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self.connection:
            self.connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
            applied = {row[0] for row in self.connection.execute("SELECT version FROM schema_migrations")}
            if 1 not in applied:
                self.connection.execute("""CREATE TABLE events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )""")
                self.connection.execute("CREATE INDEX events_time_idx ON events(occurred_at)")
                self.connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
            if 2 not in applied:
                self.connection.execute("""CREATE TABLE checkpoints (
                    checkpoint_key TEXT PRIMARY KEY,
                    saved_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )""")
                self.connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")

            if 3 not in applied:
                self.connection.execute("CREATE INDEX IF NOT EXISTS events_type_time_idx ON events(event_type, occurred_at DESC)")
                self.connection.execute("INSERT INTO schema_migrations(version) VALUES (3)")

    def append(self, event: Event) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO events(event_id, occurred_at, event_type, payload_json) VALUES (?, ?, ?, ?)",
                (event.event_id, event.occurred_at, event.event_type, json.dumps(event.payload)),
            )

    def recent(self, limit: int = 50) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT event_id, occurred_at, event_type, payload_json FROM events ORDER BY occurred_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"event_id": row[0], "occurred_at": row[1], "event_type": row[2], "payload": json.loads(row[3])}
            for row in reversed(rows)
        ]

    def save_checkpoint(self, key: str, payload: dict[str, object]) -> None:
        from sage_wow.models import utc_now
        with self.connection:
            self.connection.execute(
                "INSERT INTO checkpoints(checkpoint_key, saved_at, payload_json) VALUES (?, ?, ?) "
                "ON CONFLICT(checkpoint_key) DO UPDATE SET saved_at=excluded.saved_at, payload_json=excluded.payload_json",
                (key, utc_now(), json.dumps(payload)),
            )

    def load_checkpoint(self, key: str) -> dict[str, object] | None:
        row = self.connection.execute("SELECT payload_json FROM checkpoints WHERE checkpoint_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self) -> None:
        self.connection.close()
