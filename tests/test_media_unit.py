import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import app.media as media
import app.stt as stt
from app.media import (
    MediaError,
    MediaFailure,
    MediaMetadata,
    MediaSuccess,
    _parse_json3,
    _parse_vtt,
    _subtitle_track,
    is_media_url,
    transcribe_media,
)
from app.orchestrator import Orchestrator
from app.pipeline import (
    ExtractRequest,
    FetchPage,
    Finish,
    OrchestratorConfig,
    TranscribeMedia,
    build_response,
    media_finished,
    start_pipeline,
)
from app.tracer import close_tracer, init_tracer
from tests.test_utils import read_trace_events, remove_sqlite_database

URL = "https://www.youtube.com/shorts/HSbKf8Nk-4U"


@pytest.fixture(autouse=True)
def configured_stt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt, "STT_BASE_URL", "https://stt.test/v1")
    monkeypatch.setattr(stt, "STT_API_KEY", "test-key")
    monkeypatch.setattr(stt, "STT_MODEL", "whisper-1")
    monkeypatch.setattr(stt, "STT_LANGUAGE", "")


def info(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Clip",
        "description": "A description",
        "duration": 42,
        "webpage_url": URL,
        "uploader": "Uploader",
        "extractor_key": "Youtube",
    }
    base.update(overrides)
    return base


def metadata(**overrides: Any) -> MediaMetadata:
    values: dict[str, Any] = {
        "requested_url": URL,
        "final_url": URL,
        "title": "Clip",
        "description": "A description",
        "duration_s": 42,
        "uploader": "Uploader",
        "platform": "Youtube",
    }
    values.update(overrides)
    return MediaMetadata(**values)


def config(**overrides: Any) -> OrchestratorConfig:
    values: dict[str, Any] = {
        "page_delay_min_s": 0.0,
        "page_delay_max_s": 0.0,
    }
    values.update(overrides)
    return OrchestratorConfig(**values)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc",
        "https://youtube.com/shorts/abc",
        "https://youtu.be/abc",
        "https://www.tiktok.com/@user/video/1",
        "https://www.instagram.com/reel/abc/",
        "https://x.com/user/status/1",
        "https://twitter.com/user/status/1",
        "https://vimeo.com/123",
        "https://www.twitch.tv/videos/1",
        "https://soundcloud.com/user/track",
    ],
)
def test_allowlisted_media_urls(url: str) -> None:
    assert is_media_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/video",
        "https://youtube.com.evil.example/watch",
        "https://notyoutube.com/watch",
        "https://www.youtube.com@evil.example/watch",
        "ftp://youtube.com/watch",
        "not a url",
    ],
)
def test_non_media_urls(url: str) -> None:
    assert not is_media_url(url)


def test_duration_gate_skips_before_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media, "_probe", lambda url, timeout_s: info(duration=1201)
    )

    def fail_download(*args: Any, **kwargs: Any) -> Path:
        pytest.fail("no download should happen past the duration gate")

    monkeypatch.setattr(media, "_download_audio", fail_download)

    outcome = transcribe_media(URL, max_duration_s=1200)

    assert isinstance(outcome, MediaSuccess)
    assert outcome.transcript_state == "skipped"
    assert outcome.error_code == "media_too_long"
    assert outcome.transcript == ""
    assert outcome.metadata.description == "A description"


def test_live_stream_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        media, "_probe", lambda url, timeout_s: info(is_live=True)
    )

    outcome = transcribe_media(URL)

    assert isinstance(outcome, MediaSuccess)
    assert outcome.error_code == "media_live_unsupported"


def test_stt_not_configured_returns_description_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stt, "STT_API_KEY", "")
    monkeypatch.setattr(media, "_probe", lambda url, timeout_s: info())

    outcome = transcribe_media(URL)

    assert isinstance(outcome, MediaSuccess)
    assert outcome.transcript_state == "skipped"
    assert outcome.error_code == "stt_not_configured"
    assert outcome.metadata.title == "Clip"


