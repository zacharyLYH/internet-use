"""Download and transcribe audio from allowlisted media hosts.

The stage is deliberately narrow: only mainstream video/audio platforms are
eligible, so yt-dlp cannot be used as a generic downloader or SSRF tool. The
order of preference is existing subtitles, then the user's STT endpoint, then
the description alone. Failures after metadata never fail the request.
"""

import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from app import stt
from app.urls import host_matches, parse_http_url

MEDIA_DOMAINS = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "tiktok.com",
        "instagram.com",
        "facebook.com",
        "fb.watch",
        "x.com",
        "twitter.com",
        "vimeo.com",
        "dailymotion.com",
        "dai.ly",
        "twitch.tv",
        "soundcloud.com",
        "reddit.com",
        "redd.it",
        "streamable.com",
    }
)

PREFERRED_SUBTITLE_LANGUAGES = ("en", "en-US", "en-GB")
SUBTITLE_FORMATS = ("json3", "vtt", "srt", "srv3", "ttml")
MAX_TRANSCRIPT_CHARS = int(os.getenv("MAX_TRANSCRIPT_CHARS", "40000"))
DEFAULT_MAX_DURATION_S = 1200
DEFAULT_TIMEOUT_S = 120.0

TranscriptSource = Literal["subtitles", "stt", "none"]
TranscriptState = Literal["extracted", "unavailable", "skipped", "failed"]

VTT_TAG = re.compile(r"<[^>]+>")
VTT_TIMESTAMP = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->")
VTT_CUE_NUMBER = re.compile(r"^\d+$")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

logger = logging.getLogger(__name__)


class MediaError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    requested_url: str
    final_url: str
    title: str
    description: str
    duration_s: int | None
    uploader: str | None
    platform: str | None


@dataclass(frozen=True, slots=True)
class MediaSuccess:
    metadata: MediaMetadata
    transcript: str
    transcript_source: TranscriptSource
    transcript_state: TranscriptState
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class MediaFailure:
    requested_url: str
    error_code: str
    error_message: str


MediaOutcome = MediaSuccess | MediaFailure


def is_media_url(url: str) -> bool:
    """True when the URL belongs to an allowlisted media platform."""
    parsed = parse_http_url(url)
    if parsed is None or parsed.hostname is None:
        return False
    return host_matches(parsed.hostname, MEDIA_DOMAINS)


def transcribe_media(
    url: str,
    *,
    max_duration_s: int = DEFAULT_MAX_DURATION_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    language_hint: str | None = None,
) -> MediaOutcome:
    if max_duration_s < 1:
        raise ValueError("max_duration_s must be >= 1")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be > 0")

    started = time.monotonic()
    try:
        info = _probe(url, timeout_s)
    except MediaError as exc:
        logger.info("media metadata failed url=%s code=%s", url, exc.code)
        return MediaFailure(url, exc.code, exc.message)

    metadata = _metadata(url, info)
    if bool(info.get("is_live")):
        return _metadata_only(
            metadata,
            "skipped",
            "media_live_unsupported",
            "Live streams are not transcribed.",
        )
    if metadata.duration_s is not None and metadata.duration_s > max_duration_s:
        return _metadata_only(
            metadata,
            "skipped",
            "media_too_long",
            f"Media is longer than {max_duration_s} seconds.",
        )
    if not stt.is_configured():
        return _metadata_only(
            metadata,
            "skipped",
            "stt_not_configured",
            "STT endpoint is not configured.",
        )

    with tempfile.TemporaryDirectory(prefix="internet-use-media-") as tmp:
        workdir = Path(tmp)
        text = _fetch_subtitle_text(info, language_hint, timeout_s)
        source: TranscriptSource = "subtitles"
        if not text:
            source = "stt"
            try:
                audio = _download_audio(url, workdir, timeout_s)
                upload = _normalize_audio(audio, workdir, timeout_s)
            except MediaError as exc:
                return _metadata_only(
                    metadata, "failed", exc.code, exc.message
                )
            outcome = stt.transcribe_audio(
                upload, timeout_s=timeout_s, language_hint=language_hint
            )
            if isinstance(outcome, stt.TranscriptionFailure):
                state: TranscriptState = (
                    "unavailable"
                    if outcome.error_code == "stt_empty_transcript"
                    else "failed"
                )
                return _metadata_only(
                    metadata, state, outcome.error_code, outcome.message
                )
            text = outcome.text

    transcript = _clip(text)
    logger.info(
        "media transcribed url=%s source=%s chars=%d duration_ms=%d",
        url,
        source,
        len(transcript),
        int((time.monotonic() - started) * 1000),
    )
    return MediaSuccess(
        metadata=metadata,
        transcript=transcript,
        transcript_source=source,
        transcript_state="extracted",
    )


def _metadata_only(
    metadata: MediaMetadata,
    state: TranscriptState,
    code: str,
    message: str,
) -> MediaSuccess:
    return MediaSuccess(
        metadata=metadata,
        transcript="",
        transcript_source="none",
        transcript_state=state,
        error_code=code,
        error_message=message,
    )


def _metadata(url: str, info: dict[str, Any]) -> MediaMetadata:
    duration = info.get("duration")
    duration_s = int(duration) if isinstance(duration, (int, float)) else None
    return MediaMetadata(
        requested_url=url,
        final_url=_text(info.get("webpage_url")) or url,
        title=_one_line(info.get("title")),
        description=_text(info.get("description")),
        duration_s=duration_s,
        uploader=_one_line(info.get("uploader"))
        or _one_line(info.get("channel")),
        platform=_one_line(info.get("extractor_key")) or _host(url),
    )


