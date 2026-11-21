import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

import app.fetcher as fetcher_module
from app.fetcher import Browser
from app.main import AppSettings, create_app
from app.orchestrator import QueueExpiredError, RequestQueue
from app.pipeline import (
    ExtractRequest,
    ExtractResponse,
    OrchestratorConfig,
)
from app.tracer import close_tracer
from tests.test_utils import (
    headless_browser,
    read_trace_events,
    remove_sqlite_database,
)

API_KEY = "test-api-key"


@pytest.fixture(scope="module")
def e2e_browser(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Browser]:
    instance = headless_browser(
        tmp_path_factory.mktemp("api-chrome-profile"),
        settle_timeout_s=1.0,
    )
    original = fetcher_module._browser
    fetcher_module._browser = instance
    try:
        yield instance
    finally:
        fetcher_module._browser = original
        instance.close()


@pytest.fixture
def client(
    tmp_path: Path, e2e_browser: Browser
) -> Iterator[TestClient]:
    keys_file = tmp_path / "api_keys.txt"
    keys_file.write_text(f"# test keys\n{API_KEY}\n", encoding="utf-8")
    keys_file.chmod(0o600)
    db_path = tmp_path / "traces.sqlite3"
    settings = AppSettings(
        trace_db_path=db_path,
        api_keys_file=keys_file,
        queue_max_size=4,
        queue_wait_s=20.0,
        max_request_bytes=2000,
    )
    config = OrchestratorConfig(
        search_timeout_s=8.0,
        fetch_timeout_s=10.0,
        search_retries=0,
        fetch_retries=0,
        retry_backoff_s=0.0,
        page_delay_min_s=0.0,
        page_delay_max_s=0.0,
    )
    try:
        with TestClient(create_app(settings, config)) as test_client:
            yield test_client
    finally:
        close_tracer()
        remove_sqlite_database(db_path)


def _headers(key: str | None = API_KEY) -> dict[str, str]:
    if key is None:
        return {}
    return {"Authorization": f"Bearer {key}"}


def _post(
    client: TestClient, payload: dict[str, object], key: str | None = API_KEY
) -> httpx.Response:
    content = payload.get("url", payload.get("query", ""))
    chat_payload: dict[str, object] = {
        "model": "test-model",
        "messages": [{"role": "user", "content": content}],
    }
    for field in ("max_results", "max_pages"):
        if field in payload:
            chat_payload[field] = payload[field]
    return cast(
        httpx.Response,
        client.post(
            "/v1/chat/completions", json=chat_payload, headers=_headers(key)
        ),
    )


def _db_path(client: TestClient) -> Path:
    settings = cast(AppSettings, cast(Any, client.app).state.settings)
    return settings.trace_db_path


@asynccontextmanager
async def _openai_client(client: TestClient) -> AsyncIterator[AsyncOpenAI]:
    transport = httpx2.ASGITransport(app=cast(Any, client.app))
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield AsyncOpenAI(
            api_key=API_KEY,
            base_url="http://testserver/v1",
            http_client=http_client,
        )