def test_subtitles_are_used_instead_of_stt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media, "_probe", lambda url, timeout_s: info())
    monkeypatch.setattr(
        media,
        "_fetch_subtitle_text",
        lambda info_dict, hint, timeout_s: "caption text",
    )

    def fail_stt(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("STT should not be called when subtitles exist")

    monkeypatch.setattr(stt, "transcribe_audio", fail_stt)

    outcome = transcribe_media(URL)

    assert isinstance(outcome, MediaSuccess)
    assert outcome.transcript == "caption text"
    assert outcome.transcript_source == "subtitles"
    assert outcome.transcript_state == "extracted"


def test_downloads_and_transcribes_without_subtitles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media, "_probe", lambda url, timeout_s: info())
    monkeypatch.setattr(
        media,
        "_download_audio",
        lambda url, directory, timeout_s: tmp_path / "audio.webm",
    )
    monkeypatch.setattr(
        media,
        "_normalize_audio",
        lambda source, directory, timeout_s: tmp_path / "upload.mp3",
    )
    calls: list[tuple[Path, float, str | None]] = []

    def fake_transcribe(
        path: Path, *, timeout_s: float, language_hint: str | None = None
    ) -> stt.TranscriptionOutcome:
        calls.append((path, timeout_s, language_hint))
        return stt.TranscriptionSuccess("spoken words")

    monkeypatch.setattr(stt, "transcribe_audio", fake_transcribe)

    outcome = transcribe_media(URL, timeout_s=7, language_hint="en")

    assert isinstance(outcome, MediaSuccess)
    assert outcome.transcript == "spoken words"
    assert outcome.transcript_source == "stt"
    assert outcome.transcript_state == "extracted"
    assert calls == [(tmp_path / "upload.mp3", 7, "en")]


def test_stt_failure_falls_back_to_description(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media, "_probe", lambda url, timeout_s: info())
    monkeypatch.setattr(
        media, "_fetch_subtitle_text", lambda info_dict, hint, timeout_s: ""
    )
    monkeypatch.setattr(
        media,
        "_download_audio",
        lambda url, directory, timeout_s: tmp_path / "audio.webm",
    )
    monkeypatch.setattr(
        media,
        "_normalize_audio",
        lambda source, directory, timeout_s: tmp_path / "upload.mp3",
    )
    monkeypatch.setattr(
        stt,
        "transcribe_audio",
        lambda *args, **kwargs: stt.TranscriptionFailure(
            "stt_request_failed", "boom"
        ),
    )

    outcome = transcribe_media(URL)

    assert isinstance(outcome, MediaSuccess)
    assert outcome.transcript == ""
    assert outcome.transcript_state == "failed"
    assert outcome.error_code == "stt_request_failed"
    assert outcome.metadata.description == "A description"


def test_empty_transcript_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media, "_probe", lambda url, timeout_s: info())
    monkeypatch.setattr(
        media, "_fetch_subtitle_text", lambda info_dict, hint, timeout_s: ""
    )
    monkeypatch.setattr(
        media,
        "_download_audio",
        lambda url, directory, timeout_s: tmp_path / "audio.webm",
    )
    monkeypatch.setattr(
        media,
        "_normalize_audio",
        lambda source, directory, timeout_s: tmp_path / "upload.mp3",
    )
    monkeypatch.setattr(
        stt,
        "transcribe_audio",
        lambda *args, **kwargs: stt.TranscriptionFailure(
            "stt_empty_transcript", "transcript was empty"
        ),
    )

    outcome = transcribe_media(URL)

    assert isinstance(outcome, MediaSuccess)
    assert outcome.transcript_state == "unavailable"
    assert outcome.error_code == "stt_empty_transcript"


def test_metadata_failure_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_probe(url: str, timeout_s: float) -> dict[str, Any]:
        raise MediaError("media_unavailable", "gone")

    monkeypatch.setattr(media, "_probe", fail_probe)

    outcome = transcribe_media(URL)

    assert isinstance(outcome, MediaFailure)
    assert outcome.error_code == "media_unavailable"


def test_ffmpeg_missing_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(MediaError) as excinfo:
        media._normalize_audio(tmp_path / "audio.webm", tmp_path, 10)

    assert excinfo.value.code == "ffmpeg_missing"


def test_ffmpeg_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"bad input"
        ),
    )

    with pytest.raises(MediaError) as excinfo:
        media._normalize_audio(tmp_path / "audio.webm", tmp_path, 10)

    assert excinfo.value.code == "ffmpeg_failed"
    assert excinfo.value.message == "bad input"


