from typing import cast

import pytest
from trafilatura.deduplication import LRUCache

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

LINKY = """<!doctype html>
<html><head><title>Linky</title></head><body>
<nav><a href="/login">Log in</a> <a href="/">Home</a></nav>
<article>
<h1>Linky Heading</h1>
<p>Paragraph one cites the <a href="https://kafka.apache.org/documentation/#retention">official Kafka docs</a> for details about retention and cleanup policies inside the cluster.</p>
<p>Paragraph two asks you to <a href="/login">log in</a> before commenting, to read <a href="/read-more">Read more</a> about the topic, and to <a href="https://facebook.com/share?u=x">Share on Facebook</a> with friends and colleagues.</p>
<p>Paragraph three repeats the <a href="https://kafka.apache.org/documentation/#retention">official Kafka docs</a> pointer and adds ordinary prose about brokers, partitions, and consumers for extra length.</p>
</article>
<footer><a href="/privacy">Privacy</a></footer>
</body></html>"""


def test_extracts_article_content() -> None:
    outcome = clean_to_markdown(ARTICLE, URL)

    assert isinstance(outcome, CleanSuccess)
    assert outcome.extraction_state == "extracted"
    content = outcome.markdown_content
    assert "Main Heading" in content
    assert "First paragraph" in content
    assert "NAVIGATION_MARKER" not in content
    assert "COMMENT_MARKER" not in content
    assert "a relative link" in content
    assert "[a relative link](https://example.com/relative-path)" in content
    assert "| Name | Value |" not in content
    assert content == content.strip()
    assert outcome.source_html_bytes == len(ARTICLE.encode("utf-8"))
    assert outcome.markdown_bytes == len(content.encode("utf-8"))


def test_keeps_useful_links_and_unwraps_noisy_ones() -> None:
    outcome = clean_to_markdown(LINKY, URL)

    assert isinstance(outcome, CleanSuccess)
    assert outcome.extraction_state == "extracted"
    content = outcome.markdown_content
    assert (
        "[official Kafka docs](https://kafka.apache.org/documentation/#retention)"
        in content
    )
    assert content.count("](https://kafka.apache.org/documentation/#retention)") == 1
    assert content.count("official Kafka docs") == 2
    assert "asks you to log in before commenting" in content
    assert content.count("](https://kafka.apache.org") == 1
    assert "(https://example.com/login)" not in content
    assert "(https://example.com/read-more)" not in content
    assert "(https://facebook.com" not in content
    assert "  " not in content


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
    assert isinstance(captured.pop("deduplicate", None), LRUCache)
    assert captured == {
        "filecontent": "<html><body>text</body></html>",
        "url": URL,
        "output_format": "markdown",
        "include_comments": False,
        "include_tables": False,
        "include_links": True,
        "include_images": False,
        "include_formatting": True,
        "favor_precision": True,
        "fast": False,
    }


def test_repeated_extraction_keeps_content() -> None:
    first = clean_to_markdown(ARTICLE, URL)
    second = clean_to_markdown(ARTICLE, URL)

    assert isinstance(first, CleanSuccess)
    assert isinstance(second, CleanSuccess)
    assert first.markdown_content == second.markdown_content
    assert "Main Heading" in second.markdown_content


def test_postprocess_strips_citation_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (
        "Heading[1]\n\n\n\n"
        "Fact with note[ 12 ] and <sup class=\"reference\">[13]</sup> text. "
        "The paragraph continues with ordinary prose so it looks like a real body. "
        "History [edit] section is mentioned inside this paragraph as well. "
        "Another sentence follows here to give the paragraph enough length. "
        "Closing sentence of the first paragraph mentions the topic once more.\n\n\n\n"
        "Filler paragraph keeps the document above the minimum size and reads as prose. "
        "It has several sentences so that it is clearly a paragraph, not a label. "
        "This sentence adds more words to the paragraph to make it realistic. "
        "Final sentence of the second paragraph wraps up the discussion of the topic.\n\n"
        "Tail paragraph."
    )

    monkeypatch.setattr(
        "app.cleaner.extract", lambda filecontent, **kwargs: raw
    )

    outcome = clean_to_markdown("<html><body>text</body></html>", URL)

    assert isinstance(outcome, CleanSuccess)
    content = outcome.markdown_content
    assert content.startswith("Heading\n\nFact with note and text.")
    assert "History section is mentioned" in content
    assert content.endswith("Tail paragraph.")
    assert "[1]" not in content
    assert "[ 12 ]" not in content
    assert "<sup" not in content
    assert "[edit]" not in content
    assert "\n\n\n" not in content


