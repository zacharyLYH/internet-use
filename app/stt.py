"""Optional OpenAI-compatible speech-to-text client.

Users bring their own endpoint, model, and key, exactly like the relevance
filter. The endpoint must accept ``POST {base_url}/audio/transcriptions`` as a
multipart upload and answer with ``{"text": "..."}``. Transcription is off
unless all three settings are present.
"""

import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

DEFAULT_TIMEOUT_S = 300.0


def normalize_base_url(value: str) -> str:
    """Collapse duplicate slashes in the path so `host//v1` also works."""
    return re.sub(r"(?<!:)/{2,}", "/", value.strip()).rstrip("/")


STT_BASE_URL = normalize_base_url(
    os.getenv("STT_BASE_URL", "https://api.openai.com/v1")
)
STT_API_KEY = os.getenv("STT_API_KEY", "")
STT_MODEL = os.getenv("STT_MODEL", "whisper-1")
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "").strip()

TranscriptionErrorCode = Literal[
    "stt_not_configured",
    "stt_request_failed",
    "stt_invalid_response",
    "stt_empty_transcript",
]

CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
}

logger = logging.getLogger(__name__)

_client: httpx.Client | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionSuccess:
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptionFailure:
    error_code: TranscriptionErrorCode
    message: str


TranscriptionOutcome = TranscriptionSuccess | TranscriptionFailure


def is_configured() -> bool:
    """True when an endpoint, a model, and an API key are all present."""
    return bool(STT_BASE_URL and STT_MODEL and STT_API_KEY)


def language() -> str | None:
    """Configured language hint, or None to let the endpoint auto-detect."""
    return STT_LANGUAGE or None


def transcribe_audio(
    path: Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    language_hint: str | None = None,
) -> TranscriptionOutcome:
    if timeout_s <= 0:
        raise ValueError("timeout_s must be > 0")
    if not is_configured():
        logger.warning("transcription requested without STT credentials")
        return TranscriptionFailure(
            "stt_not_configured", "STT endpoint is not configured."
        )

    started = time.monotonic()
    try:
        with path.open("rb") as handle:
            response = _get_client().post(
                f"{STT_BASE_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {STT_API_KEY}"},
                files={
                    "file": (
                        path.name,
                        handle,
                        CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"),
                    )
                },
                data=_form_data(language_hint),
                timeout=timeout_s,
            )
        response.raise_for_status()
        text = _transcript_text(response)
    except httpx.HTTPStatusError as exc:
        detail = _error_detail(exc.response)
        request_url = f"{STT_BASE_URL}/audio/transcriptions"
        logger.warning(
            "stt request failed url=%s status=%s detail=%s",
            request_url,
            exc.response.status_code,
            detail,
        )
        message = (
            f"STT request failed with status {exc.response.status_code}"
            f" for {request_url}."
        )
        if detail:
            message = f"{message} {detail}"
        return TranscriptionFailure("stt_request_failed", message)
    except httpx.HTTPError as exc:
        logger.warning("stt request error=%s", type(exc).__name__)
        return TranscriptionFailure("stt_request_failed", "STT request failed.")
    except ValueError as exc:
        logger.warning("stt response invalid reason=%s", exc)
        code: TranscriptionErrorCode = (
            "stt_empty_transcript"
            if str(exc) == "transcript was empty"
            else "stt_invalid_response"
        )
        return TranscriptionFailure(code, str(exc))

    logger.info(
        "stt completed chars=%d duration_ms=%d",
        len(text),
        int((time.monotonic() - started) * 1000),
    )
    return TranscriptionSuccess(text)


def _form_data(language_hint: str | None) -> dict[str, str]:
    data = {"model": STT_MODEL, "response_format": "json"}
    hint = (language_hint or "").strip()
    if hint:
        data["language"] = hint
    return data


def _transcript_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError("response was not JSON") from exc
    if not isinstance(data, Mapping):
        raise ValueError("response was not a JSON object")
    text = data.get("text")
    if not isinstance(text, str):
        raise ValueError("response had no text")
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("transcript was empty")
    return cleaned


def _error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, Mapping):
        return ""
    error = data.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str):
            return " ".join(message.split())[:300]
    if isinstance(error, str):
        return " ".join(error.split())[:300]
    return ""


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=DEFAULT_TIMEOUT_S, follow_redirects=True)
    return _client
