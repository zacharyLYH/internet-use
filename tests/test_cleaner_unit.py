from typing import cast

import pytest

from app.cleaner import (
    MESSAGES,
    CleanFailure,
    CleanSuccess,
    clean_to_markdown,
)

URL = "https://example.com/posts/page"

ARTICLE = """<!doctype html>
<html><head><title>Example Article</title><meta name="author" content="Jane"></head>
<body>
<nav>NAVIGATION_MARKER</nav>
<article>
<h1>Main Heading</h1>
<p>First paragraph with <a href="/relative-path">a relative link</a> and <strong>bold text</strong>. Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.</p>
<p>Second paragraph with <a href="/relative-path-2">another link</a>. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat.</p>
<p>Third paragraph. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur.</p>
<p>Fourth paragraph. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.</p>
<p>Fifth paragraph. Sed ut perspiciatis unde omnis iste natus error sit voluptatem accusantium doloremque laudantium.</p>
<p>Sixth paragraph. Totam rem aperiam, eaque ipsa quae ab illo inventore veritatis et quasi architecto beatae vitae dicta sunt explicabo.</p>
<table><tr><th>Name</th><th>Value</th></tr><tr><td>Alpha</td><td>1</td></tr></table>
<p>Unicode: café 漢字.</p>
</article>
<div class="comments"><p>COMMENT_MARKER Lorem ipsum dolor sit amet, consectetur adipiscing elit. This comment is long enough to be extracted when comments are enabled.</p></div>
</body></html>"""

CHALLENGE = """<!doctype html><html><head><title>Access Denied</title></head>
<body><h1>Access Denied</h1><p>Please verify you are human.</p></body></html>"""


def test_extracts_article_content() -> None:
    outcome = clean_to_markdown(ARTICLE, URL)

    assert isinstance(outcome, CleanSuccess)
    assert outcome.extraction_state == "extracted"
    content = outcome.markdown_content
    assert "Main Heading" in content
    assert "First paragraph" in content
    assert "NAVIGATION_MARKER" not in content
    assert "COMMENT_MARKER" not in content
    assert "| Name | Value |" in content
    assert "[a relative link](https://example.com/relative-path)" in content
    assert content == content.strip()
    assert outcome.source_html_bytes == len(ARTICLE.encode("utf-8"))
    assert outcome.markdown_bytes == len(content.encode("utf-8"))


def test_byte_counts_use_utf8() -> None:
    outcome = clean_to_markdown(ARTICLE, URL)

    assert isinstance(outcome, CleanSuccess)
    assert outcome.source_html_bytes > len(ARTICLE)
    assert outcome.source_html_bytes == len(ARTICLE.encode("utf-8"))
    assert outcome.markdown_bytes == len(outcome.markdown_content.encode("utf-8"))


@pytest.mark.parametrize("html", ["", "   ", "\n\t "])
def test_empty_html_returns_empty_success(html: str) -> None:
    outcome = clean_to_markdown(html, URL)

    assert isinstance(outcome, CleanSuccess)
    assert outcome.extraction_state == "empty"
    assert outcome.markdown_content == ""
    assert outcome.markdown_bytes == 0
    assert outcome.source_html_bytes == len(html.encode("utf-8"))


def test_empty_html_skips_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> str:
        raise AssertionError("extract must not run for empty HTML")

    monkeypatch.setattr("app.cleaner.extract", fail)

    outcome = clean_to_markdown(" \n ", URL)

    assert isinstance(outcome, CleanSuccess)
    assert outcome.extraction_state == "empty"


def test_textless_html_returns_empty() -> None:
    html = """<html><head><style>body { color: red; }</style></head>
    <body><script>var x = 1;</script></body></html>"""

    outcome = clean_to_markdown(html, URL)

    assert isinstance(outcome, CleanSuccess)
    assert outcome.extraction_state == "empty"
    assert outcome.markdown_content == ""


def test_challenge_html_is_processed_without_failure() -> None:
    outcome = clean_to_markdown(CHALLENGE, URL)

    assert isinstance(outcome, CleanSuccess)
    assert outcome.extraction_state in ("empty", "extracted")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "example.com",
        "/relative",
        "ftp://example.com",
        "javascript:alert(1)",
        "http://user:pass@example.com",
        "http://example.com:99999",
    ],
)
def test_invalid_urls_return_invalid_input(url: str) -> None:
    outcome = clean_to_markdown("<html><body>text</body></html>", url)

    assert isinstance(outcome, CleanFailure)
    assert outcome.error_code == "invalid_input"
    assert outcome.message == MESSAGES["invalid_input"]


def test_invalid_url_wins_over_empty_html() -> None:
    outcome = clean_to_markdown("", "not a url")

    assert isinstance(outcome, CleanFailure)
    assert outcome.error_code == "invalid_input"


@pytest.mark.parametrize("bad_html", [None, 123, b"<html></html>"])
def test_invalid_html_type_returns_invalid_input(bad_html: object) -> None:
    outcome = clean_to_markdown(cast(str, bad_html), URL)

    assert isinstance(outcome, CleanFailure)
    assert outcome.error_code == "invalid_input"


def test_invalid_url_type_returns_invalid_input() -> None:
    outcome = clean_to_markdown("<html></html>", cast(str, 123))

    assert isinstance(outcome, CleanFailure)
    assert outcome.error_code == "invalid_input"


@pytest.mark.parametrize("result", [None, "", "  \n"])
def test_empty_extraction_result_is_empty(
    monkeypatch: pytest.MonkeyPatch, result: str | None
) -> None:
    def fake_extract(filecontent: str, **kwargs: object) -> str | None:
        return result

    monkeypatch.setattr("app.cleaner.extract", fake_extract)

    outcome = clean_to_markdown("<html><body>text</body></html>", URL)

    assert isinstance(outcome, CleanSuccess)
    assert outcome.extraction_state == "empty"
    assert outcome.markdown_content == ""
    assert outcome.markdown_bytes == 0


def test_extraction_exception_returns_sanitized_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(filecontent: str, **kwargs: object) -> str:
        raise RuntimeError("SECRET_HTML_MARKER")

    monkeypatch.setattr("app.cleaner.extract", fail)

    outcome = clean_to_markdown("<html><body>text</body></html>", URL)

    assert isinstance(outcome, CleanFailure)
    assert outcome.error_code == "extraction_error"
    assert outcome.message == MESSAGES["extraction_error"]
    assert "SECRET_HTML_MARKER" not in outcome.message


def test_uses_configured_extraction_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_extract(filecontent: str, **kwargs: object) -> str:
        captured["filecontent"] = filecontent
        captured.update(kwargs)
        return "# Heading"

    monkeypatch.setattr("app.cleaner.extract", fake_extract)

    outcome = clean_to_markdown("<html><body>text</body></html>", f" {URL} ")

    assert isinstance(outcome, CleanSuccess)
    assert captured == {
        "filecontent": "<html><body>text</body></html>",
        "url": URL,
        "output_format": "markdown",
        "include_comments": False,
        "include_tables": True,
        "include_links": True,
        "include_images": False,
        "include_formatting": True,
        "fast": False,
    }
