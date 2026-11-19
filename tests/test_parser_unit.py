import pytest

from app.parser import parse_search_response


def result(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "title": "Title",
        "url": "https://example.com/page",
        "content": "Snippet",
        "engines": ["google"],
        "category": "general",
        "score": 1.5,
        "publishedDate": "2026-01-01",
    }
    base.update(overrides)
    return base


def test_maps_fields() -> None:
    assert parse_search_response({"results": [result()]}) == [
        {
            "title": "Title",
            "url": "https://example.com/page",
            "snippet": "Snippet",
            "engines": ["google"],
            "category": "general",
            "score": 1.5,
            "published": "2026-01-01",
        }
    ]


def test_dedupes_normalized_urls() -> None:
    hits = parse_search_response(
        {
            "results": [
                result(url="https://Example.com/a#one"),
                result(url="https://example.com/a#two"),
            ]
        }
    )

    assert len(hits) == 1
    assert hits[0]["url"] == "https://Example.com/a#one"


def test_dedupes_trailing_slash_variants() -> None:
    hits = parse_search_response(
        {
            "results": [
                result(url="https://example.com/a/"),
                result(url="https://example.com/a"),
            ]
        }
    )

    assert len(hits) == 1


def test_malformed_urls_are_skipped_not_raised() -> None:
    hits = parse_search_response(
        {
            "results": [
                result(url="http://[bad"),
                result(url="http://example.com:99999/x"),
                result(url="https://ok.com/x"),
            ]
        }
    )

    assert [h["url"] for h in hits] == ["https://ok.com/x"]


def test_skips_blank_and_non_http_entries() -> None:
    hits = parse_search_response(
        {
            "results": [
                result(title=""),
                result(url=""),
                result(url="ftp://example.com/x"),
                result(url="/relative"),
                "not a mapping",
            ]
        }
    )

    assert hits == []


def test_missing_results_is_empty() -> None:
    assert parse_search_response({}) == []


def test_non_list_results_raises() -> None:
    with pytest.raises(ValueError):
        parse_search_response({"results": {}})


def test_fallbacks_for_sparse_entries() -> None:
    hits = parse_search_response(
        {
            "results": [
                result(
                    engines=None,
                    engine="brave",
                    publishedDate=None,
                    pubdate="2025-01-01",
                    content=None,
                    score=None,
                    category=None,
                )
            ]
        }
    )

    assert hits[0]["engines"] == ["brave"]
    assert hits[0]["published"] == "2025-01-01"
    assert hits[0]["snippet"] == ""
    assert hits[0]["score"] == 0.0
    assert hits[0]["category"] == ""


def test_does_not_mutate_input() -> None:
    raw = {"results": [result()]}
    parse_search_response(raw)

    assert raw == {"results": [result()]}
