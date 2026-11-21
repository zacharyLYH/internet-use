from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.stt as stt
from app.fetcher import PageSnapshot
from app.main import AppSettings, create_app
from app.pipeline import OrchestratorConfig
from tests.test_utils import ARTICLE_HTML

API_KEY = "test-api-key"


def settings(tmp_path: Path, **overrides: Any) -> AppSettings:
    keys_file = tmp_path / "api_keys.txt"
    keys_file.write_text(f"{API_KEY}\n", encoding="utf-8")
    keys_file.chmod(0o600)
    values: dict[str, Any] = {
        "trace_db_path": tmp_path / "traces.sqlite3",
        "api_keys_file": keys_file,
        "queue_max_size": 4,
        "queue_wait_s": 10.0,
    }
    values.update(overrides)
    return AppSettings(**values)


def config() -> OrchestratorConfig:
    return OrchestratorConfig(
        page_delay_min_s=0.0,
        page_delay_max_s=0.0,
        stt_enabled=False,
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(settings(tmp_path), config())) as test_client:
        yield test_client


def headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


def snapshot(url: str) -> PageSnapshot:
    return PageSnapshot(
        requested_url=url,
        final_url=url,
        title="Article",
        status_code=200,
        content_type="text/html",
        raw_page_html=ARTICLE_HTML,
        render_state="complete",
    )


def configure_stt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt, "STT_BASE_URL", "https://stt.test/v1")
    monkeypatch.setattr(stt, "STT_API_KEY", "test-key")
    monkeypatch.setattr(stt, "STT_MODEL", "whisper-1")


def test_extract_requires_auth(client: TestClient) -> None:
    response = client.post("/v1/extract", json={"query": "test"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_extract_rejects_query_and_url_together(client: TestClient) -> None:
    response = client.post(
        "/v1/extract",
        headers=headers(),
        json={"query": "test", "url": "https://example.com/a"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_extract_scrapes_a_direct_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_fetch(
        url: str, *, total_timeout_s: float | None = None
    ) -> PageSnapshot:
        calls.append(url)
        return snapshot(url)

    monkeypatch.setattr("app.orchestrator.fetch_target_page", fake_fetch)

    response = client.post(
        "/v1/extract",
        headers=headers(),
        json={"url": "https://example.com/article"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "completed"
    assert body["counts"]["selected"] == 1
    assert "Article title" in body["pages"][0]["markdown"]
    assert calls == ["https://example.com/article"]


def test_transcriptions_require_auth(client: TestClient) -> None:
    response = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.mp3", b"audio", "audio/mpeg")},
    )

    assert response.status_code == 401


def test_transcriptions_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stt, "STT_API_KEY", "")

    response = client.post(
        "/v1/audio/transcriptions",
        headers=headers(),
        files={"file": ("clip.mp3", b"audio", "audio/mpeg")},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "stt_not_configured"


def test_transcriptions_forward_the_upload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_stt(monkeypatch)
    calls: list[tuple[str, bytes, float, str | None]] = []

    def fake_transcribe(
        path: Path, *, timeout_s: float, language_hint: str | None = None
    ) -> stt.TranscriptionOutcome:
        calls.append((path.name, path.read_bytes(), timeout_s, language_hint))
        return stt.TranscriptionSuccess("hello")

    monkeypatch.setattr(stt, "transcribe_audio", fake_transcribe)

    response = client.post(
        "/v1/audio/transcriptions",
        headers=headers(),
        files={"file": ("clip.mp3", b"audio-bytes", "audio/mpeg")},
        data={"model": "ignored", "language": "fr"},
    )

    assert response.status_code == 200
    assert response.json() == {"text": "hello"}
    assert calls == [("upload.mp3", b"audio-bytes", 300.0, "fr")]


def test_transcriptions_reject_unsupported_response_format(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_stt(monkeypatch)

    response = client.post(
        "/v1/audio/transcriptions",
        headers=headers(),
        files={"file": ("clip.mp3", b"audio", "audio/mpeg")},
        data={"response_format": "text"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_transcriptions_map_upstream_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_stt(monkeypatch)
    monkeypatch.setattr(
        stt,
        "transcribe_audio",
        lambda *args, **kwargs: stt.TranscriptionFailure(
            "stt_request_failed", "boom"
        ),
    )

    response = client.post(
        "/v1/audio/transcriptions",
        headers=headers(),
        files={"file": ("clip.mp3", b"audio", "audio/mpeg")},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_stt_failed"


def test_transcriptions_return_empty_text_for_silence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_stt(monkeypatch)
    monkeypatch.setattr(
        stt,
        "transcribe_audio",
        lambda *args, **kwargs: stt.TranscriptionFailure(
            "stt_empty_transcript", "transcript was empty"
        ),
    )

    response = client.post(
        "/v1/audio/transcriptions",
        headers=headers(),
        files={"file": ("clip.mp3", b"audio", "audio/mpeg")},
    )

    assert response.status_code == 200
    assert response.json() == {"text": ""}


def test_transcriptions_reject_empty_upload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_stt(monkeypatch)

    response = client.post(
        "/v1/audio/transcriptions",
        headers=headers(),
        files={"file": ("clip.mp3", b"", "audio/mpeg")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_transcriptions_reject_oversized_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_stt(monkeypatch)
    app = create_app(settings(tmp_path, max_audio_bytes=4), config())

    with TestClient(app) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            headers=headers(),
            files={"file": ("clip.mp3", b"audio-bytes", "audio/mpeg")},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"

