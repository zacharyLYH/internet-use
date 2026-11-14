import logging
import queue
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from queue import Empty, Full
from uuid import UUID

from app.cleaner import clean_to_markdown
from app.fetcher import FetchOutcome, fetch_target_page
from app.pipeline import (
    Action,
    CleanPage,
    Counts,
    ExtractRequest,
    ExtractResponse,
    ExtractedPage,
    ExtractionState,
    Fail,
    FetchPage,
    Finish,
    OrchestratorConfig,
    PageError,
    Pause,
    PipelineError,
    PipelineState,
    RequestOutcome,
    SearchPage,
    Transition,
    action_started_event,
    build_response,
    clean_finished,
    fetch_finished,
    search_failed,
    search_succeeded,
    start_pipeline,
)
from app.search import SearchError, fetch_search_response
from app.tracer import (
    TraceError,
    TraceEvent,
    request_event,
    trace_event,
    write_events,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Counts",
    "ExtractRequest",
    "ExtractResponse",
    "ExtractedPage",
    "ExtractionState",
    "Orchestrator",
    "OrchestratorConfig",
    "PageError",
    "PipelineError",
    "QueueExpiredError",
    "QueueFullError",
    "QueueStoppedError",
    "RequestOutcome",
    "RequestQueue",
]


class QueueFullError(RuntimeError):
    pass


class QueueExpiredError(RuntimeError):
    pass


class QueueStoppedError(RuntimeError):
    pass


@dataclass(slots=True)
class _Job:
    request_id: UUID
    request: ExtractRequest
    deadline: float
    future: Future[ExtractResponse]
    started: threading.Event
    cancelled: threading.Event


class Orchestrator:
    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        self._config = config or OrchestratorConfig()

    def run(self, request_id: UUID, request: ExtractRequest) -> ExtractResponse:
        started = time.monotonic()
        transition = start_pipeline(request_id, request)
        self._emit(request_id, transition.events)
        state = transition.state
        action = transition.action

        while True:
            if isinstance(action, Finish):
                result = build_response(state)
                self._emit(
                    request_id,
                    (
                        request_event(
                            result.outcome,
                            outcome=result.outcome,
                            counts=result.counts.model_dump(),
                            elapsed_ms=_elapsed_ms(started),
                        ),
                    ),
                )
                return result

            if isinstance(action, Fail):
                self._emit(
                    request_id,
                    (
                        request_event(
                            "failed",
                            error_code=action.error.code,
                            elapsed_ms=_elapsed_ms(started),
                        ),
                    ),
                )
                raise action.error

            if isinstance(action, Pause):
                self._sleep(action)
                action = action.next_action
                continue

            started_event = action_started_event(action)
            if started_event is not None:
                self._emit(request_id, (started_event,))

            transition = self._execute(state, action)
            self._emit(request_id, transition.events)
            state = transition.state
            action = transition.action

    def _execute(
        self,
        state: PipelineState,
        action: SearchPage | FetchPage | CleanPage,
    ) -> Transition:
        if isinstance(action, SearchPage):
            try:
                search_response = fetch_search_response(
                    (state.request.query or "").strip(),
                    pageno=action.page,
                    timeout_s=self._config.search_timeout_s,
                )
            except SearchError as exc:
                return search_failed(state, action, exc, self._config)
            return search_succeeded(state, action, search_response)

        if isinstance(action, FetchPage):
            outcome = fetch_target_page(
                action.url, total_timeout_s=self._config.fetch_timeout_s
            )
            return fetch_finished(state, action, outcome, self._config)

        cleaned = clean_to_markdown(
            action.snapshot.raw_page_html,
            action.snapshot.final_url,
        )
        return clean_finished(state, action, cleaned)

    def _sleep(self, pause: Pause) -> None:
        if pause.kind == "retry":
            time.sleep(self._config.retry_backoff_s * pause.attempt)
        elif self._config.page_delay_max_s > 0:
            time.sleep(
                random.uniform(
                    self._config.page_delay_min_s,
                    self._config.page_delay_max_s,
                )
            )

    def _emit(self, request_id: UUID, events: tuple[TraceEvent, ...]) -> None:
        write_events(request_id, events)


class RequestQueue:
    def __init__(
        self,
        orchestrator: Orchestrator,
        *,
        max_size: int,
        wait_s: float,
        on_worker_stop: Callable[[], None] | None = None,
    ) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        if wait_s <= 0:
            raise ValueError("wait_s must be > 0")
        self._orchestrator = orchestrator
        self._wait_s = wait_s
        self._queue: queue.Queue[_Job | None] = queue.Queue(maxsize=max_size)
        self._stopping = threading.Event()
        self._on_worker_stop = on_worker_stop
        self._thread = threading.Thread(
            target=self._work, name="pipeline-worker", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        try:
            self._queue.put_nowait(None)
        except Full:
            pass
        self._thread.join(timeout=10)
        while True:
            try:
                job = self._queue.get_nowait()
            except Empty:
                break
            if job is not None and not job.future.done():
                job.started.set()
                job.future.set_exception(QueueStoppedError("queue stopped"))

    def submit(self, request_id: UUID, request: ExtractRequest) -> ExtractResponse:
        job = _Job(
            request_id=request_id,
            request=request,
            deadline=time.monotonic() + self._wait_s,
            future=Future(),
            started=threading.Event(),
            cancelled=threading.Event(),
        )
        try:
            self._queue.put_nowait(job)
        except Full as exc:
            raise QueueFullError("request queue is full") from exc
        if not job.started.wait(timeout=self._wait_s):
            job.cancelled.set()
            raise QueueExpiredError("request timed out while queued")
        return job.future.result()

    def _work(self) -> None:
        try:
            while True:
                job = self._next_job()
                if job is None:
                    return
                if self._expire(job):
                    continue
                job.started.set()
                try:
                    result = self._orchestrator.run(job.request_id, job.request)
                except Exception as exc:
                    if not job.future.done():
                        job.future.set_exception(exc)
                else:
                    if not job.future.done():
                        job.future.set_result(result)
        finally:
            if self._on_worker_stop is not None:
                try:
                    self._on_worker_stop()
                except Exception:
                    logger.exception("worker shutdown callback failed")

    def _next_job(self) -> _Job | None:
        while True:
            try:
                job = self._queue.get(timeout=0.2)
            except Empty:
                if self._stopping.is_set():
                    return None
                continue
            if job is None:
                return None
            return job

    def _expire(self, job: _Job) -> bool:
        if not job.cancelled.is_set() and time.monotonic() < job.deadline:
            return False
        try:
            write_events(
                job.request_id,
                (
                    trace_event(
                        "request.cancelled",
                        reason="queue_expired",
                    ),
                ),
            )
        except TraceError:
            logger.warning("trace write failed while expiring queued request")
        job.started.set()
        if not job.future.done():
            job.future.set_exception(QueueExpiredError("queue wait expired"))
        return True


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
