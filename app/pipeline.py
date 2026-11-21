import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.cleaner import CleanFailure, CleanOutcome
from app.fetcher import FetchOutcome, PageSnapshot, RenderState, WarningCode
from app.linkfilter import is_low_value_url
from app.media import (
    MediaFailure,
    MediaOutcome,
    MediaSuccess,
    TranscriptSource,
    TranscriptState,
    is_media_url,
)
from app.parser import SearchHit, parse_search_response
from app.relevance import (
    RelevanceFailure,
    RelevanceOutcome,
    is_configured,
)
from app.search import SearchError
from app.stt import is_configured as stt_is_configured
from app.tracer import TraceEvent, request_event, stage_event, trace_event
from app.urls import is_valid_http_url

ExtractionState = Literal["extracted", "empty", "failed", "not_attempted"]
RequestOutcome = Literal["completed", "completed_with_errors"]
Mode = Literal["query", "url"]
PauseKind = Literal["page", "retry"]

RETRYABLE_FETCH = frozenset({"network_error", "navigation_timeout"})


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    search_timeout_s: float = 10.0
    fetch_timeout_s: float = 30.0
    search_retries: int = 2
    fetch_retries: int = 1
    retry_backoff_s: float = 0.25
    page_delay_min_s: float = 0.5
    page_delay_max_s: float = 1.0
    llm_filter_enabled: bool = False
    llm_filter_timeout_s: float = 10.0
    stt_enabled: bool = False
    stt_timeout_s: float = 300.0
    stt_language: str = ""
    media_timeout_s: float = 120.0
    media_duration_max_s: int = 1200

    def __post_init__(self) -> None:
        if self.search_timeout_s <= 0 or self.fetch_timeout_s <= 0:
            raise ValueError("stage timeouts must be > 0")
        if self.search_retries < 0 or self.fetch_retries < 0:
            raise ValueError("retry counts must be >= 0")
        if self.retry_backoff_s < 0:
            raise ValueError("retry_backoff_s must be >= 0")
        if self.page_delay_min_s < 0 or self.page_delay_max_s < 0:
            raise ValueError("page delays must be >= 0")
        if self.page_delay_min_s > self.page_delay_max_s:
            raise ValueError("page_delay_min_s must be <= page_delay_max_s")
        if self.llm_filter_timeout_s <= 0:
            raise ValueError("llm_filter_timeout_s must be > 0")
        if self.stt_timeout_s <= 0 or self.media_timeout_s <= 0:
            raise ValueError("stt_timeout_s and media_timeout_s must be > 0")
        if self.media_duration_max_s < 1:
            raise ValueError("media_duration_max_s must be >= 1")

    @classmethod
    def from_env(cls) -> Self:
        return cls(
            search_timeout_s=float(os.getenv("SEARCH_TIMEOUT_S", "10")),
            fetch_timeout_s=float(os.getenv("FETCH_TIMEOUT_S", "30")),
            search_retries=int(os.getenv("SEARCH_RETRIES", "2")),
            fetch_retries=int(os.getenv("FETCH_RETRIES", "1")),
            retry_backoff_s=float(os.getenv("RETRY_BACKOFF_S", "0.25")),
            page_delay_min_s=float(os.getenv("PAGE_DELAY_MIN_S", "0.5")),
            page_delay_max_s=float(os.getenv("PAGE_DELAY_MAX_S", "1.0")),
            llm_filter_enabled=is_configured(),
            llm_filter_timeout_s=float(os.getenv("LLM_FILTER_TIMEOUT_S", "10")),
            stt_enabled=stt_is_configured(),
            stt_timeout_s=float(os.getenv("STT_TIMEOUT_S", "300")),
            stt_language=os.getenv("STT_LANGUAGE", "").strip(),
            media_timeout_s=float(os.getenv("MEDIA_TIMEOUT_S", "120")),
            media_duration_max_s=int(os.getenv("MAX_MEDIA_DURATION_S", "1200")),
        )


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    url: str | None = None
    max_results: int = Field(default=5, ge=1, le=20)
    max_pages: int = Field(default=3, ge=1, le=10)


class PageError(BaseModel):
    code: str
    message: str


