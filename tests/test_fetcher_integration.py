import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.fetcher import Browser, FetchFailure, PageSnapshot

STATIC = """<!doctype html><html><head><title>Static Page</title></head>
<body><h1>Static marker</h1></body></html>"""

SPA = """<!doctype html><html><head><title>SPA</title></head><body>
<script>setTimeout(() => { document.body.innerHTML =
'<p>rendered by js</p>'; }, 50);</script></body></html>"""

LONGPOLL = """<!doctype html><html><head><title>Long Poll</title></head><body>
<script>fetch('/hang');</script><p>committed</p></body></html>"""

CHALLENGE = """<!doctype html><html><head><title>Access Denied</title></head>
<body>Please verify you are human</body></html>"""

NEVER_SETTLES = """<!doctype html><html><head><title>Changing</title></head><body>
<script>let n = 0; setInterval(() => { document.body.innerText = String(++n); }, 30);</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/redirect":
            self._redirect("/static")
        elif path == "/redirect-blocked":
            self._redirect("http://169.254.169.254/")
        elif path == "/redirect-loop":
            self._redirect("/redirect-loop")
        elif path == "/missing":
            self._send("<html><body>Not Found</body></html>", status=404)
        elif path == "/pdf":
            self._send(b"%PDF-1.4", content_type="application/pdf")
        elif path == "/large":
            self._send(f"<html><body>{'x' * 2000}</body></html>")
        elif path == "/empty":
            self._send("<html><body></body></html>")
        elif path == "/challenge":
            self._send(CHALLENGE)
        elif path == "/spa":
            self._send(SPA)
        elif path == "/longpoll":
            self._send(LONGPOLL)
        elif path == "/bad-subresource":
            self._send('<html><body><p>ok</p><img src="http://169.254.169.254/x"></body></html>')
        elif path == "/failed-subresource":
            self._send('<html><body><p>ok</p><img src="http://127.0.0.1:1/x"></body></html>')
        elif path == "/blocked-iframe":
            self._send('<html><body><p>ok</p><iframe src="http://169.254.169.254/"></iframe></body></html>')
        elif path == "/never-settles":
            self._send(NEVER_SETTLES)
        elif path == "/hang":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.flush()
            time.sleep(30)
        elif path == "/hang-before-commit":
            time.sleep(5)
            self._send(STATIC)
        else:
            self._send(STATIC)

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _send(
        self, body: str | bytes, *, status: int = 200, content_type: str = "text/html"
    ) -> None:
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture(scope="session")
def site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture(scope="session")
def browser(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Browser]:
    instance = Browser(
        profile_dir=tmp_path_factory.mktemp("chrome-profile"),
        headless=True,
        allow_private=True,
        settle_timeout_s=2.0,
    )
    try:
        yield instance
    finally:
        instance.close()


def test_static_page(browser: Browser, site: str) -> None:
    outcome = browser.fetch(f"{site}/static")

    assert isinstance(outcome, PageSnapshot)
    assert outcome.render_state == "complete"
    assert outcome.status_code == 200
    assert outcome.title == "Static Page"
    assert "Static marker" in outcome.raw_page_html


def test_client_rendered_content(browser: Browser, site: str) -> None:
    outcome = browser.fetch(f"{site}/spa")

    assert isinstance(outcome, PageSnapshot)
    assert "rendered by js" in outcome.raw_page_html


def test_redirect_reports_both_urls(browser: Browser, site: str) -> None:
    outcome = browser.fetch(f"{site}/redirect")

    assert isinstance(outcome, PageSnapshot)
    assert outcome.requested_url == f"{site}/redirect"
    assert outcome.final_url == f"{site}/static"


def test_http_error_returns_snapshot(browser: Browser, site: str) -> None:
    outcome = browser.fetch(f"{site}/missing")

    assert isinstance(outcome, PageSnapshot)
    assert outcome.status_code == 404
    assert "Not Found" in outcome.raw_page_html


def test_non_html_is_rejected(browser: Browser, site: str) -> None:
    outcome = browser.fetch(f"{site}/pdf")

    assert isinstance(outcome, FetchFailure)
    assert outcome.error_code == "unsupported_content_type"


def test_empty_render_is_partial(browser: Browser, site: str) -> None:
    outcome = browser.fetch(f"{site}/empty")

    assert isinstance(outcome, PageSnapshot)
    assert outcome.render_state == "partial"
    assert "empty_render" in outcome.warnings


def test_challenge_is_warned(browser: Browser, site: str) -> None:
    outcome = browser.fetch(f"{site}/challenge")

    assert isinstance(outcome, PageSnapshot)
    assert "possible_challenge" in outcome.warnings


def test_long_polling_does_not_block(browser: Browser, site: str) -> None:
    started = time.monotonic()
    outcome = browser.fetch(f"{site}/longpoll")

    assert isinstance(outcome, PageSnapshot)
    assert outcome.render_state == "complete"
    assert time.monotonic() - started < 5


def test_blocked_subresource_is_warned(browser: Browser, site: str) -> None:
    outcome = browser.fetch(f"{site}/bad-subresource")

    assert isinstance(outcome, PageSnapshot)
    assert "blocked_destination" in outcome.warnings


def test_failed_subresource_is_warned(browser: Browser, site: str) -> None:
    outcome = browser.fetch(f"{site}/failed-subresource")

    assert isinstance(outcome, PageSnapshot)
    assert "resource_failures" in outcome.warnings


def test_blocked_iframe_keeps_page(browser: Browser, site: str) -> None:
    outcome = browser.fetch(f"{site}/blocked-iframe")

    assert isinstance(outcome, PageSnapshot)
    assert "blocked_destination" in outcome.warnings


def test_blocked_redirect_fails(browser: Browser, site: str) -> None:
    outcome = browser.fetch(f"{site}/redirect-blocked")

    assert isinstance(outcome, FetchFailure)
    assert outcome.error_code == "blocked_destination"


def test_redirect_loop_is_reported(browser: Browser, site: str) -> None:
    outcome = browser.fetch(f"{site}/redirect-loop")

    assert isinstance(outcome, FetchFailure)
    assert outcome.error_code == "redirect_error"


def test_connection_refused_is_network_error(browser: Browser) -> None:
    outcome = browser.fetch("http://127.0.0.1:1/")

    assert isinstance(outcome, FetchFailure)
    assert outcome.error_code == "network_error"


def test_never_settling_page_times_out(site: str, tmp_path: Path) -> None:
    browser = Browser(
        profile_dir=tmp_path / "profile",
        headless=True,
        allow_private=True,
        settle_timeout_s=0.4,
    )
    try:
        outcome = browser.fetch(f"{site}/never-settles")
    finally:
        browser.close()

    assert isinstance(outcome, PageSnapshot)
    assert outcome.render_state == "partial"
    assert "render_timeout" in outcome.warnings


def test_oversized_response_is_rejected(site: str, tmp_path: Path) -> None:
    browser = Browser(
        profile_dir=tmp_path / "profile",
        headless=True,
        allow_private=True,
        max_html_chars=200,
    )
    try:
        outcome = browser.fetch(f"{site}/large")
    finally:
        browser.close()

    assert isinstance(outcome, FetchFailure)
    assert outcome.error_code == "response_too_large"


def test_navigation_timeout_before_commit(site: str, tmp_path: Path) -> None:
    browser = Browser(
        profile_dir=tmp_path / "profile",
        headless=True,
        allow_private=True,
        nav_timeout_ms=300,
    )
    try:
        outcome = browser.fetch(f"{site}/hang-before-commit")
    finally:
        browser.close()

    assert isinstance(outcome, FetchFailure)
    assert outcome.error_code == "navigation_timeout"
