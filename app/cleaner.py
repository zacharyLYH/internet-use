import logging
import re
import time
from dataclasses import dataclass
from typing import Literal

from trafilatura import extract
from trafilatura.deduplication import LRUCache

from app.linkfilter import classify_link
from app.urls import is_valid_http_url

MAX_MARKDOWN_CHARS = 20_000
MIN_MARKDOWN_CHARS = 200
MAX_CHROME_PAGE_CHARS = 1_000
MAX_CHROME_LINE_RATIO = 0.5
SHORT_LINE_CHARS = 40

# Matches bare and markdown-escaped citation markers: [1], \[1\], [\1].
CITATION_MARKER = re.compile(r"\\?\[\s*\\?\d+\\?\s*\\?\]")
# Trafilatura leaves footnote targets as bare parenthesised URLs.
CITATION_URL = re.compile(
    r"\s*\(\s*<?https?://[^()\s]*#(?:cite_note|cite_ref|endnote|_ftn)"
    r"[^()\s]*>?\s*\)"
)
SUP_TAG = re.compile(r"<sup\b[^>]*>.*?</sup>", re.IGNORECASE | re.DOTALL)
EDIT_MARKER = re.compile(r"\[edit\]", re.IGNORECASE)
BLANK_LINES = re.compile(r"\n{3,}")
MARKDOWN_LINK = re.compile(r"\[([^\[\]]*)\]\(\s*<?([^()\s<>]+)>?\s*\)")
INLINE_SPACE_RUN = re.compile(r"[ \t]{2,}")
SPACE_BEFORE_PUNCTUATION = re.compile(r"[ \t]+([.,;:!?)])")
FOOTNOTE_START = re.compile(r"^\s*\d+\.\s+(?:↑|Jump up to:)", re.MULTILINE)
MIN_CONTENT_BEFORE_REFERENCES = 200

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
    if not is_valid_http_url(target):
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
            include_tables=False,
            include_links=True,
            include_images=False,
            include_formatting=True,
            favor_precision=True,
            deduplicate=LRUCache(),
            fast=False,
        )
    except Exception as exc:
        logger.warning("clean extraction error=%s", type(exc).__name__)
        return _failure("extraction_error")
    content = _postprocess(result, target) if result is not None else ""
    if len(content) < MIN_MARKDOWN_CHARS or _is_chrome_only(content):
        return CleanSuccess("", "empty", source_bytes, 0)
    return CleanSuccess(
        content, "extracted", source_bytes, len(content.encode("utf-8"))
    )


def _is_chrome_only(text: str) -> bool:
    if len(text) >= MAX_CHROME_PAGE_CHARS:
        return False
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return True
    short = sum(1 for line in lines if len(line) < SHORT_LINE_CHARS)
    return short / len(lines) > MAX_CHROME_LINE_RATIO


def _postprocess(raw_markdown: str, page_url: str) -> str:
    text = SUP_TAG.sub("", raw_markdown)
    text = CITATION_MARKER.sub("", text)
    text = CITATION_URL.sub("", text)
    text = EDIT_MARKER.sub("", text)
    text = _strip_references(text)
    text = _filter_links(text, page_url)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = BLANK_LINES.sub("\n\n", text).strip()
    return _cap(text)


def _filter_links(text: str, page_url: str) -> str:
    """Keep useful body links, drop noise, and preserve the surrounding prose."""
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        label = match.group(1)
        verdict = classify_link(label, match.group(2), page_url, seen)
        if verdict == "keep":
            return match.group(0)
        if verdict == "drop":
            return ""
        return label

    filtered = MARKDOWN_LINK.sub(replace, text)
    filtered = INLINE_SPACE_RUN.sub(" ", filtered)
    return SPACE_BEFORE_PUNCTUATION.sub(r"\1", filtered)


def _strip_references(text: str) -> str:
    match = FOOTNOTE_START.search(text)
    if match is None or match.start() < MIN_CONTENT_BEFORE_REFERENCES:
        return text
    return text[: match.start()].rstrip()


def _cap(text: str) -> str:
    if len(text) <= MAX_MARKDOWN_CHARS:
        return text
    cut = text.rfind("\n\n", 0, MAX_MARKDOWN_CHARS)
    if cut < MAX_MARKDOWN_CHARS // 2:
        return text[:MAX_MARKDOWN_CHARS].rstrip()
    return text[:cut].rstrip()


def _failure(code: CleanErrorCode) -> CleanFailure:
    return CleanFailure(code, MESSAGES[code])
