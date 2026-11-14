import logging
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from trafilatura import extract

ExtractionState = Literal["extracted", "empty"]
CleanErrorCode = Literal["invalid_input", "extraction_error"]

MESSAGES: dict[CleanErrorCode, str] = {
    "invalid_input": "Input must be HTML text and an absolute http(s) URL.",
    "extraction_error": "Content extraction failed.",
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CleanSuccess:
    markdown_content: str
    extraction_state: ExtractionState
    source_html_bytes: int
    markdown_bytes: int


@dataclass(frozen=True, slots=True)
class CleanFailure:
    error_code: CleanErrorCode
    message: str


CleanOutcome = CleanSuccess | CleanFailure


def clean_to_markdown(raw_page_html: str, url: str) -> CleanOutcome:
    started = time.monotonic()
    outcome = _clean(raw_page_html, url)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if isinstance(outcome, CleanSuccess):
        logger.info(
            "clean state=%s source_bytes=%d markdown_bytes=%d duration_ms=%d",
            outcome.extraction_state,
            outcome.source_html_bytes,
            outcome.markdown_bytes,
            elapsed_ms,
        )
    else:
        logger.info(
            "clean state=failed error=%s duration_ms=%d",
            outcome.error_code,
            elapsed_ms,
        )
    return outcome


def _clean(raw_page_html: object, url: object) -> CleanOutcome:
    if not isinstance(raw_page_html, str) or not isinstance(url, str):
        return _failure("invalid_input")
    target = url.strip()
    if not _valid_url(target):
        return _failure("invalid_input")
    try:
        source_bytes = len(raw_page_html.encode("utf-8"))
    except UnicodeEncodeError:
        return _failure("invalid_input")
    if not raw_page_html.strip():
        return CleanSuccess("", "empty", source_bytes, 0)
    try:
        result = extract(
            raw_page_html,
            url=target,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_links=True,
            include_images=False,
            include_formatting=True,
            fast=False,
        )
    except Exception as exc:
        logger.warning("clean extraction error=%s", type(exc).__name__)
        return _failure("extraction_error")
    content = result.strip() if result is not None else ""
    if not content:
        return CleanSuccess("", "empty", source_bytes, 0)
    return CleanSuccess(
        content, "extracted", source_bytes, len(content.encode("utf-8"))
    )


def _valid_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
    )


def _failure(code: CleanErrorCode) -> CleanFailure:
    return CleanFailure(code, MESSAGES[code])
