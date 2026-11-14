import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

from app.fetcher import Browser

ARTICLE_HTML = """<!doctype html><html><head><title>Article</title></head><body>
<nav>menu</nav><article><h1>Article title</h1>
<p>This is the first substantive paragraph about the topic and its important details.</p>
<p>This is the second substantive paragraph with additional information for readers.</p>
<h2>Details</h2><p>More body text that should survive extraction in markdown form.</p></article>
<footer>footer</footer></body></html>"""

STATIC_HTML = """<!doctype html><html><head><title>Static Page</title></head>
<body><h1>Static marker</h1></body></html>"""

EMPTY_HTML = """<!doctype html><html><head><title>Empty</title></head><body>
<script>document.body.innerHTML = '';</script></body></html>"""

CHALLENGE_HTML = """<!doctype html><html><head><title>Access Denied</title></head>
<body>Please verify you are human</body></html>"""

SPA_HTML = """<!doctype html><html><head><title>SPA</title></head><body>
<script>setTimeout(() => { document.body.innerHTML =
'<p>rendered by js</p>'; }, 50);</script></body></html>"""

LONGPOLL_HTML = """<!doctype html><html><head><title>Long Poll</title></head><body>
<script>fetch('/hang');</script><p>committed</p></body></html>"""

NEVER_SETTLES_HTML = """<!doctype html><html><head><title>Changing</title></head><body>
<script>let n = 0; setInterval(() => { document.body.innerText = String(++n); }, 30);</script>
</body></html>"""

LARGE_HTML = f"<html><body><article><p>{'x' * 6000}</p></article></body></html>"


class SiteHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/static":
            self._send(STATIC_HTML)
        elif path == "/article":
            self._send(ARTICLE_HTML)
        elif path == "/redirect":
            self._redirect("/article")
        elif path == "/redirect-static":
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
            self._send(LARGE_HTML)
        elif path == "/empty":
            self._send(EMPTY_HTML)
        elif path == "/challenge":
            self._send(CHALLENGE_HTML)
        elif path == "/spa":
            self._send(SPA_HTML)
        elif path == "/longpoll":
            self._send(LONGPOLL_HTML)
        elif path == "/bad-subresource":
            self._send(
                '<html><body><p>ok</p>'
                '<img src="http://169.254.169.254/x"></body></html>'
            )
        elif path == "/failed-subresource":
            self._send(
                '<html><body><p>ok</p>'
                '<img src="http://127.0.0.1:1/x"></body></html>'
            )
        elif path == "/blocked-iframe":
            self._send(
                '<html><body><p>ok</p>'
                '<iframe src="http://169.254.169.254/"></iframe></body></html>'
            )
        elif path == "/never-settles":
            self._send(NEVER_SETTLES_HTML)
        elif path == "/hang":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.flush()
            time.sleep(30)
        elif path == "/hang-before-commit":
            time.sleep(5)
            self._send(STATIC_HTML)
        else:
            self._send(ARTICLE_HTML)

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _send(
        self,
        body: str | bytes,
        *,
        status: int = 200,
        content_type: str = "text/html",
    ) -> None:
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def running_site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), SiteHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def headless_browser(
    profile_dir: Path,
    *,
    max_html_chars: int = 5000,
    nav_timeout_ms: int = 5000,
    settle_timeout_s: float = 2.0,
    total_timeout_s: float = 10.0,
) -> Browser:
    return Browser(
        profile_dir=profile_dir,
        headless=True,
        allow_private=True,
        max_html_chars=max_html_chars,
        nav_timeout_ms=nav_timeout_ms,
        settle_timeout_s=settle_timeout_s,
        total_timeout_s=total_timeout_s,
    )


def read_trace_events(
    db_path: Path, request_id: UUID | str
) -> list[tuple[str, dict[str, Any]]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT event_type, details_json FROM trace_events"
            " WHERE request_id = ? ORDER BY id",
            (str(request_id),),
        ).fetchall()
    return [(str(event), json.loads(str(details))) for event, details in rows]


def remove_sqlite_database(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        path.with_name(f"{path.name}{suffix}").unlink(missing_ok=True)
