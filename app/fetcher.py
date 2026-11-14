import atexit
import ipaddress
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import SplitResult, urlsplit

from patchright.sync_api import (
    BrowserContext,
    Error,
    Page,
    Playwright,
    Request,
    Response,
    TimeoutError,
    sync_playwright,
)

from app.urls import parse_http_url

ErrorCode = Literal[
    "invalid_url",
    "blocked_destination",
    "navigation_timeout",
    "network_error",
    "redirect_error",
    "unsupported_content_type",
    "response_too_large",
    "browser_unavailable",
]
WarningCode = Literal[
    "render_timeout",
    "possible_challenge",
    "empty_render",
    "resource_failures",
    "blocked_destination",
]
RenderState = Literal["complete", "partial"]
SettleResult = Literal["settled", "timeout", "too_large"]

MESSAGES: dict[ErrorCode, str] = {
    "invalid_url": "URL must be an absolute http(s) URL without credentials.",
    "blocked_destination": "Destination is not permitted.",
    "navigation_timeout": "Page did not become ready before the deadline.",
    "network_error": "Page could not be reached.",
    "redirect_error": "Page redirected incorrectly.",
    "unsupported_content_type": "Main document is not HTML.",
    "response_too_large": "Page HTML exceeds the configured limit.",
    "browser_unavailable": "Browser is unavailable.",
}


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    requested_url: str
    final_url: str
    title: str
    status_code: int | None
    content_type: str | None
    raw_page_html: str
    render_state: RenderState
    warnings: tuple[WarningCode, ...] = ()


@dataclass(frozen=True, slots=True)
class FetchFailure:
    requested_url: str
    error_code: ErrorCode
    message: str
    warnings: tuple[WarningCode, ...] = ()


FetchOutcome = PageSnapshot | FetchFailure

PROFILE_DIR = Path(os.getenv("BROWSER_PROFILE_DIR", "data/browser-profile"))
MAX_HTML_CHARS = int(os.getenv("MAX_HTML_CHARS", "5000000"))
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "30000"))
SETTLE_TIMEOUT_S = float(os.getenv("SETTLE_TIMEOUT_S", "8"))
TOTAL_TIMEOUT_S = float(os.getenv("TOTAL_TIMEOUT_S", "30"))

_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_CHALLENGE_MARKERS = (
    "captcha",
    "access denied",
    "are you a robot",
    "just a moment",
    "verify you are human",
    "attention required",
)
logger = logging.getLogger(__name__)


