import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.fetcher import Browser, FetchFailure, PageSnapshot
from tests.test_utils import headless_browser


@pytest.fixture(scope="session")
def browser(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Browser]:
    instance = headless_browser(tmp_path_factory.mktemp("chrome-profile"))
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
    outcome = browser.fetch(f"{site}/redirect-static")

    assert isinstance(outcome, PageSnapshot)
    assert outcome.requested_url == f"{site}/redirect-static"
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


def test_per_request_total_timeout_shortens_navigation(
    site: str, tmp_path: Path
) -> None:
    browser = Browser(
        profile_dir=tmp_path / "profile",
        headless=True,
        allow_private=True,
        total_timeout_s=5.0,
        settle_timeout_s=0.4,
    )
    try:
        warmup = browser.fetch(f"{site}/static")
        assert isinstance(warmup, PageSnapshot)
        started = time.monotonic()
        outcome = browser.fetch(
            f"{site}/hang-before-commit", total_timeout_s=0.3
        )
        elapsed = time.monotonic() - started
    finally:
        browser.close()

    assert isinstance(outcome, FetchFailure)
    assert outcome.error_code == "navigation_timeout"
    assert elapsed < 2