def test_health_is_unauthenticated_and_traced(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    request_id = response.headers["x-request-id"]
    events = read_trace_events(_db_path(client), request_id)
    assert [event for event, _ in events] == ["api.ingress"]
    assert events[0][1]["path"] == "/health"


def test_missing_api_key_is_rejected_and_traced(client: TestClient) -> None:
    response = _post(client, {"url": "https://example.com/a"}, key=None)

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert body["request_id"] == response.headers["x-request-id"]
    events = read_trace_events(_db_path(client), body["request_id"])
    assert [event for event, _ in events] == ["api.ingress", "api.auth_rejected"]


def test_invalid_api_key_is_rejected(client: TestClient) -> None:
    response = _post(client, {"url": "https://example.com/a"}, key="wrong")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"model": "m", "messages": []},
        {"model": "m", "messages": [{"role": "user", "content": ""}]},
        {
            "model": "m",
            "messages": [
                {"role": "user", "content": [{"type": "image_url"}]}
            ],
        },
        {
            "model": "m",
            "messages": [{"role": "user", "content": "q"}],
            "max_results": 0,
        },
        {
            "model": "m",
            "messages": [{"role": "user", "content": "q"}],
            "max_pages": 11,
        },
    ],
)
def test_invalid_request_is_rejected(
    client: TestClient, payload: dict[str, object]
) -> None:
    response = client.post(
        "/v1/chat/completions", json=payload, headers=_headers()
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_validation_failure_is_traced(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions", json={}, headers=_headers()
    )

    request_id = response.json()["request_id"]
    events = read_trace_events(_db_path(client), request_id)
    assert [event for event, _ in events] == [
        "api.ingress",
        "api.auth_succeeded",
        "request.validation_failed",
    ]


def test_body_too_large_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        content=json.dumps(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "x" * 3000}],
            }
        ),
        headers={**_headers(), "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "request_too_large"
    events = read_trace_events(_db_path(client), body["request_id"])
    assert [event for event, _ in events] == ["api.ingress", "request.rejected"]


def test_direct_article_end_to_end(
    client: TestClient, site: str
) -> None:
    response = _post(client, {"url": f"{site}/article"})

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "completed"
    assert body["counts"] == {
        "selected": 1,
        "fetched": 1,
        "extracted": 1,
        "empty": 0,
        "failed": 0,
        "not_attempted": 0,
        "transcribed": 0,
    }
    page = body["pages"][0]
    assert page["status_code"] == 200
    assert page["content_type"] == "text/html"
    assert page["extraction_state"] == "extracted"
    assert "Article title" in page["markdown"]

    events = read_trace_events(_db_path(client), body["request_id"])
    event_types = [event for event, _ in events]
    assert event_types[:2] == ["api.ingress", "api.auth_succeeded"]
    assert event_types[2] == "request.started"
    assert event_types[-1] == "request.completed"
    assert "search." not in " ".join(event_types)
    assert "fetch.completed" in event_types
    assert "extraction.completed" in event_types
    completed = dict(events)["request.completed"]
    assert completed["outcome"] == "completed"
    assert completed["counts"]["extracted"] == 1


def test_openai_sdk_chat_completions_reads_url(
    client: TestClient, site: str
) -> None:
    async def create() -> Any:
        async with _openai_client(client) as sdk:
            return await sdk.chat.completions.with_raw_response.create(
                model="openai/gpt-5.2:online",
                messages=[{"role": "user", "content": f"{site}/article"}],
            )

    raw = asyncio.run(create())

    assert raw.http_response.status_code == 200
    body = raw.parse(to=dict)
    assert body["outcome"] == "completed"
    assert "Article title" in body["pages"][0]["markdown"]


def test_openai_sdk_chat_completions_reads_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_queue = cast(RequestQueue, cast(Any, client.app).state.request_queue)
    captured: dict[str, ExtractRequest] = {}

    def capture(request_id: object, payload: ExtractRequest) -> ExtractResponse:
        captured["payload"] = payload
        return ExtractResponse(request_id=str(request_id), outcome="completed")

    monkeypatch.setattr(request_queue, "submit", capture)

    async def create() -> Any:
        async with _openai_client(client) as sdk:
            return await sdk.chat.completions.with_raw_response.create(
                model="openai/gpt-5.2:online",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "What are today's top tech news stories?"
                                ),
                            }
                        ],
                    }
                ],
            )

    raw = asyncio.run(create())

    assert raw.http_response.status_code == 200
    assert raw.parse(to=dict)["outcome"] == "completed"
    assert captured["payload"].query == (
        "What are today's top tech news stories?"
    )
    assert captured["payload"].url is None


def test_redirect_reports_final_url(client: TestClient, site: str) -> None:
    response = _post(client, {"url": f"{site}/redirect"})

    assert response.status_code == 200
    page = response.json()["pages"][0]
    assert page["requested_url"] == f"{site}/redirect"
    assert page["final_url"] == f"{site}/article"
    assert page["extraction_state"] == "extracted"