class Browser:
    def __init__(
        self,
        profile_dir: Path = PROFILE_DIR,
        *,
        headless: bool = False,
        allow_private: bool = False,
        max_html_chars: int = MAX_HTML_CHARS,
        nav_timeout_ms: int = NAV_TIMEOUT_MS,
        settle_timeout_s: float = SETTLE_TIMEOUT_S,
        total_timeout_s: float = TOTAL_TIMEOUT_S,
    ) -> None:
        self._profile_dir = profile_dir
        self._headless = headless
        self._allow_private = allow_private
        self._max_html_chars = max_html_chars
        self._nav_timeout_ms = nav_timeout_ms
        self._settle_timeout_s = settle_timeout_s
        self._total_timeout_s = total_timeout_s
        self._lock = threading.Lock()
        self._context: BrowserContext | None = None
        self._host_cache: dict[str, bool] = {}

    def fetch(
        self, url: str, *, total_timeout_s: float | None = None
    ) -> FetchOutcome:
        started = time.monotonic()
        budget = self._total_timeout_s
        if total_timeout_s is not None:
            budget = min(budget, total_timeout_s)
        outcome = self._fetch(url, started, budget)
        host = (urlsplit(outcome.requested_url).hostname or "").lower()
        status: int | None | str
        render: str
        if isinstance(outcome, PageSnapshot):
            status, error, render = outcome.status_code, "", outcome.render_state
        else:
            status, error, render = "", outcome.error_code, ""
        logger.info(
            "fetch host=%s status=%s error=%s render=%s warnings=%s duration_ms=%d",
            host,
            status,
            error,
            render,
            ",".join(outcome.warnings),
            int((time.monotonic() - started) * 1000),
        )
        return outcome

    def _fetch(
        self, url: str, started: float, total_timeout_s: float
    ) -> FetchOutcome:
        requested = url.strip()
        parsed = parse_http_url(requested)
        if parsed is None:
            return _failure(requested, "invalid_url")
        initial = _initial_error(parsed, self._allow_private)
        if initial is not None:
            return _failure(requested, initial)
        if total_timeout_s <= 0:
            return _failure(requested, "navigation_timeout")

        budget = max(0.0, total_timeout_s - (time.monotonic() - started))
        if not self._lock.acquire(timeout=budget):
            return _failure(requested, "navigation_timeout")
        try:
            return self._fetch_locked(requested, started, total_timeout_s)
        except Error:
            self._close_context()
            return _failure(requested, "browser_unavailable")
        finally:
            self._host_cache.clear()
            self._lock.release()

    def close(self) -> None:
        if not self._lock.acquire(timeout=5):
            return
        try:
            self._close_context()
        finally:
            self._lock.release()

    def _fetch_locked(
        self, requested: str, started: float, total_timeout_s: float
    ) -> FetchOutcome:
        context = self._ensure_context()
        page = context.new_page()
        warnings: set[WarningCode] = set()
        blocked_main = False
        failures = 0
        response: Response | None = None
        timed_out = False
        main_frame: str | None = None
        cdp = context.new_cdp_session(page)

        def on_paused(params: dict[str, Any]) -> None:
            nonlocal blocked_main, main_frame
            request_id = str(params["requestId"])
            frame = str(params.get("frameId", ""))
            is_document = params.get("resourceType") == "Document"
            if is_document and main_frame is None:
                main_frame = frame
            target = urlsplit(str(params["request"]["url"]))
            host = (target.hostname or "").lower()
            if target.scheme in ("http", "https") and host:
                allowed = _host_allowed(host, self._allow_private, self._host_cache)
            else:
                allowed = not is_document
            if allowed:
                cdp.send("Fetch.continueRequest", {"requestId": request_id})
                return
            if is_document and frame == main_frame:
                blocked_main = True
            warnings.add("blocked_destination")
            cdp.send(
                "Fetch.failRequest",
                {"requestId": request_id, "errorReason": "BlockedByClient"},
            )

        def on_failed(request: Request) -> None:
            nonlocal failures
            failures += 1

        def on_popup(popup: Page) -> None:
            try:
                popup.close()
            except Error:
                pass

        cdp.on("Fetch.requestPaused", on_paused)
        cdp.send(
            "Fetch.enable",
            {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
        )
        page.on("requestfailed", on_failed)
        page.on("popup", on_popup)
        try:
            try:
                response = page.goto(
                    requested,
                    wait_until="domcontentloaded",
                    timeout=_nav_timeout_ms(self._nav_timeout_ms, total_timeout_s),
                )
            except TimeoutError:
                timed_out = True
            except Error as exc:
                if blocked_main:
                    return _failure(requested, "blocked_destination")
                return _failure(requested, _nav_error_code(exc))

            if blocked_main:
                return _failure(requested, "blocked_destination")
            if timed_out and page.url == "about:blank":
                return _failure(requested, "navigation_timeout")
            if not _url_allowed(page.url, self._allow_private, self._host_cache):
                return _failure(requested, "blocked_destination")

            content_type = _content_type(response)
            if content_type and content_type not in _HTML_TYPES:
                return _failure(requested, "unsupported_content_type")

            settle = self._settle(page, started, total_timeout_s)
            if settle == "too_large":
                return _failure(requested, "response_too_large")
            if timed_out or settle == "timeout":
                warnings.add("render_timeout")
            html = page.content()
            title = page.title()
            text_length = _text_length(page)
            if text_length == 0:
                warnings.add("empty_render")
            if _looks_like_challenge(title, html):
                warnings.add("possible_challenge")
            if failures:
                warnings.add("resource_failures")
            return PageSnapshot(
                requested_url=requested,
                final_url=page.url,
                title=title,
                status_code=response.status if response else None,
                content_type=content_type or None,
                raw_page_html=html,
                render_state=(
                    "partial"
                    if timed_out or settle != "settled" or text_length == 0
                    else "complete"
                ),
                warnings=tuple(sorted(warnings)),
            )
        finally:
            try:
                page.close()
            except Error:
                pass
            for other in list(context.pages):
                if other is not page:
                    try:
                        other.close()
                    except Error:
                        pass

    def _ensure_context(self) -> BrowserContext:
        if self._context is not None:
            return self._context
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._profile_dir.chmod(0o700)
        executable = os.getenv("CHROME_EXECUTABLE")
        options: dict[str, Any] = {
            "headless": self._headless,
            "no_viewport": True,
        }
        if executable:
            options["executable_path"] = executable
        else:
            options["channel"] = "chrome"
        self._context = _get_playwright().chromium.launch_persistent_context(
            str(self._profile_dir.resolve()),
            **options,
        )
        return self._context

    def _close_context(self) -> None:
        context, self._context = self._context, None
        if context is not None:
            try:
                context.close()
            except Error:
                pass

    def _settle(
        self, page: Page, started: float, total_timeout_s: float
    ) -> SettleResult:
        deadline = min(
            started + total_timeout_s, time.monotonic() + self._settle_timeout_s
        )
        previous: tuple[str, int, str] | None = None
        stable = 0
        while time.monotonic() < deadline:
            try:
                current = (page.url, _html_length(page), page.title())
            except Error:
                return "timeout"
            if current[1] > self._max_html_chars:
                return "too_large"
            if current == previous:
                stable += 1
                if stable >= 2:
                    return "settled"
            else:
                previous, stable = current, 0
            time.sleep(0.25)
        return "timeout"


def _failure(url: str, code: ErrorCode) -> FetchFailure:
    return FetchFailure(url, code, MESSAGES[code])


def _initial_error(parsed: SplitResult, allow_private: bool) -> ErrorCode | None:
    host = parsed.hostname
    if host is None:
        return "invalid_url"
    addresses = _resolve(host)
    if not addresses:
        return "network_error"
    if all(_ip_allowed(address, allow_private) for address in addresses):
        return None
    return "blocked_destination"


def _url_allowed(url: str, allow_private: bool, cache: dict[str, bool]) -> bool:
    parsed = parse_http_url(url)
    if parsed is None or parsed.hostname is None:
        return False
    return _host_allowed(parsed.hostname, allow_private, cache)


def _host_allowed(host: str, allow_private: bool, cache: dict[str, bool]) -> bool:
    if host in cache:
        return cache[host]
    addresses = _resolve(host)
    if not addresses:
        return True
    allowed = all(_ip_allowed(address, allow_private) for address in addresses)
    cache[host] = allowed
    return allowed


def _resolve(host: str) -> list[str]:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [host]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    addresses: set[str] = set()
    for info in infos:
        address = info[4][0]
        if isinstance(address, str):
            addresses.add(address)
    return list(addresses)


def _ip_allowed(value: str, allow_private: bool) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_link_local or address.is_multicast or address.is_reserved:
        return False
    return address.is_global or (allow_private and address.is_private)


def _nav_error_code(exc: Error) -> ErrorCode:
    return "redirect_error" if "ERR_TOO_MANY_REDIRECTS" in str(exc).upper() else "network_error"


def _nav_timeout_ms(configured_ms: int, total_timeout_s: float) -> int:
    return max(1, min(configured_ms, int(total_timeout_s * 1000)))


def _content_type(response: Response | None) -> str:
    if response is None:
        return ""
    return (response.headers.get("content-type") or "").split(";")[0].strip().lower()


def _html_length(page: Page) -> int:
    try:
        return int(page.evaluate("document.documentElement.outerHTML.length"))
    except (Error, ValueError):
        return 0


def _text_length(page: Page) -> int:
    try:
        return int(page.evaluate("document.body ? document.body.innerText.trim().length : 0"))
    except (Error, ValueError):
        return 0


def _looks_like_challenge(title: str, html: str) -> bool:
    probe = f"{title}\n{html[:5000]}".lower()
    return any(marker in probe for marker in _CHALLENGE_MARKERS)


_playwright_lock = threading.Lock()
_playwright: Playwright | None = None


def _get_playwright() -> Playwright:
    global _playwright
    with _playwright_lock:
        if _playwright is None:
            _playwright = sync_playwright().start()
        return _playwright


def _stop_playwright() -> None:
    global _playwright
    with _playwright_lock:
        playwright, _playwright = _playwright, None
    if playwright is not None:
        try:
            playwright.stop()
        except Exception:
            pass


_browser = Browser()


def shutdown_fetcher() -> None:
    try:
        _browser.close()
    except Exception:
        pass
    _stop_playwright()


def _shutdown() -> None:
    shutdown_fetcher()


atexit.register(_shutdown)


def fetch_target_page(
    url: str, *, total_timeout_s: float | None = None
) -> FetchOutcome:
    return _browser.fetch(url, total_timeout_s=total_timeout_s)