def test_parses_vtt_and_dedupes_repeated_lines() -> None:
    text = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:01.000\n"
        "Hello <c>world</c>\n\n"
        "00:00:01.000 --> 00:00:02.000\n"
        "Hello world\n\n"
        "00:00:02.000 --> 00:00:03.000\n"
        "Again &amp; again\n"
    )

    assert _parse_vtt(text) == "Hello world Again & again"


def test_parses_json3_and_dedupes_repeated_lines() -> None:
    payload = json.dumps(
        {
            "events": [
                {"segs": [{"utf8": "Hi "}, {"utf8": "there"}]},
                {"segs": [{"utf8": "Hi there"}]},
                {"segs": [{"utf8": " bye"}]},
            ]
        }
    )

    assert _parse_json3(payload) == "Hi there bye"


def test_subtitle_track_prefers_language_then_manual() -> None:
    info_dict = {
        "subtitles": {"de": [{"ext": "vtt", "url": "manual-de"}]},
        "automatic_captions": {"en": [{"ext": "json3", "url": "auto-en"}]},
    }

    assert _subtitle_track(info_dict, None) == ("auto-en", "json3")
    assert _subtitle_track(info_dict, "de") == ("manual-de", "vtt")


def test_media_url_routes_to_transcription_when_stt_enabled() -> None:
    transition = start_pipeline(
        uuid4(), ExtractRequest(url=URL), config(stt_enabled=True)
    )

    assert isinstance(transition.action, TranscribeMedia)
    assert transition.action.url == URL


def test_media_url_scrapes_when_stt_disabled() -> None:
    transition = start_pipeline(
        uuid4(), ExtractRequest(url=URL), config(stt_enabled=False)
    )

    assert isinstance(transition.action, FetchPage)


def test_media_finished_builds_markdown_and_counts() -> None:
    request_id = uuid4()
    transition = start_pipeline(
        request_id, ExtractRequest(url=URL), config(stt_enabled=True)
    )
    outcome = MediaSuccess(
        metadata=metadata(),
        transcript="spoken words",
        transcript_source="stt",
        transcript_state="extracted",
    )

    finished = media_finished(
        transition.state, TranscribeMedia(URL, 1), outcome
    )

    assert isinstance(finished.action, Finish)
    page = finished.state.pages[0]
    assert page.title == "Clip"
    assert page.markdown.startswith("# Clip")
    assert "Uploader · 0:42 · Youtube" in page.markdown
    assert "A description" in page.markdown
    assert "## Transcript" in page.markdown
    assert "spoken words" in page.markdown
    assert page.media is not None
    assert page.media.transcript_state == "extracted"
    assert page.media.transcript_source == "stt"

    response = build_response(finished.state)
    assert response.counts.transcribed == 1
    assert response.counts.extracted == 1


def test_media_finished_records_failure() -> None:
    transition = start_pipeline(
        uuid4(), ExtractRequest(url=URL), config(stt_enabled=True)
    )

    finished = media_finished(
        transition.state,
        TranscribeMedia(URL, 1),
        MediaFailure(URL, "media_unavailable", "gone"),
    )

    page = finished.state.pages[0]
    assert page.extraction_state == "failed"
    assert page.error is not None
    assert page.error.code == "media_unavailable"
    assert page.media is not None
    assert page.media.error_message == "gone"
    assert build_response(finished.state).outcome == "completed_with_errors"


def test_orchestrator_runs_the_media_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request_id = uuid4()
    db_path = tmp_path / "traces.sqlite3"
    close_tracer()
    init_tracer(db_path)
    try:
        monkeypatch.setattr(
            "app.orchestrator.transcribe_media",
            lambda url, **kwargs: MediaSuccess(
                metadata=metadata(),
                transcript="spoken words",
                transcript_source="stt",
                transcript_state="extracted",
            ),
        )
        response = Orchestrator(config(stt_enabled=True)).run(
            request_id, ExtractRequest(url=URL)
        )
        events = read_trace_events(db_path, request_id)
    finally:
        close_tracer()
        remove_sqlite_database(db_path)

    assert response.counts.transcribed == 1
    event_types = [event_type for event_type, _ in events]
    assert "media.started" in event_types
    assert "media.completed" in event_types