def test_empty_page_reports_empty(client: TestClient, site: str) -> None:
    response = _post(client, {"url": f"{site}/empty"})

    assert response.status_code == 200
    body = response.json()
    page = body["pages"][0]
    assert body["outcome"] == "completed"
    assert page["extraction_state"] == "empty"
    assert page["markdown"] == ""
    assert body["counts"]["empty"] == 1
    completed = dict(read_trace_events(_db_path(client), body["request_id"]))[
        "extraction.completed"
    ]
    assert completed["extraction_state"] == "empty"


def test_http_error_page_is_still_extracted(
    client: TestClient, site: str
) -> None:
    response = _post(client, {"url": f"{site}/missing"})

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "completed"
    assert body["pages"][0]["status_code"] == 404
    assert body["counts"]["failed"] == 0


def test_non_html_fails_the_page(client: TestClient, site: str) -> None:
    response = _post(client, {"url": f"{site}/pdf"})

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "completed_with_errors"
    assert body["counts"]["failed"] == 1
    assert body["pages"][0]["extraction_state"] == "failed"
    assert body["pages"][0]["error"]["code"] == "unsupported_content_type"
    events = read_trace_events(_db_path(client), body["request_id"])
    fetch_failures = [
        details for event, details in events if event == "fetch.failed"
    ]
    assert fetch_failures[0]["error_code"] == "unsupported_content_type"


def test_blocked_destination_fails_the_page(client: TestClient) -> None:
    response = _post(client, {"url": "http://169.254.169.254/"})

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "completed_with_errors"
    assert body["pages"][0]["error"]["code"] == "blocked_destination"


def test_network_error_fails_the_page(client: TestClient) -> None:
    response = _post(client, {"url": "http://127.0.0.1:1/"})

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "completed_with_errors"
    assert body["pages"][0]["error"]["code"] == "network_error"


def test_redirect_loop_fails_the_page(client: TestClient, site: str) -> None:
    response = _post(client, {"url": f"{site}/redirect-loop"})

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "completed_with_errors"
    assert body["pages"][0]["error"]["code"] == "redirect_error"


def test_challenge_warning_is_preserved(client: TestClient, site: str) -> None:
    response = _post(client, {"url": f"{site}/challenge"})

    assert response.status_code == 200
    page = response.json()["pages"][0]
    assert "possible_challenge" in page["warnings"]


def test_oversized_page_fails_the_page(client: TestClient, site: str) -> None:
    response = _post(client, {"url": f"{site}/large"})

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "completed_with_errors"
    assert body["pages"][0]["error"]["code"] == "response_too_large"


def test_search_mode_end_to_end(client: TestClient) -> None:
    response = _post(
        client,
        {"query": "python", "max_results": 1, "max_pages": 1},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] in ("completed", "completed_with_errors")
    assert body["counts"]["selected"] == 1
    assert len(body["pages"]) == 1
    assert body["pages"][0]["requested_url"].startswith(("http://", "https://"))
    events = read_trace_events(_db_path(client), body["request_id"])
    event_types = [event for event, _ in events]
    assert "search.started" in event_types
    assert "search.completed" in event_types
    assert "normalization.completed" in event_types
    started = dict(events)["request.started"]
    assert started["mode"] == "query"
    assert started["max_results"] == 1


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    response = client.get("/nope", headers=_headers())

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["request_id"] == response.headers["x-request-id"]


def test_trace_outage_returns_503(client: TestClient) -> None:
    close_tracer()

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "trace_unavailable"


def test_queue_timeout_returns_504(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_queue = cast(RequestQueue, cast(Any, client.app).state.request_queue)

    def expire(*args: object, **kwargs: object) -> None:
        raise QueueExpiredError("simulated queue timeout")

    monkeypatch.setattr(request_queue, "submit", expire)

    response = _post(client, {"url": "https://example.com/a"})

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "queue_timeout"