class MediaDetails(BaseModel):
    platform: str | None = None
    duration_s: int | None = None
    transcript_source: TranscriptSource = "none"
    transcript_state: TranscriptState = "unavailable"
    transcript_chars: int = 0
    error_code: str | None = None


class ExtractedPage(BaseModel):
    requested_url: str
    final_url: str | None = None
    title: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    render_state: RenderState | None = None
    warnings: list[WarningCode] = Field(default_factory=list)
    extraction_state: ExtractionState = "not_attempted"
    markdown: str = ""
    media: MediaDetails | None = None
    error: PageError | None = None


class Counts(BaseModel):
    selected: int = 0
    fetched: int = 0
    extracted: int = 0
    empty: int = 0
    failed: int = 0
    not_attempted: int = 0
    transcribed: int = 0


class ExtractResponse(BaseModel):
    request_id: str
    outcome: RequestOutcome
    pages: list[ExtractedPage] = Field(default_factory=list)
    counts: Counts = Field(default_factory=Counts)


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class PipelineState:
    request_id: UUID
    request: ExtractRequest
    mode: Mode
    hits: tuple[SearchHit, ...] = ()
    seen_urls: frozenset[str] = frozenset()
    pages: tuple[ExtractedPage, ...] = ()
    next_hit: int = 0
    searched_pages: int = 0
    media_enabled: bool = False

    @property
    def query_text(self) -> str:
        return (self.request.query or "").strip()


@dataclass(frozen=True, slots=True)
class SearchPage:
    page: int
    attempt: int


@dataclass(frozen=True, slots=True)
class FetchPage:
    url: str
    ordinal: int
    attempt: int


@dataclass(frozen=True, slots=True)
class CleanPage:
    snapshot: PageSnapshot
    ordinal: int


@dataclass(frozen=True, slots=True)
class TranscribeMedia:
    url: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class FilterHits:
    pass


@dataclass(frozen=True, slots=True)
class Pause:
    next_action: SearchPage | FetchPage
    kind: PauseKind
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class Finish:
    pass


@dataclass(frozen=True, slots=True)
class Fail:
    error: PipelineError


ExecutableAction = SearchPage | FetchPage | CleanPage | FilterHits | TranscribeMedia
Action = ExecutableAction | Pause | Finish | Fail


@dataclass(frozen=True, slots=True)
class Transition:
    state: PipelineState
    action: Action
    events: tuple[TraceEvent, ...] = ()


def start_pipeline(
    request_id: UUID,
    request: ExtractRequest,
    config: OrchestratorConfig | None = None,
) -> Transition:
    query = (request.query or "").strip()
    url = (request.url or "").strip()
    if bool(query) == bool(url):
        raise PipelineError(
            "invalid_request",
            "Exactly one of query or url is required.",
            400,
        )

    mode: Mode = "query" if query else "url"
    details: dict[str, object] = {"mode": mode}
    if mode == "query":
        details["max_results"] = request.max_results
        details["max_pages"] = request.max_pages
    events = (request_event("started", **details),)
    media_enabled = bool(config and config.stt_enabled)
    state = PipelineState(
        request_id=request_id,
        request=request,
        mode=mode,
        media_enabled=media_enabled,
    )

    if mode == "query":
        return Transition(state, SearchPage(page=1, attempt=1), events)
    if not is_valid_http_url(url):
        error = PipelineError(
            "invalid_request",
            "url must be an absolute http(s) URL without credentials.",
            400,
        )
        return Transition(state, Fail(error), events)
    return Transition(state, _next_action(state, url, 1), events)


