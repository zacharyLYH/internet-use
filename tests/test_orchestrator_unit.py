import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from app.cleaner import CleanFailure
from app.fetcher import FetchFailure, FetchOutcome, PageSnapshot
from app.orchestrator import (
    Orchestrator,
    QueueExpiredError,
    QueueFullError,
    RequestQueue,
)
from app.pipeline import (
    ExtractRequest,
    ExtractResponse,
    OrchestratorConfig,
    PipelineError,
)
from app.relevance import RelevanceFailure, RelevanceSelection
from app.search import SearchError
from app.tracer import close_tracer, init_tracer
from tests.test_utils import (
    ARTICLE_HTML,
    read_trace_events,
    remove_sqlite_database,
)


def _config(**overrides: object) -> OrchestratorConfig:
    values: dict[str, object] = {
        "search_timeout_s": 3.0,
        "fetch_timeout_s": 4.0,
        "search_retries": 0,
        "fetch_retries": 0,
        "retry_backoff_s": 0.0,
        "page_delay_min_s": 0.0,
        "page_delay_max_s": 0.0,
    }
    values.update(overrides)
    return OrchestratorConfig(**values)  # type: ignore[arg-type]


def _hit(url: str, title: str) -> dict[str, object]:
    return {
        "title": title,
        "url": url,
        "content": f"snippet for {title}",
        "engines": ["test"],
        "category": "general",
        "score": 1.0,
    }


def _snapshot(url: str, html: str = ARTICLE_HTML) -> PageSnapshot:
    return PageSnapshot(
        requested_url=url,
        final_url=url,
        title="Article",
        status_code=200,
        content_type="text/html",
        raw_page_html=html,
        render_state="complete",
    )


@pytest.fixture
def trace_db(tmp_path: Path) -> Iterator[Path]:
    close_tracer()
    path = tmp_path / "traces.sqlite3"
    init_tracer(path)
    try:
        yield path
    finally:
        close_tracer()
        remove_sqlite_database(path)


