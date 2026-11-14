import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.cleaner import CleanFailure, CleanOutcome
from app.fetcher import (
    FetchFailure,
    FetchOutcome,
    PageSnapshot,
    RenderState,
    WarningCode,
)
from app.parser import SearchHit, parse_search_response
from app.search import SearchError
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
    error: PageError | None = None


class Counts(BaseModel):
    selected: int = 0
    fetched: int = 0
    extracted: int = 0
    empty: int = 0
    failed: int = 0
    not_attempted: int = 0


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
    target_url: str | None
    hits: tuple[SearchHit, ...] = ()
    seen_urls: frozenset[str] = frozenset()
    pages: tuple[ExtractedPage, ...] = ()
    next_hit: int = 0


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


Action = SearchPage | FetchPage | CleanPage | Pause | Finish | Fail


@dataclass(frozen=True, slots=True)
class Transition:
    state: PipelineState
    action: Action
    events: tuple[TraceEvent, ...] = ()


def start_pipeline(request_id: UUID, request: ExtractRequest) -> Transition:
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
    state = PipelineState(
        request_id=request_id,
        request=request,
        mode=mode,
        target_url=url or None,
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
    return Transition(state, FetchPage(url, ordinal=1, attempt=1), events)


def search_succeeded(
    state: PipelineState,
    action: SearchPage,
    response: Mapping[str, Any],
) -> Transition:
    result_count = _result_count(response)
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
        page_hits = parse_search_response(response)
    except ValueError:
        events.append(
            stage_event(
                "normalization",
                "failed",
                page=action.page,
                error_code="normalization_failed",
            )
        )
        return Transition(
            state,
            Fail(
                PipelineError(
                    "normalization_failed",
                    "Search response could not be normalized.",
                )
            ),
            tuple(events),
        )

    hits = list(state.hits)
    seen = set(state.seen_urls)
    new_hits: list[SearchHit] = []
    for hit in page_hits:
        if hit["url"] in seen:
            continue
        seen.add(hit["url"])
        hits.append(hit)
        new_hits.append(hit)
        if len(hits) >= state.request.max_results:
            break

    events.append(
        stage_event(
            "normalization",
            "completed",
            page=action.page,
            selected_count=len(new_hits),
            selected=_hit_details(new_hits),
        )
    )
    next_state = replace(
        state, hits=tuple(hits), seen_urls=frozenset(seen)
    )

    if (
        not new_hits
        or len(hits) >= state.request.max_results
        or action.page >= state.request.max_pages
    ):
        return Transition(next_state, _next_page(next_state), tuple(events))

    next_search = SearchPage(page=action.page + 1, attempt=1)
    return Transition(
        next_state,
        Pause(next_search, kind="page"),
        tuple(events),
    )


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
        events.append(
            trace_event(
                "retry.scheduled",
                stage="search",
                page=action.page,
                attempt=action.attempt + 1,
            )
        )
        next_search = SearchPage(
            page=action.page, attempt=action.attempt + 1
        )
        return Transition(
            state,
            Pause(next_search, kind="retry", attempt=action.attempt),
            tuple(events),
        )
    return Transition(
        state,
        Fail(PipelineError("search_failed", "Search request failed.")),
        tuple(events),
    )


def fetch_finished(
    state: PipelineState,
    action: FetchPage,
    outcome: FetchOutcome,
    config: OrchestratorConfig,
) -> Transition:
    phase = (
        "completed" if isinstance(outcome, PageSnapshot) else "failed"
    )
    events = [
        stage_event(
            "fetch",
            phase,
            **_fetch_details(outcome, action.ordinal, action.attempt),
        )
    ]

    if isinstance(outcome, PageSnapshot):
        return Transition(
            state, CleanPage(outcome, action.ordinal), tuple(events)
        )

    retryable = outcome.error_code in RETRYABLE_FETCH
    if retryable and action.attempt < config.fetch_retries + 1:
        events.append(
            trace_event(
                "retry.scheduled",
                stage="fetch",
                ordinal=action.ordinal,
                attempt=action.attempt + 1,
            )
        )
        next_fetch = FetchPage(
            url=action.url,
            ordinal=action.ordinal,
            attempt=action.attempt + 1,
        )
        return Transition(
            state,
            Pause(next_fetch, kind="retry", attempt=action.attempt),
            tuple(events),
        )

    page = ExtractedPage(
        requested_url=outcome.requested_url,
        warnings=list(outcome.warnings),
        extraction_state="failed",
        error=PageError(code=outcome.error_code, message=outcome.message),
    )
    next_state = _page_finished(state, page)
    return Transition(next_state, _next_page(next_state), tuple(events))


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
    return Transition(next_state, _next_page(next_state), (event,))


def action_started_event(action: Action) -> TraceEvent | None:
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
    return None


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


def _next_page(state: PipelineState) -> FetchPage | Finish:
    if state.mode == "url":
        return Finish()
    if state.next_hit >= len(state.hits):
        return Finish()
    hit = state.hits[state.next_hit]
    return FetchPage(
        url=hit["url"],
        ordinal=state.next_hit + 1,
        attempt=1,
    )


def _page_finished(
    state: PipelineState, page: ExtractedPage
) -> PipelineState:
    return replace(
        state,
        pages=state.pages + (page,),
        next_hit=state.next_hit + 1,
    )


def _result_count(response: Mapping[str, Any]) -> int:
    results = response.get("results")
    return len(results) if isinstance(results, list) else 0


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
        details.update(
            {
                "final_url": outcome.final_url,
                "status_code": outcome.status_code,
                "content_type": outcome.content_type,
                "render_state": outcome.render_state,
                "html_bytes": len(outcome.raw_page_html.encode("utf-8")),
            }
        )
    else:
        details.update(
            {
                "error_code": outcome.error_code,
                "message": outcome.message,
            }
        )
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
    )