def _probe(url: str, timeout_s: float) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": timeout_s,
        "retries": 1,
        "cachedir": False,
        "logger": _QuietLogger(),
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise MediaError("media_unavailable", _clean_error(exc)) from exc
    except Exception as exc:
        raise MediaError(
            "media_unavailable", f"Media lookup failed ({type(exc).__name__})."
        ) from exc
    if not isinstance(info, dict):
        raise MediaError("media_unavailable", "Media metadata was not available.")
    return info


def _download_audio(url: str, directory: Path, timeout_s: float) -> Path:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(directory / "audio.%(ext)s"),
        "socket_timeout": timeout_s,
        "retries": 1,
        "cachedir": False,
        "logger": _QuietLogger(),
    }
    try:
        with YoutubeDL(options) as ydl:
            ydl.download([url])
    except DownloadError as exc:
        raise MediaError("media_download_failed", _clean_error(exc)) from exc
    except Exception as exc:
        raise MediaError(
            "media_download_failed",
            f"Audio download failed ({type(exc).__name__}).",
        ) from exc
    for candidate in sorted(directory.glob("audio.*")):
        if candidate.suffix == ".part" or not candidate.is_file():
            continue
        if candidate.stat().st_size > 0:
            return candidate
    raise MediaError("media_download_failed", "Audio download produced no file.")


def _normalize_audio(source: Path, directory: Path, timeout_s: float) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MediaError(
            "ffmpeg_missing", "ffmpeg is required for media transcription."
        )
    target = directory / "upload.mp3"
    command = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(target),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=timeout_s, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(
            "ffmpeg_failed", f"Audio conversion failed ({type(exc).__name__})."
        ) from exc
    if completed.returncode != 0 or not target.exists():
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        message = detail[-1][:300] if detail else "Audio conversion failed."
        raise MediaError("ffmpeg_failed", message)
    return target


def _fetch_subtitle_text(
    info: dict[str, Any], language_hint: str | None, timeout_s: float
) -> str:
    track = _subtitle_track(info, language_hint)
    if track is None:
        return ""
    url, ext = track
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": timeout_s,
        "logger": _QuietLogger(),
    }
    try:
        with YoutubeDL(options) as ydl:
            raw = ydl.urlopen(url).read()
    except Exception as exc:
        logger.warning("subtitle download failed error=%s", type(exc).__name__)
        return ""
    if not isinstance(raw, bytes):
        return ""
    text = raw.decode("utf-8", "replace")
    if ext == "json3":
        return _parse_json3(text)
    return _parse_vtt(text)


def _subtitle_track(
    info: dict[str, Any], language_hint: str | None
) -> tuple[str, str] | None:
    buckets = [
        bucket
        for bucket in (info.get("subtitles"), info.get("automatic_captions"))
        if isinstance(bucket, dict)
    ]
    if not buckets:
        return None
    languages: set[str] = set()
    for bucket in buckets:
        languages.update(key for key in bucket if isinstance(key, str))
    for language in _language_order(languages, language_hint):
        for bucket in buckets:
            track = _track_for(bucket.get(language))
            if track is not None:
                return track
    return None


def _track_for(formats: object) -> tuple[str, str] | None:
    if not isinstance(formats, list):
        return None
    by_ext: dict[str, str] = {}
    for entry in formats:
        if not isinstance(entry, dict):
            continue
        ext = entry.get("ext")
        url = entry.get("url")
        if isinstance(ext, str) and isinstance(url, str):
            by_ext.setdefault(ext, url)
    for ext in SUBTITLE_FORMATS:
        url = by_ext.get(ext)
        if url:
            return url, ext
    return None


def _language_order(
    languages: set[str], language_hint: str | None
) -> list[str]:
    preferred: list[str] = []
    if language_hint:
        preferred.append(language_hint)
    preferred.extend(PREFERRED_SUBTITLE_LANGUAGES)
    ordered = [lang for lang in preferred if lang in languages]
    ordered.extend(sorted(lang for lang in languages if lang not in ordered))
    return ordered


def _parse_vtt(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        if VTT_TIMESTAMP.match(line) or VTT_CUE_NUMBER.match(line):
            continue
        cleaned = html.unescape(VTT_TAG.sub("", line)).strip()
        if cleaned:
            lines.append(cleaned)
    return _join_caption_lines(lines)


def _parse_json3(text: str) -> str:
    try:
        payload = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    events = payload.get("events")
    if not isinstance(events, list):
        return ""
    lines: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        segs = event.get("segs")
        if not isinstance(segs, list):
            continue
        piece = "".join(
            seg.get("utf8", "")
            for seg in segs
            if isinstance(seg, dict) and isinstance(seg.get("utf8"), str)
        )
        piece = " ".join(piece.split())
        if piece:
            lines.append(piece)
    return _join_caption_lines(lines)


def _join_caption_lines(lines: list[str]) -> str:
    kept: list[str] = []
    for line in lines:
        if kept and kept[-1] == line:
            continue
        kept.append(line)
    return " ".join(kept).strip()


def _clip(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= MAX_TRANSCRIPT_CHARS:
        return cleaned
    clipped = cleaned[:MAX_TRANSCRIPT_CHARS]
    cut = clipped.rfind(" ")
    if cut > 0:
        clipped = clipped[:cut]
    return clipped.rstrip() + "..."


def _clean_error(exc: BaseException) -> str:
    text = ANSI_ESCAPE.sub("", str(exc)).strip()
    text = text.removeprefix("ERROR: ").strip()
    return text[:300] or "Media extraction failed."


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _one_line(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _host(url: str) -> str | None:
    parsed = parse_http_url(url)
    return parsed.hostname if parsed is not None else None


class _QuietLogger:
    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        logger.debug("yt-dlp: %s", message)

