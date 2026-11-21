from pathlib import Path
from typing import Any

import httpx
import pytest

import app.stt as stt


def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "upload.mp3"
    path.write_bytes(b"audio-bytes")
    return path


def response(payload: Any, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request(
            "POST", f"{stt.STT_BASE_URL}/audio/transcriptions"
        ),
    )


def patch_post(
    monkeypatch: pytest.MonkeyPatch, result: httpx.Response | Exception
) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        captured.append({"url": url, **kwargs})
        if isinstance(result, Exception):
            raise result
        return result

    client = httpx.Client()
    monkeypatch.setattr(client, "post", fake_post)
    monkeypatch.setattr(stt, "_get_client", lambda: client)
    return captured


def configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt, "STT_BASE_URL", "https://stt.test/v1")
    monkeypatch.setattr(stt, "STT_API_KEY", "test-key")
    monkeypatch.setattr(stt, "STT_MODEL", "whisper-1")


def test_missing_api_key_skips_the_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_client() -> httpx.Client:
        pytest.fail("no request should be sent without an API key")

    monkeypatch.setattr(stt, "STT_API_KEY", "")
    monkeypatch.setattr(stt, "_get_client", fail_client)

    outcome = stt.transcribe_audio(audio_file(tmp_path))

    assert isinstance(outcome, stt.TranscriptionFailure)
    assert outcome.error_code == "stt_not_configured"


def test_is_configured_requires_all_three_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch)
    assert stt.is_configured()

    monkeypatch.setattr(stt, "STT_API_KEY", "")
    assert not stt.is_configured()

    configure(monkeypatch)
    monkeypatch.setattr(stt, "STT_MODEL", "")
    assert not stt.is_configured()


def test_normalize_base_url_collapses_duplicate_slashes() -> None:
    assert stt.normalize_base_url("https://host//v1/") == "https://host/v1"
    assert stt.normalize_base_url("https://host/v1") == "https://host/v1"
    assert stt.normalize_base_url(" https://host/v1/// ") == "https://host/v1"


def test_posts_multipart_and_reads_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch)
    captured = patch_post(monkeypatch, response({"text": "  hello world  "}))

    outcome = stt.transcribe_audio(audio_file(tmp_path))

    assert outcome == stt.TranscriptionSuccess("hello world")
    call = captured[0]
    assert call["url"] == "https://stt.test/v1/audio/transcriptions"
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["data"] == {
        "model": "whisper-1",
        "response_format": "json",
    }
    name, handle, content_type = call["files"]["file"]
    assert name == "upload.mp3"
    assert content_type == "audio/mpeg"


def test_language_hint_is_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch)
    captured = patch_post(monkeypatch, response({"text": "bonjour"}))

    stt.transcribe_audio(audio_file(tmp_path), language_hint="fr")

    assert captured[0]["data"]["language"] == "fr"


def test_http_error_returns_request_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch)
    patch_post(
        monkeypatch,
        response({"error": {"message": "bad audio"}}, status_code=400),
    )

    outcome = stt.transcribe_audio(audio_file(tmp_path))

    assert isinstance(outcome, stt.TranscriptionFailure)
    assert outcome.error_code == "stt_request_failed"
    assert "bad audio" in outcome.message
    assert "https://stt.test/v1/audio/transcriptions" in outcome.message


def test_transport_error_returns_request_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch)
    patch_post(monkeypatch, httpx.ConnectError("down"))

    outcome = stt.transcribe_audio(audio_file(tmp_path))

    assert isinstance(outcome, stt.TranscriptionFailure)
    assert outcome.error_code == "stt_request_failed"


def test_missing_text_returns_invalid_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch)
    patch_post(monkeypatch, response({"segments": []}))

    outcome = stt.transcribe_audio(audio_file(tmp_path))

    assert isinstance(outcome, stt.TranscriptionFailure)
    assert outcome.error_code == "stt_invalid_response"


def test_empty_text_returns_empty_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch)
    patch_post(monkeypatch, response({"text": "   "}))

    outcome = stt.transcribe_audio(audio_file(tmp_path))

    assert isinstance(outcome, stt.TranscriptionFailure)
    assert outcome.error_code == "stt_empty_transcript"
