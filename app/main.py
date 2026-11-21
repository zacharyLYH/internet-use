import logging
import os
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint

from app import stt
from app.auth import is_authorized, load_api_keys
from app.fetcher import shutdown_fetcher
from app.orchestrator import (
    Orchestrator,
    QueueExpiredError,
    QueueFullError,
    QueueStoppedError,
    RequestQueue,
)
from app.pipeline import (
    ExtractRequest,
    ExtractResponse,
    OrchestratorConfig,
    PipelineError,
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
    max_audio_bytes: int = 25_000_000

    def __post_init__(self) -> None:
        if self.queue_max_size < 1:
            raise ValueError("queue_max_size must be >= 1")
        if self.queue_wait_s <= 0:
            raise ValueError("queue_wait_s must be > 0")
        if self.max_request_bytes < 1:
            raise ValueError("max_request_bytes must be >= 1")
        if self.max_audio_bytes < 1:
            raise ValueError("max_audio_bytes must be >= 1")

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
            max_audio_bytes=int(os.getenv("MAX_AUDIO_BYTES", "25000000")),
        )


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class Health(BaseModel):
    status: str


class ChatContentPart(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    text: str | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[ChatContentPart] | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    max_results: int | None = Field(default=None, ge=1, le=20)
    max_pages: int | None = Field(default=None, ge=1, le=10)


class TranscriptionResponse(BaseModel):
    text: str


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
            limit = (
                resolved.max_audio_bytes
                if request.url.path == "/v1/audio/transcriptions"
                else resolved.max_request_bytes
            )
            try:
                too_large = int(length_header) > limit
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

    @app.post("/v1/chat/completions", response_model=ExtractResponse)
    def chat_completions(
        payload: ChatCompletionRequest, request: Request
    ) -> ExtractResponse:
        return _submit(request, _extract_request_from_chat(payload))

    @app.post("/v1/extract", response_model=ExtractResponse)
    def extract(payload: ExtractRequest, request: Request) -> ExtractResponse:
        return _submit(request, payload)

    @app.post(
        "/v1/audio/transcriptions", response_model=TranscriptionResponse
    )
    def audio_transcriptions(
        request: Request,
        file: UploadFile = File(...),
        model: str | None = Form(default=None),
        language: str | None = Form(default=None),
        response_format: str | None = Form(default=None),
    ) -> TranscriptionResponse:
        return _transcribe_upload(
            request,
            file,
            language=language or config.stt_language or None,
            response_format=response_format,
            timeout_s=config.stt_timeout_s,
        )

    return app


def _transcribe_upload(
    request: Request,
    upload: UploadFile,
    *,
    language: str | None,
    response_format: str | None,
    timeout_s: float,
) -> TranscriptionResponse:
    if response_format not in (None, "", "json"):
        raise ApiError(
            400, "invalid_request", "Only response_format=json is supported."
        )
    if not stt.is_configured():
        raise ApiError(
            503, "stt_not_configured", "STT endpoint is not configured."
        )
    settings: AppSettings = request.app.state.settings
    data = upload.file.read(settings.max_audio_bytes + 1)
    if not data:
        raise ApiError(400, "invalid_request", "Audio file is empty.")
    if len(data) > settings.max_audio_bytes:
        raise ApiError(413, "request_too_large", "Audio file is too large.")

    suffix = Path(upload.filename or "").suffix.lower() or ".mp3"
    with tempfile.TemporaryDirectory(prefix="internet-use-stt-") as tmp:
        path = Path(tmp) / f"upload{suffix}"
        path.write_bytes(data)
        outcome = stt.transcribe_audio(
            path,
            timeout_s=timeout_s,
            language_hint=language or None,
        )

    if isinstance(outcome, stt.TranscriptionSuccess):
        return TranscriptionResponse(text=outcome.text)
    if outcome.error_code == "stt_empty_transcript":
        return TranscriptionResponse(text="")
    if outcome.error_code == "stt_not_configured":
        raise ApiError(503, "stt_not_configured", outcome.message)
    code = (
        "upstream_stt_failed"
        if outcome.error_code == "stt_request_failed"
        else "upstream_stt_invalid_response"
    )
    raise ApiError(502, code, outcome.message)


def _submit(request: Request, payload: ExtractRequest) -> ExtractResponse:
    request_queue: RequestQueue = request.app.state.request_queue
    try:
        return request_queue.submit(_request_id(request), payload)
    except QueueFullError as exc:
        raise ApiError(503, "queue_full", "The request queue is full.") from exc
    except QueueExpiredError as exc:
        message = "The request timed out while waiting."
        raise ApiError(504, "queue_timeout", message) from exc
    except QueueStoppedError as exc:
        message = "The service is shutting down."
        raise ApiError(503, "service_unavailable", message) from exc
    except PipelineError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from exc
    except TraceError as exc:
        message = "Trace storage is unavailable."
        raise ApiError(503, "trace_unavailable", message) from exc
    except Exception as exc:
        logger.exception("pipeline request failed")
        raise ApiError(500, "internal_error", "The request failed.") from exc


def _extract_request_from_chat(payload: ChatCompletionRequest) -> ExtractRequest:
    text = _chat_text(payload)
    max_results = payload.max_results or 5
    max_pages = payload.max_pages or 3
    if is_valid_http_url(text):
        return ExtractRequest(url=text, max_results=max_results, max_pages=max_pages)
    return ExtractRequest(query=text, max_results=max_results, max_pages=max_pages)


def _chat_text(payload: ChatCompletionRequest) -> str:
    for role in ("user", None):
        for message in reversed(payload.messages):
            if role is not None and message.role != role:
                continue
            content = _message_text(message)
            if content:
                return content
    required = "A message with text content is required."
    raise ApiError(400, "invalid_request", required)


def _message_text(message: ChatMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            part.text.strip()
            for part in content
            if part.type == "text" and part.text and part.text.strip()
        ).strip()
    return ""


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
    message = "Trace storage is unavailable."
    return _error_response(503, request_id, "trace_unavailable", message)


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