def search_succeeded(
    state: PipelineState,
    action: SearchPage,
    response: Mapping[str, Any],
    config: OrchestratorConfig,
) -> Transition:
    results = response.get("results")
    result_count = len(results) if isinstance(results, list) else 0
    events = [
        stage_event(
            "search",
            "completed",
            page=action.page,
            attempt=action.attempt,
            result_count=result_count,
        ),
        stage_event(
            "normalization",
            "started",
            page=action.page,
            result_count=result_count,
        ),
    ]

    try:
        parsed_hits = parse_search_response(response)
    except ValueError:
        failed = stage_event(
            "normalization",
            "failed",
            page=action.page,
            error_code="normalization_failed",
        )
        events.append(failed)
        error = PipelineError(
            "normalization_failed", "Search response could not be normalized."
        )
        return Transition(state, Fail(error), tuple(events))

    page_hits = [
        hit for hit in parsed_hits if not is_low_value_url(hit["url"])
    ]
    low_value_count = len(parsed_hits) - len(page_hits)

    limit = state.request.max_results
    hits = list(state.hits)
    seen = set(state.seen_urls)
    new_hits: list[SearchHit] = []
    for hit in page_hits:
        if hit["url"] in seen:
            continue
        seen.add(hit["url"])
        hits.append(hit)
        new_hits.append(hit)
        if len(hits) >= limit:
            break

    events.append(
        stage_event(
            "normalization",
            "completed",
            page=action.page,
            selected_count=len(new_hits),
            low_value_count=low_value_count,
            selected=_hit_details(new_hits),
        )
    )
    next_state = replace(
        state,
        hits=tuple(hits),
        seen_urls=frozenset(seen),
        searched_pages=action.page,
    )

    if not new_hits:
        return Transition(next_state, _next_fetch(next_state), tuple(events))
    if len(hits) < limit and action.page < state.request.max_pages:
        next_search = SearchPage(page=action.page + 1, attempt=1)
        return Transition(
            next_state,
            Pause(next_search, kind="page"),
            tuple(events),
        )
    if config.llm_filter_enabled and len(next_state.hits) > 1:
        return Transition(next_state, FilterHits(), tuple(events))
    return Transition(next_state, _next_fetch(next_state), tuple(events))


def search_failed(
    state: PipelineState,
    action: SearchPage,
    error: SearchError,
    config: OrchestratorConfig,
) -> Transition:
    events = [
        stage_event(
            "search",
            "failed",
            page=action.page,
            attempt=action.attempt,
            error_code="search_failed",
            retryable=error.retryable,
        )
    ]
    if error.retryable and action.attempt < config.search_retries + 1:
        retry = trace_event(
            "retry.scheduled",
            stage="search",
            page=action.page,
            attempt=action.attempt + 1,
        )
        events.append(retry)
        next_search = SearchPage(page=action.page, attempt=action.attempt + 1)
        pause = Pause(next_search, kind="retry", attempt=action.attempt)
        return Transition(state, pause, tuple(events))
    failure = PipelineError("search_failed", "Search request failed.")
    return Transition(state, Fail(failure), tuple(events))


def fetch_finished(
    state: PipelineState,
    action: FetchPage,
    outcome: FetchOutcome,
    config: OrchestratorConfig,
) -> Transition:
    if isinstance(outcome, PageSnapshot):
        phase = "completed"
    else:
        phase = "failed"
    details = _fetch_details(outcome, action.ordinal, action.attempt)
    events = [stage_event("fetch", phase, **details)]

    if isinstance(outcome, PageSnapshot):
        return Transition(
            state, CleanPage(outcome, action.ordinal), tuple(events)
        )

    retryable = outcome.error_code in RETRYABLE_FETCH
    if retryable and action.attempt < config.fetch_retries + 1:
        retry = trace_event(
            "retry.scheduled",
            stage="fetch",
            ordinal=action.ordinal,
            attempt=action.attempt + 1,
        )
        events.append(retry)
        next_fetch = FetchPage(action.url, action.ordinal, action.attempt + 1)
        pause = Pause(next_fetch, kind="retry", attempt=action.attempt)
        return Transition(state, pause, tuple(events))

    page = ExtractedPage(
        requested_url=outcome.requested_url,
        warnings=list(outcome.warnings),
        extraction_state="failed",
        error=PageError(code=outcome.error_code, message=outcome.message),
    )
    next_state = _page_finished(state, page)
    return Transition(next_state, _next_fetch(next_state), tuple(events))


