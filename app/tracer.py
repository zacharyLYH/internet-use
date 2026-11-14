import json
import os
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

TRACE_DB_PATH = Path(os.getenv("TRACE_DB_PATH", "data/traces.sqlite3"))


@dataclass(frozen=True, slots=True)
class TraceEvent:
    event_type: str
    details: dict[str, Any]


def trace_event(event_type: str, **details: object) -> TraceEvent:
    return TraceEvent(event_type, dict(details))


def stage_event(stage: str, phase: str, **details: object) -> TraceEvent:
    return trace_event(f"{stage}.{phase}", **details)


def request_event(phase: str, **details: object) -> TraceEvent:
    return trace_event(f"request.{phase}", **details)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trace_events_request_order
    ON trace_events (request_id, created_at, id);
CREATE INDEX IF NOT EXISTS trace_events_created_at
    ON trace_events (created_at);
"""


class TraceError(RuntimeError):
    """Raised when a trace event cannot be written."""


_lock = threading.Lock()
_connection: sqlite3.Connection | None = None


def init_tracer(path: str | Path | None = None) -> None:
    global _connection
    with _lock:
        if _connection is not None:
            return
        db_path = Path(path or TRACE_DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(db_path), timeout=5.0, check_same_thread=False
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.executescript(_SCHEMA)
        connection.commit()
        try:
            db_path.chmod(0o600)
        except OSError:
            pass
        _connection = connection


def close_tracer() -> None:
    global _connection
    with _lock:
        connection, _connection = _connection, None
    if connection is not None:
        connection.close()


def write_events(
    request_id: UUID | str, events: Sequence[TraceEvent]
) -> None:
    for event in events:
        write_event(event.event_type, request_id, event.details)


def write_event(
    event_type: str,
    request_id: UUID | str,
    details: Mapping[str, Any],
) -> None:
    payload = json.dumps(dict(details), separators=(",", ":"), ensure_ascii=False)
    with _lock:
        connection = _connection
        if connection is None:
            raise TraceError("tracer is not initialized")
        try:
            connection.execute(
                "INSERT INTO trace_events (request_id, event_type, details_json)"
                " VALUES (?, ?, ?)",
                (str(request_id), event_type, payload),
            )
            connection.commit()
        except sqlite3.Error as exc:
            raise TraceError("trace write failed") from exc