def test_tiny_content_is_treated_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.cleaner.extract", lambda filecontent, **kwargs: "Loading..."
    )

    outcome = clean_to_markdown("<html><body>text</body></html>", URL)

    assert isinstance(outcome, CleanSuccess)
    assert outcome.extraction_state == "empty"
    assert outcome.markdown_content == ""
    assert outcome.markdown_bytes == 0


def test_ui_chrome_page_is_treated_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chrome = "\n\n".join(
        [
            "## In this video",
            "Timeline",
            "Chapters",
            "Transcript",
            "## The Entire Life of Napoleon in 82 Minutes",
            "48KLikes",
            "2.4MViews",
            "2025Sep 17",
            "Follow along using the transcript.",
            "Show transcript",
            "### Maximus",
            "93.6K subscribers",
            "## Video details",
            "Date",
            "Views",
            "Likes",
            "## Transcript",
            "NaN / NaN",
            "Show more",
        ]
    )

    monkeypatch.setattr(
        "app.cleaner.extract", lambda filecontent, **kwargs: chrome
    )

    outcome = clean_to_markdown("<html><body>text</body></html>", URL)

    assert isinstance(outcome, CleanSuccess)
    assert outcome.extraction_state == "empty"
    assert outcome.markdown_content == ""


def test_markdown_is_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.cleaner.extract", lambda filecontent, **kwargs: "x" * 30_000
    )

    outcome = clean_to_markdown("<html><body>text</body></html>", URL)

    assert isinstance(outcome, CleanSuccess)
    assert len(outcome.markdown_content) == 20_000
    assert outcome.markdown_bytes == 20_000


def test_cap_keeps_paragraph_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "a" * 19_000 + "\n\n" + "b" * 5_000

    monkeypatch.setattr(
        "app.cleaner.extract", lambda filecontent, **kwargs: raw
    )

    outcome = clean_to_markdown("<html><body>text</body></html>", URL)

    assert isinstance(outcome, CleanSuccess)
    assert outcome.markdown_content == "a" * 19_000


def test_postprocess_drops_footnote_links(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = (
        "The stapler was invented in the nineteenth century"
        ".[\\[1\\]](https://en.wikipedia.org#cite_note-1) "
        "Its modern form dates to the twentieth"
        ".[\\[2\\]](https://en.wikipedia.org#cite_note-2) "
        "Collectors still value early models made before the first world war"
        " for their brass fittings and wooden bases"
        ".[\\[3\\]](https://en.wikipedia.org#cite_note-3) "
        "This paragraph is deliberately long enough to pass the minimum"
        " content check that the cleaner applies before returning text."
        " The archive index (https://en.wikipedia.org#cite_ref-office_4-1)"
        " lists the original models."
    )

    monkeypatch.setattr(
        "app.cleaner.extract", lambda filecontent, **kwargs: raw
    )

    outcome = clean_to_markdown("<html><body>text</body></html>", URL)

    assert isinstance(outcome, CleanSuccess)
    content = outcome.markdown_content
    assert "cite_note" not in content
    assert "#" not in content
    assert "](" not in content
    assert content.startswith("The stapler was invented")
    assert "Its modern form dates" in content
    assert "#cite_ref" not in content
    assert "The archive index lists the original models." in content
    assert content.endswith("The archive index lists the original models.")


def test_postprocess_strips_trailing_reference_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (
        "Body paragraph with real content. " * 10
        + "\n\n1. ↑ Ref one. Retrieved 2020-01-01.\n"
        "2. Jump up to: 1 2 Ref two. ISBN 978-1.\n"
    )

    monkeypatch.setattr(
        "app.cleaner.extract", lambda filecontent, **kwargs: raw
    )

    outcome = clean_to_markdown("<html><body>text</body></html>", URL)

    assert isinstance(outcome, CleanSuccess)
    assert "Body paragraph with real content." in outcome.markdown_content
    assert "Ref one" not in outcome.markdown_content
    assert "ISBN" not in outcome.markdown_content