def clean_finished(
    state: PipelineState, action: CleanPage, outcome: CleanOutcome
) -> Transition:
    snapshot = action.snapshot
    page = ExtractedPage(
        requested_url=snapshot.requested_url,
        final_url=snapshot.final_url,
        title=snapshot.title,
        status_code=snapshot.status_code,
        content_type=snapshot.content_type,
        render_state=snapshot.render_state,
        warnings=list(snapshot.warnings),
    )

    if isinstance(outcome, CleanFailure):
        event = stage_event(
            "extraction",
            "failed",
            ordinal=action.ordinal,
            url=snapshot.final_url,
            error_code=outcome.error_code,
        )
        page.extraction_state = "failed"
        page.error = PageError(code=outcome.error_code, message=outcome.message)
    else:
        event = stage_event(
            "extraction",
            "completed",
            ordinal=action.ordinal,
            url=snapshot.final_url,
            extraction_state=outcome.extraction_state,
            source_html_bytes=outcome.source_html_bytes,
            markdown_bytes=outcome.markdown_bytes,
        )
        page.extraction_state = outcome.extraction_state
        page.markdown = outcome.markdown_content

    next_state = _page_finished(state, page)
    return Transition(next_state, _next_fetch(next_state), (event,))


def media_finished(
    state: PipelineState, action: TranscribeMedia, outcome: MediaOutcome
) -> Transition:
    if isinstance(outcome, MediaFailure):
        event = stage_event(
            "media",
            "failed",
            ordinal=action.ordinal,
            url=action.url,
            error_code=outcome.error_code,
        )
        page = ExtractedPage(
            requested_url=outcome.requested_url,
            extraction_state="failed",
            media=MediaDetails(
                transcript_state="failed", error_code=outcome.error_code
            ),
            error=PageError(
                code=outcome.error_code, message=outcome.error_message
            ),
        )
    else:
        metadata = outcome.metadata
        markdown = _media_markdown(
            metadata.title,
            metadata.description,
            metadata.uploader,
            metadata.duration_s,
            metadata.platform,
            outcome.transcript,
        )
        event = stage_event(
            "media",
            "completed",
            ordinal=action.ordinal,
            url=metadata.final_url,
            duration_s=metadata.duration_s,
            transcript_source=outcome.transcript_source,
            transcript_state=outcome.transcript_state,
            transcript_chars=len(outcome.transcript),
            error_code=outcome.error_code,
        )
        page = ExtractedPage(
            requested_url=metadata.requested_url,
            final_url=metadata.final_url,
            title=metadata.title or None,
            extraction_state="extracted" if markdown else "empty",
            markdown=markdown,
            media=MediaDetails(
                platform=metadata.platform,
                duration_s=metadata.duration_s,
                transcript_source=outcome.transcript_source,
                transcript_state=outcome.transcript_state,
                transcript_chars=len(outcome.transcript),
                error_code=outcome.error_code,
            ),
        )

    next_state = _page_finished(state, page)
    return Transition(next_state, _next_fetch(next_state), (event,))


def _media_markdown(
    title: str,
    description: str,
    uploader: str | None,
    duration_s: int | None,
    platform: str | None,
    transcript: str,
) -> str:
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
    facts = [uploader, _format_duration(duration_s), platform]
    fact_line = " · ".join(fact for fact in facts if fact)
    if fact_line:
        lines.extend(("", fact_line))
    if description:
        lines.extend(("", description.strip()))
    if transcript:
        lines.extend(("", "## Transcript", "", transcript))
    return "\n".join(lines).strip()


