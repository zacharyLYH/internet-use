import logging
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint

from app.auth import is_authorized, load_api_keys
from app.fetcher import shutdown_fetcher
from app.orchestrator import (
    ExtractRequest,
    ExtractResponse,
    Orchestrator,
    OrchestratorConfig,
    PipelineError,
    QueueExpiredError,
    QueueFullError,
    QueueStoppedError,
    RequestQueue,
)
from app.tracer import TraceError, close_tracer, init_tracer, write_event
from app.urls import is_valid_http_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AppSettings:
    trace_db_path: Path
    api_keys_file: Path
    queue_max_size: int = 100
    queue_wait_s: float = 30.0
    max_request_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        if self.queue_max_size < 1:
            raise ValueError("queue_max_size must be >= 1")
        if self.queue_wait_s <= 0:
            raise ValueError("queue_wait_s must be > 0")
        if self.max_request_bytes < 1:
            raise ValueError("max_request_bytes must be >= 1")

    @classmethod
    def from_env(cls) -> Self:
        return cls(
            trace_db_path=Path(
                os.getenv("TRACE_DB_PATH", "data/traces.sqlite3")
            ),
            api_keys_file=Path(os.getenv("API_KEYS_FILE", "data/api_keys.txt")),
            queue_max_size=int(os.getenv("QUEUE_MAX_SIZE", "100")),
            queue_wait_s=float(os.getenv("QUEUE_WAIT_S", "30")),
            max_request_bytes=int(os.getenv("MAX_REQUEST_BYTES", "1000000")),
        )


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class Health(BaseModel):
    status: str


def create_app(
    settings: AppSettings | None = None,
    orchestrator_config: OrchestratorConfig | None = None,
) -> FastAPI:
    resolved = settings or AppSettings.from_env()
    config = orchestrator_config or OrchestratorConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            init_tracer(resolved.trace_db_path)
            api_keys = load_api_keys(resolved.api_keys_file)
        except Exception:
            close_tracer()
            raise
        request_queue = RequestQueue(
            Orchestrator(config),
            max_size=resolved.queue_max_size,
            wait_s=resolved.queue_wait_s,
            on_worker_stop=shutdown_fetcher,
        )
        request_queue.start()
        app.state.api_keys = api_keys
        app.state.request_queue = request_queue
        app.state.settings = resolved
        try:
            yield
        finally:
            request_queue.stop()
            close_tracer()

    app = FastAPI(title="internet-use", lifespan=lifespan)

    @app.middleware("http")
    async def ingress(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = uuid4()
        request.state.request_id = request_id
        client_ip = request.client.host if request.client else ""
        ingress_details = {
            "method": request.method,
            "path": request.url.path,
            "source_ip": client_ip,
            "user_agent": request.headers.get("user-agent", "")[:300],
        }
        if not await _record_event(request_id, "api.ingress", ingress_details):
            return _trace_unavailable(request_id)

        if request.url.path == "/health":
            response = await call_next(request)
            response.headers["X-Request-ID"] = str(request_id)
            return response

        length_header = request.headers.get("content-length")
        if length_header is not None:
            try:
                too_large = int(length_header) > resolved.max_request_bytes
            except ValueError:
                too_large = False
            if too_large:
                if not await _record_event(
                    request_id,
                    "request.rejected",
                    {"reason": "body_too_large"},
                ):
                    return _trace_unavailable(request_id)
                return _error_response(
                    413, request_id, "request_too_large", "Request body is too large."
                )

        authorization = request.headers.get("authorization")
        if not is_authorized(authorization, request.app.state.api_keys):
            if not await _record_event(
                request_id,
                "api.auth_rejected",
                {"reason": "missing_or_invalid"},
            ):
                return _trace_unavailable(request_id)
            return _error_response(
                401, request_id, "unauthorized", "A valid API key is required."
            )

        if not await _record_event(request_id, "api.auth_succeeded", {}):
            return _trace_unavailable(request_id)

        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled request error")
            return _error_response(
                500, request_id, "internal_error", "The request failed."
            )
        response.headers["X-Request-ID"] = str(request_id)
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(
            exc.status_code, _request_id(request), exc.code, exc.message
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = _request_id(request)
        if not await _record_event(request_id, "request.validation_failed", {}):
            return _trace_unavailable(request_id)
        return _error_response(
            400, request_id, "invalid_request", "Request body is invalid."
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        return _error_response(
            exc.status_code, _request_id(request), code, "Request failed."
        )

    @app.get("/health")
    def health() -> Health:
        return Health(status="ok")

    @app.post("/extract", response_model=ExtractResponse)
    def extract(payload: ExtractRequest, request: Request) -> ExtractResponse:
        query = (payload.query or "").strip()
        url = (payload.url or "").strip()
        if bool(query) == bool(url):
            raise ApiError(
                400,
                "invalid_request",
                "Exactly one of query or url is required.",
            )
        if url and not is_valid_http_url(url):
            raise ApiError(
                400,
                "invalid_request",
                "url must be an absolute http(s) URL without credentials.",
            )

        request_queue: RequestQueue = request.app.state.request_queue
        try:
            return request_queue.submit(_request_id(request), payload)
        except QueueFullError as exc:
            raise ApiError(
                503, "queue_full", "The request queue is full."
            ) from exc
        except QueueExpiredError as exc:
            raise ApiError(
                504,
                "queue_timeout",
                "The request timed out while waiting.",
            ) from exc
        except QueueStoppedError as exc:
            raise ApiError(
                503, "service_unavailable", "The service is shutting down."
            ) from exc
        except PipelineError as exc:
            raise ApiError(exc.status_code, exc.code, exc.message) from exc
        except TraceError as exc:
            raise ApiError(
                503, "trace_unavailable", "Trace storage is unavailable."
            ) from exc
        except Exception as exc:
            logger.exception("pipeline request failed")
            raise ApiError(
                500, "internal_error", "The request failed."
            ) from exc

    return app


def _request_id(request: Request) -> UUID:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, UUID) else uuid4()


async def _record_event(
    request_id: UUID, event_type: str, details: Mapping[str, object]
) -> bool:
    try:
        await run_in_threadpool(write_event, event_type, request_id, details)
    except TraceError:
        return False
    return True


def _trace_unavailable(request_id: UUID) -> JSONResponse:
    return _error_response(
        503,
        request_id,
        "trace_unavailable",
        "Trace storage is unavailable.",
    )


def _error_response(
    status_code: int, request_id: UUID, code: str, message: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "request_id": str(request_id),
            "error": {"code": code, "message": message},
        },
        headers={"X-Request-ID": str(request_id)},
    )


app = create_app()