def test_query_paginates_dedupes_and_cleans(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    search_calls: list[tuple[str, int, float]] = []
    fetch_calls: list[tuple[str, float | None]] = []
    pages = {
        1: [_hit("https://example.com/a", "A")],
        2: [_hit("https://example.com/a", "A"), _hit("https://example.com/b", "B")],
    }

    def fake_search(
        query: str, *, pageno: int = 1, timeout_s: float = 0.0
    ) -> dict[str, object]:
        search_calls.append((query, pageno, timeout_s))
        return {"results": pages[pageno]}

    def fake_fetch(
        url: str, *, total_timeout_s: float | None = None
    ) -> FetchOutcome:
        fetch_calls.append((url, total_timeout_s))
        return _snapshot(url)

    monkeypatch.setattr("app.orchestrator.fetch_search_response", fake_search)
    monkeypatch.setattr("app.orchestrator.fetch_target_page", fake_fetch)

    request_id = uuid4()
    response = Orchestrator(_config()).run(
        request_id,
        ExtractRequest(query=" query ", max_results=2, max_pages=3),
    )

    assert [call[1] for call in search_calls] == [1, 2]
    assert search_calls[0][0] == "query"
    assert all(call[2] == 3.0 for call in search_calls)
    assert [call[0] for call in fetch_calls] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert all(call[1] == 4.0 for call in fetch_calls)
    assert response.outcome == "completed"
    assert response.counts.selected == 2
    assert response.counts.fetched == 2
    assert response.counts.extracted == 2
    assert response.pages[0].markdown.startswith("# Article title")
    assert [event for event, _ in read_trace_events(trace_db, request_id)] == [
        "request.started",
        "search.started",
        "search.completed",
        "normalization.started",
        "normalization.completed",
        "search.started",
        "search.completed",
        "normalization.started",
        "normalization.completed",
        "fetch.started",
        "fetch.completed",
        "extraction.started",
        "extraction.completed",
        "fetch.started",
        "fetch.completed",
        "extraction.started",
        "extraction.completed",
        "request.completed",
    ]


def test_search_retries_retryable_failure(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []

    def fake_search(
        query: str, *, pageno: int = 1, timeout_s: float = 0.0
    ) -> dict[str, object]:
        attempts.append(pageno)
        if len(attempts) < 3:
            raise SearchError("temporary", retryable=True)
        return {"results": [_hit("https://example.com/a", "A")]}

    monkeypatch.setattr("app.orchestrator.fetch_search_response", fake_search)
    monkeypatch.setattr(
        "app.orchestrator.fetch_target_page",
        lambda url, *, total_timeout_s=None: _snapshot(url),
    )

    request_id = uuid4()
    response = Orchestrator(_config(search_retries=2)).run(
        request_id, ExtractRequest(query="q", max_results=1, max_pages=1)
    )

    assert attempts == [1, 1, 1]
    assert response.outcome == "completed"
    events = read_trace_events(trace_db, request_id)
    retry_events = [details for event, details in events if event == "retry.scheduled"]
    assert retry_events == [
        {"stage": "search", "page": 1, "attempt": 2},
        {"stage": "search", "page": 1, "attempt": 3},
    ]


def test_search_nonretryable_failure_does_not_retry(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []

    def fake_search(
        query: str, *, pageno: int = 1, timeout_s: float = 0.0
    ) -> dict[str, object]:
        attempts.append(pageno)
        raise SearchError("bad response", retryable=False)

    monkeypatch.setattr("app.orchestrator.fetch_search_response", fake_search)

    request_id = uuid4()
    with pytest.raises(PipelineError) as raised:
        Orchestrator(_config(search_retries=2)).run(
            request_id, ExtractRequest(query="q", max_results=1, max_pages=1)
        )

    assert raised.value.code == "search_failed"
    assert raised.value.status_code == 502
    assert attempts == [1]
    events = read_trace_events(trace_db, request_id)
    assert [event for event, _ in events] == [
        "request.started",
        "search.started",
        "search.failed",
        "request.failed",
    ]


def test_normalization_failure_is_reported(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.orchestrator.fetch_search_response",
        lambda query, *, pageno=1, timeout_s=0.0: {"results": "bad"},
    )

    request_id = uuid4()
    with pytest.raises(PipelineError) as raised:
        Orchestrator(_config()).run(
            request_id, ExtractRequest(query="q", max_results=1, max_pages=1)
        )

    assert raised.value.code == "normalization_failed"
    events = read_trace_events(trace_db, request_id)
    assert [event for event, _ in events] == [
        "request.started",
        "search.started",
        "search.completed",
        "normalization.started",
        "normalization.failed",
        "request.failed",
    ]


def test_fetch_retries_retryable_failure(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes: list[FetchOutcome] = [
        FetchFailure("https://example.com/a", "network_error", "Page could not be reached."),
        _snapshot("https://example.com/a"),
    ]
    attempts: list[str] = []

    def fake_fetch(
        url: str, *, total_timeout_s: float | None = None
    ) -> FetchOutcome:
        attempts.append(url)
        return outcomes.pop(0)

    monkeypatch.setattr("app.orchestrator.fetch_target_page", fake_fetch)

    request_id = uuid4()
    response = Orchestrator(_config(fetch_retries=1)).run(
        request_id, ExtractRequest(url="https://example.com/a")
    )

    assert attempts == ["https://example.com/a", "https://example.com/a"]
    assert response.outcome == "completed"
    retry_events = [
        details
        for event, details in read_trace_events(trace_db, request_id)
        if event == "retry.scheduled"
    ]
    assert retry_events == [{"stage": "fetch", "ordinal": 1, "attempt": 2}]


def test_nonretryable_fetch_failure_is_not_retried(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def fake_fetch(
        url: str, *, total_timeout_s: float | None = None
    ) -> FetchOutcome:
        attempts.append(url)
        return FetchFailure(url, "unsupported_content_type", "Main document is not HTML.")

    monkeypatch.setattr("app.orchestrator.fetch_target_page", fake_fetch)

    request_id = uuid4()
    response = Orchestrator(_config(fetch_retries=2)).run(
        request_id, ExtractRequest(url="https://example.com/a")
    )

    assert attempts == ["https://example.com/a"]
    assert response.outcome == "completed_with_errors"
    assert response.counts.failed == 1
    assert response.counts.not_attempted == 0
    assert response.pages[0].error is not None
    assert response.pages[0].error.code == "unsupported_content_type"
    assert [event for event, _ in read_trace_events(trace_db, request_id)][-2:] == [
        "fetch.failed",
        "request.completed_with_errors",
    ]


def test_clean_failure_is_reported(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.orchestrator.fetch_target_page",
        lambda url, *, total_timeout_s=None: _snapshot(url),
    )
    monkeypatch.setattr(
        "app.orchestrator.clean_to_markdown",
        lambda html, url: CleanFailure("extraction_error", "Content extraction failed."),
    )

    request_id = uuid4()
    response = Orchestrator(_config()).run(
        request_id, ExtractRequest(url="https://example.com/a")
    )

    assert response.outcome == "completed_with_errors"
    assert response.counts.failed == 1
    assert response.pages[0].extraction_state == "failed"
    assert response.pages[0].error is not None
    events = read_trace_events(trace_db, request_id)
    assert [event for event, _ in events][-3:] == [
        "extraction.started",
        "extraction.failed",
        "request.completed_with_errors",
    ]


def test_no_hits_stops_after_first_page(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []

    def fake_search(
        query: str, *, pageno: int = 1, timeout_s: float = 0.0
    ) -> dict[str, object]:
        attempts.append(pageno)
        return {"results": []}

    monkeypatch.setattr("app.orchestrator.fetch_search_response", fake_search)

    response = Orchestrator(_config()).run(
        uuid4(), ExtractRequest(query="q", max_results=3, max_pages=3)
    )

    assert attempts == [1]
    assert response.outcome == "completed"
    assert response.pages == []
    assert response.counts.selected == 0


def test_invalid_request_is_rejected_before_work(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.orchestrator.fetch_target_page",
        lambda url, *, total_timeout_s=None: pytest.fail("fetch should not run"),
    )

    request_id = uuid4()
    with pytest.raises(PipelineError) as raised:
        Orchestrator(_config()).run(
            request_id, ExtractRequest(query="q", url="https://example.com/a")
        )

    assert raised.value.status_code == 400
    assert read_trace_events(trace_db, request_id) == []


class _SlowOrchestrator:
    def __init__(self, delay_s: float = 0.4) -> None:
        self._delay_s = delay_s

    def run(self, request_id: UUID, request: ExtractRequest) -> ExtractResponse:
        time.sleep(self._delay_s)
        return ExtractResponse(request_id=str(request_id), outcome="completed")


def test_queue_expiry_cancels_waiting_request(trace_db: Path) -> None:
    request_queue = RequestQueue(
        cast(Orchestrator, _SlowOrchestrator()), max_size=2, wait_s=0.15
    )
    request_queue.start()
    first_result: list[ExtractResponse] = []

    def submit_first() -> None:
        first_result.append(
            request_queue.submit(
                uuid4(), ExtractRequest(url="https://example.com/first")
            )
        )

    first = threading.Thread(target=submit_first)
    first.start()
    time.sleep(0.05)
    second_id = uuid4()
    try:
        with pytest.raises(QueueExpiredError):
            request_queue.submit(
                second_id, ExtractRequest(url="https://example.com/second")
            )
    finally:
        first.join(timeout=3)
        request_queue.stop()

    assert not first.is_alive()
    assert len(first_result) == 1
    events = read_trace_events(trace_db, second_id)
    assert [event for event, _ in events] == ["request.cancelled"]
    assert events[0][1] == {"reason": "queue_expired"}


def test_queue_full_is_reported(trace_db: Path) -> None:
    request_queue = RequestQueue(
        cast(Orchestrator, _SlowOrchestrator(delay_s=0.5)),
        max_size=1,
        wait_s=2.0,
    )
    request_queue.start()
    results: list[ExtractResponse] = []

    def submit(request_id: UUID) -> None:
        results.append(
            request_queue.submit(request_id, ExtractRequest(url="https://example.com/a"))
        )

    first = threading.Thread(target=submit, args=(uuid4(),))
    first.start()
    time.sleep(0.1)
    second = threading.Thread(target=submit, args=(uuid4(),))
    second.start()
    time.sleep(0.1)
    try:
        with pytest.raises(QueueFullError):
            request_queue.submit(
                uuid4(), ExtractRequest(url="https://example.com/third")
            )
    finally:
        first.join(timeout=3)
        second.join(timeout=3)
        request_queue.stop()

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(results) == 2


def test_queue_runs_worker_shutdown_callback(trace_db: Path) -> None:
    called: list[bool] = []
    request_queue = RequestQueue(
        cast(Orchestrator, _SlowOrchestrator(delay_s=0.0)),
        max_size=1,
        wait_s=1.0,
        on_worker_stop=lambda: called.append(True),
    )
    request_queue.start()

    request_queue.stop()

    assert called == [True]


def test_active_job_waits_past_queue_deadline(trace_db: Path) -> None:
    request_queue = RequestQueue(
        cast(Orchestrator, _SlowOrchestrator(delay_s=0.3)),
        max_size=1,
        wait_s=0.1,
    )
    request_queue.start()
    try:
        response = request_queue.submit(
            uuid4(), ExtractRequest(url="https://example.com/a")
        )
    finally:
        request_queue.stop()

    assert response.outcome == "completed"


def test_pagination_respects_max_pages(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []

    def fake_search(
        query: str, *, pageno: int = 1, timeout_s: float = 0.0
    ) -> dict[str, object]:
        attempts.append(pageno)
        return {
            "results": [
                _hit(f"https://example.com/{pageno}", f"Page {pageno}")
            ]
        }

    monkeypatch.setattr("app.orchestrator.fetch_search_response", fake_search)
    monkeypatch.setattr(
        "app.orchestrator.fetch_target_page",
        lambda url, *, total_timeout_s=None: _snapshot(url),
    )

    response = Orchestrator(_config()).run(
        uuid4(), ExtractRequest(query="q", max_results=10, max_pages=2)
    )

    assert attempts == [1, 2]
    assert response.counts.selected == 2


def test_search_retries_exhausted_fails(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []

    def fake_search(
        query: str, *, pageno: int = 1, timeout_s: float = 0.0
    ) -> dict[str, object]:
        attempts.append(pageno)
        raise SearchError("temporary", retryable=True)

    monkeypatch.setattr("app.orchestrator.fetch_search_response", fake_search)

    request_id = uuid4()
    with pytest.raises(PipelineError) as raised:
        Orchestrator(_config(search_retries=1)).run(
            request_id, ExtractRequest(query="q", max_results=1, max_pages=1)
        )

    assert raised.value.code == "search_failed"
    assert attempts == [1, 1]
    event_types = [event for event, _ in read_trace_events(trace_db, request_id)]
    assert event_types.count("retry.scheduled") == 1
    assert event_types[-1] == "request.failed"


def test_queued_requests_all_resolve(trace_db: Path) -> None:
    request_queue = RequestQueue(
        cast(Orchestrator, _SlowOrchestrator(delay_s=0.2)),
        max_size=3,
        wait_s=2.0,
    )
    request_queue.start()
    request_ids = [uuid4(), uuid4(), uuid4()]
    results: list[ExtractResponse] = []
    lock = threading.Lock()

    def submit(request_id: UUID) -> None:
        response = request_queue.submit(
            request_id, ExtractRequest(url="https://example.com/a")
        )
        with lock:
            results.append(response)

    threads = [
        threading.Thread(target=submit, args=(request_id,))
        for request_id in request_ids
    ]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join(timeout=3)
    finally:
        request_queue.stop()

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 3
    assert {response.request_id for response in results} == {
        str(request_id) for request_id in request_ids
    }


def test_llm_filter_drops_irrelevant_hits(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = [
        _hit("https://example.com/a", "A"),
        _hit("https://example.com/b", "B"),
        _hit("https://example.com/c", "C"),
    ]
    monkeypatch.setattr(
        "app.orchestrator.fetch_search_response",
        lambda query, *, pageno=1, timeout_s=0.0: {"results": hits},
    )
    fetch_calls: list[str] = []

    def fake_fetch(
        url: str, *, total_timeout_s: float | None = None
    ) -> FetchOutcome:
        fetch_calls.append(url)
        return _snapshot(url)

    monkeypatch.setattr("app.orchestrator.fetch_target_page", fake_fetch)
    seen: dict[str, object] = {}

    def fake_filter(
        query: str, candidates: list[object], *, timeout_s: float = 0.0
    ) -> RelevanceSelection:
        seen["query"] = query
        seen["urls"] = [hit["url"] for hit in candidates if isinstance(hit, dict)]
        seen["timeout"] = timeout_s
        return RelevanceSelection(
            frozenset(
                {
                    "https://example.com/a",
                    "https://example.com/c",
                }
            )
        )

    monkeypatch.setattr("app.orchestrator.select_relevant_hits", fake_filter)

    request_id = uuid4()
    response = Orchestrator(_config(llm_filter_enabled=True)).run(
        request_id,
        ExtractRequest(query=" python ", max_results=3, max_pages=1),
    )

    assert seen == {
        "query": "python",
        "urls": [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        ],
        "timeout": 10.0,
    }
    assert fetch_calls == [
        "https://example.com/a",
        "https://example.com/c",
    ]
    assert response.outcome == "completed"
    assert response.counts.selected == 2
    events = read_trace_events(trace_db, request_id)
    assert [event for event, _ in events][:7] == [
        "request.started",
        "search.started",
        "search.completed",
        "normalization.started",
        "normalization.completed",
        "relevance.started",
        "relevance.completed",
    ]
    assert dict(events)["relevance.completed"] == {
        "candidate_count": 3,
        "kept_count": 2,
        "dropped_count": 1,
        "kept": ["https://example.com/a", "https://example.com/c"],
        "dropped": ["https://example.com/b"],
    }


def test_llm_filter_failure_keeps_every_hit(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = [
        _hit("https://example.com/a", "A"),
        _hit("https://example.com/b", "B"),
    ]
    monkeypatch.setattr(
        "app.orchestrator.fetch_search_response",
        lambda query, *, pageno=1, timeout_s=0.0: {"results": hits},
    )
    fetch_calls: list[str] = []

    def fake_fetch(
        url: str, *, total_timeout_s: float | None = None
    ) -> FetchOutcome:
        fetch_calls.append(url)
        return _snapshot(url)

    monkeypatch.setattr("app.orchestrator.fetch_target_page", fake_fetch)
    monkeypatch.setattr(
        "app.orchestrator.select_relevant_hits",
        lambda query, candidates, *, timeout_s=0.0: RelevanceFailure(
            "llm_request_failed"
        ),
    )

    request_id = uuid4()
    response = Orchestrator(_config(llm_filter_enabled=True)).run(
        request_id,
        ExtractRequest(query="q", max_results=2, max_pages=1),
    )

    assert fetch_calls == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert response.counts.selected == 2
    events = read_trace_events(trace_db, request_id)
    assert [event for event, _ in events].count("relevance.failed") == 1
    assert dict(events)["relevance.failed"] == {
        "candidate_count": 2,
        "error_code": "llm_request_failed",
    }


def test_llm_filter_is_skipped_when_disabled(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = [
        _hit("https://example.com/a", "A"),
        _hit("https://example.com/b", "B"),
    ]
    monkeypatch.setattr(
        "app.orchestrator.fetch_search_response",
        lambda query, *, pageno=1, timeout_s=0.0: {"results": hits},
    )
    monkeypatch.setattr(
        "app.orchestrator.fetch_target_page",
        lambda url, *, total_timeout_s=None: _snapshot(url),
    )
    monkeypatch.setattr(
        "app.orchestrator.select_relevant_hits",
        lambda *args, **kwargs: pytest.fail("filter should not run"),
    )

    request_id = uuid4()
    Orchestrator(_config()).run(
        request_id,
        ExtractRequest(query="q", max_results=2, max_pages=1),
    )

    event_types = [
        event for event, _ in read_trace_events(trace_db, request_id)
    ]
    assert not [t for t in event_types if t.startswith("relevance.")]


def test_llm_filter_is_skipped_for_a_single_hit(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.orchestrator.fetch_search_response",
        lambda query, *, pageno=1, timeout_s=0.0: {
            "results": [_hit("https://example.com/a", "A")]
        },
    )
    monkeypatch.setattr(
        "app.orchestrator.fetch_target_page",
        lambda url, *, total_timeout_s=None: _snapshot(url),
    )
    monkeypatch.setattr(
        "app.orchestrator.select_relevant_hits",
        lambda *args, **kwargs: pytest.fail("filter should not run"),
    )

    request_id = uuid4()
    response = Orchestrator(_config(llm_filter_enabled=True)).run(
        request_id,
        ExtractRequest(query="q", max_results=1, max_pages=1),
    )

    assert response.counts.selected == 1
    event_types = [
        event for event, _ in read_trace_events(trace_db, request_id)
    ]
    assert not [t for t in event_types if t.startswith("relevance.")]


def test_low_value_hits_are_dropped_before_the_llm(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = [
        _hit("https://example.com/login", "Login"),
        _hit("https://example.com/posts/a", "A"),
        _hit("https://facebook.com/share?u=x", "Share"),
        _hit("https://example.com/posts/b", "B"),
    ]
    monkeypatch.setattr(
        "app.orchestrator.fetch_search_response",
        lambda query, *, pageno=1, timeout_s=0.0: {"results": results},
    )
    fetch_calls: list[str] = []

    def fake_fetch(
        url: str, *, total_timeout_s: float | None = None
    ) -> FetchOutcome:
        fetch_calls.append(url)
        return _snapshot(url)

    monkeypatch.setattr("app.orchestrator.fetch_target_page", fake_fetch)
    candidates_seen: list[str] = []

    def fake_filter(
        query: str, candidates: list[object], *, timeout_s: float = 0.0
    ) -> RelevanceSelection:
        candidates_seen.extend(
            hit["url"] for hit in candidates if isinstance(hit, dict)
        )
        return RelevanceSelection(frozenset(candidates_seen))

    monkeypatch.setattr("app.orchestrator.select_relevant_hits", fake_filter)

    request_id = uuid4()
    response = Orchestrator(_config(llm_filter_enabled=True)).run(
        request_id,
        ExtractRequest(query="q", max_results=4, max_pages=1),
    )

    expected = ["https://example.com/posts/a", "https://example.com/posts/b"]
    assert candidates_seen == expected
    assert fetch_calls == expected
    assert response.counts.selected == 2
    normalized = dict(read_trace_events(trace_db, request_id))[
        "normalization.completed"
    ]
    assert normalized["low_value_count"] == 2
    assert normalized["selected_count"] == 2


def test_llm_filter_backfills_from_another_search_page(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = {
        1: [
            _hit("https://example.com/a", "A"),
            _hit("https://example.com/b", "B"),
            _hit("https://example.com/c", "C"),
            _hit("https://example.com/d", "D"),
        ],
        2: [_hit("https://example.com/e", "E")],
    }
    search_calls: list[int] = []

    def fake_search(
        query: str, *, pageno: int = 1, timeout_s: float = 0.0
    ) -> dict[str, object]:
        search_calls.append(pageno)
        return {"results": pages.get(pageno, [])}

    monkeypatch.setattr("app.orchestrator.fetch_search_response", fake_search)
    fetch_calls: list[str] = []

    def fake_fetch(
        url: str, *, total_timeout_s: float | None = None
    ) -> FetchOutcome:
        fetch_calls.append(url)
        return _snapshot(url)

    monkeypatch.setattr("app.orchestrator.fetch_target_page", fake_fetch)
    rounds: list[list[str]] = []

    def fake_filter(
        query: str, candidates: list[object], *, timeout_s: float = 0.0
    ) -> RelevanceSelection:
        urls = [hit["url"] for hit in candidates if isinstance(hit, dict)]
        rounds.append(urls)
        keep = urls[:1] if len(rounds) == 1 else urls[:2]
        return RelevanceSelection(frozenset(keep))

    monkeypatch.setattr("app.orchestrator.select_relevant_hits", fake_filter)

    request_id = uuid4()
    response = Orchestrator(_config(llm_filter_enabled=True)).run(
        request_id,
        ExtractRequest(query="q", max_results=2, max_pages=2),
    )

    assert search_calls == [1, 2]
    assert rounds == [
        [
            "https://example.com/a",
            "https://example.com/b",
        ],
        ["https://example.com/a", "https://example.com/e"],
    ]
    assert fetch_calls == ["https://example.com/a", "https://example.com/e"]
    assert response.counts.selected == 2
    events = read_trace_events(trace_db, request_id)
    assert [event for event, _ in events].count("relevance.completed") == 2
    backfill = [
        details for event, details in events if event == "relevance.backfill"
    ]
    assert backfill == [{"page": 2, "kept_count": 1, "wanted": 2}]


def test_only_max_results_pages_are_fetched(
    trace_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = [
        _hit("https://example.com/a", "A"),
        _hit("https://example.com/b", "B"),
        _hit("https://example.com/c", "C"),
        _hit("https://example.com/d", "D"),
    ]
    monkeypatch.setattr(
        "app.orchestrator.fetch_search_response",
        lambda query, *, pageno=1, timeout_s=0.0: {"results": hits},
    )
    fetch_calls: list[str] = []

    def fake_fetch(
        url: str, *, total_timeout_s: float | None = None
    ) -> FetchOutcome:
        fetch_calls.append(url)
        return _snapshot(url)

    monkeypatch.setattr("app.orchestrator.fetch_target_page", fake_fetch)
    rounds: list[list[str]] = []

    def fake_filter(
        query: str, candidates: list[object], *, timeout_s: float = 0.0
    ) -> RelevanceSelection:
        urls = [hit["url"] for hit in candidates if isinstance(hit, dict)]
        rounds.append(urls)
        return RelevanceSelection(frozenset(urls))

    monkeypatch.setattr("app.orchestrator.select_relevant_hits", fake_filter)

    response = Orchestrator(_config(llm_filter_enabled=True)).run(
        uuid4(),
        ExtractRequest(query="q", max_results=2, max_pages=1),
    )

    assert rounds == [[
        "https://example.com/a",
        "https://example.com/b",
    ]]
    assert fetch_calls == ["https://example.com/a", "https://example.com/b"]
    assert response.counts.selected == 2


def test_llm_filter_is_enabled_by_configured_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.relevance.LLM_API_KEY", "test-key")
    assert OrchestratorConfig.from_env().llm_filter_enabled is True

    monkeypatch.setattr("app.relevance.LLM_API_KEY", "")
    assert OrchestratorConfig.from_env().llm_filter_enabled is False

    monkeypatch.setattr("app.relevance.LLM_API_KEY", "test-key")
    monkeypatch.setattr("app.relevance.LLM_MODEL", "")
    assert OrchestratorConfig.from_env().llm_filter_enabled is False