def _format_duration(seconds: int | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def relevance_finished(
    state: PipelineState, outcome: RelevanceOutcome
) -> Transition:
    candidate_count = len(state.hits)
    if isinstance(outcome, RelevanceFailure):
        event = stage_event(
            "relevance",
            "failed",
            candidate_count=candidate_count,
            error_code=outcome.error_code,
        )
        return Transition(state, _next_fetch(state), (event,))

    kept = [hit for hit in state.hits if hit["url"] in outcome.keep]
    dropped = [hit for hit in state.hits if hit["url"] not in outcome.keep]
    next_state = replace(state, hits=tuple(kept))
    events = [
        stage_event(
            "relevance",
            "completed",
            candidate_count=candidate_count,
            kept_count=len(kept),
            dropped_count=len(dropped),
            kept=[hit["url"] for hit in kept],
            dropped=[hit["url"] for hit in dropped],
        )
    ]

    if (
        len(kept) < state.request.max_results
        and state.searched_pages < state.request.max_pages
    ):
        page = state.searched_pages + 1
        events.append(
            trace_event(
                "relevance.backfill",
                page=page,
                kept_count=len(kept),
                wanted=state.request.max_results,
            )
        )
        return Transition(
            next_state,
            Pause(SearchPage(page=page, attempt=1), kind="page"),
            tuple(events),
        )
    return Transition(next_state, _next_fetch(next_state), tuple(events))


def action_started_event(
    state: PipelineState, action: ExecutableAction
) -> TraceEvent:
    if isinstance(action, SearchPage):
        return stage_event(
            "search", "started", page=action.page, attempt=action.attempt
        )
    if isinstance(action, FetchPage):
        return stage_event(
            "fetch",
            "started",
            ordinal=action.ordinal,
            url=action.url,
            attempt=action.attempt,
        )
    if isinstance(action, CleanPage):
        return stage_event(
            "extraction",
            "started",
            ordinal=action.ordinal,
            url=action.snapshot.final_url,
        )
    if isinstance(action, TranscribeMedia):
        return stage_event(
            "media",
            "started",
            ordinal=action.ordinal,
            url=action.url,
        )
    return stage_event(
        "relevance", "started", candidate_count=len(state.hits)
    )


def build_response(state: PipelineState) -> ExtractResponse:
    pages = list(state.pages)
    counts = _counts(pages)
    outcome: RequestOutcome = (
        "completed_with_errors" if counts.failed else "completed"
    )
    return ExtractResponse(
        request_id=str(state.request_id),
        outcome=outcome,
        pages=pages,
        counts=counts,
    )


def _next_fetch(
    state: PipelineState,
) -> FetchPage | TranscribeMedia | Finish:
    """Next page to fetch, capped at max_results."""
    if state.mode == "url":
        return Finish()
    if state.next_hit >= min(len(state.hits), state.request.max_results):
        return Finish()
    hit = state.hits[state.next_hit]
    return _next_action(state, hit["url"], state.next_hit + 1)


def _next_action(
    state: PipelineState, url: str, ordinal: int
) -> FetchPage | TranscribeMedia:
    """Transcribe allowlisted media when STT is on, otherwise scrape."""
    if state.media_enabled and is_media_url(url):
        return TranscribeMedia(url, ordinal)
    return FetchPage(url=url, ordinal=ordinal, attempt=1)


def _page_finished(
    state: PipelineState, page: ExtractedPage
) -> PipelineState:
    return replace(
        state,
        pages=state.pages + (page,),
        next_hit=state.next_hit + 1,
    )


def _hit_details(hits: Sequence[SearchHit]) -> list[dict[str, object]]:
    return [
        {
            "url": hit["url"],
            "title": hit["title"],
            "engines": hit["engines"],
            "category": hit["category"],
            "score": hit["score"],
        }
        for hit in hits
    ]


def _fetch_details(
    outcome: FetchOutcome, ordinal: int, attempt: int
) -> dict[str, object]:
    details: dict[str, object] = {
        "ordinal": ordinal,
        "attempt": attempt,
        "requested_url": outcome.requested_url,
        "warnings": list(outcome.warnings),
    }
    if isinstance(outcome, PageSnapshot):
        details["final_url"] = outcome.final_url
        details["status_code"] = outcome.status_code
        details["content_type"] = outcome.content_type
        details["render_state"] = outcome.render_state
        details["html_bytes"] = len(outcome.raw_page_html.encode("utf-8"))
    else:
        details["error_code"] = outcome.error_code
        details["message"] = outcome.message
    return details


def _counts(pages: Sequence[ExtractedPage]) -> Counts:
    return Counts(
        selected=len(pages),
        fetched=sum(page.render_state is not None for page in pages),
        extracted=sum(page.extraction_state == "extracted" for page in pages),
        empty=sum(page.extraction_state == "empty" for page in pages),
        failed=sum(page.extraction_state == "failed" for page in pages),
        not_attempted=sum(
            page.extraction_state == "not_attempted" for page in pages
        ),
        transcribed=sum(
            page.media is not None
            and page.media.transcript_state == "extracted"
            for page in pages
        ),
    )
