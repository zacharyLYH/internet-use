import json
import shutil
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from app.tracer import (
    TraceError,
    close_tracer,
    init_tracer,
    stage_event,
    request_event,
    trace_event,
    write_event,
    write_events,
)


@pytest.fixture(autouse=True)
def clean_tracer(tmp_path: Path) -> Iterator[None]:
    close_tracer()
    try:
        yield
    finally:
        close_tracer()
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_writes_ordered_events_with_json_details(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "traces.sqlite3"
    init_tracer(db_path)
    request_id = uuid4()

    write_event("api.ingress", request_id, {"method": "POST", "path": "/extract"})
    write_event("request.completed", request_id, {"counts": {"extracted": 1}})
    write_event("other.event", "not-a-uuid", {"ok": True})
    close_tracer()

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT request_id, event_type, details_json FROM trace_events"
            " ORDER BY id"
        ).fetchall()

    assert rows[0][:2] == (str(request_id), "api.ingress")
    assert json.loads(rows[0][2]) == {"method": "POST", "path": "/extract"}
    assert rows[1][1] == "request.completed"
    assert rows[2][0] == "not-a-uuid"
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_write_before_init_raises() -> None:
    with pytest.raises(TraceError):
        write_event("api.ingress", uuid4(), {})


def test_generic_event_builders() -> None:
    assert trace_event("api.ingress", method="POST").details == {
        "method": "POST"
    }
    stage = stage_event("normalization", "completed", page=1, selected_count=2)
    assert stage.event_type == "normalization.completed"
    assert stage.details == {"page": 1, "selected_count": 2}
    request = request_event("failed", error_code="search_failed")
    assert request.event_type == "request.failed"
    assert request.details == {"error_code": "search_failed"}


def test_write_events_preserves_order(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.sqlite3"
    init_tracer(db_path)
    request_id = uuid4()

    write_events(
        request_id,
        (
            trace_event("first", value=1),
            stage_event("fetch", "started", page=1),
            request_event("completed", outcome="completed"),
        ),
    )
    close_tracer()

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT event_type FROM trace_events ORDER BY id"
        ).fetchall()

    assert [row[0] for row in rows] == [
        "first",
        "fetch.started",
        "request.completed",
    ]
