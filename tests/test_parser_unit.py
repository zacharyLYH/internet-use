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


def test_denylist_blocks_domain_and_subdomains() -> None:
    hits = parse_search_response(
        {
            "results": [
                result(url="https://pinterest.com/a"),
                result(url="https://www.pinterest.com/b"),
                result(url="https://notpinterest.com/c"),
            ]
        },
        domain_denylist=["Pinterest.com"],
    )

    assert [h["url"] for h in hits] == ["https://notpinterest.com/c"]


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


def test_limit_applies_after_filtering() -> None:
    hits = parse_search_response(
        {
            "results": [
                result(url="https://a.com/1"),
                result(url="https://b.com/1"),
                result(url="https://c.com/1"),
            ]
        },
        max_results=2,
        domain_denylist=["a.com"],
    )

    assert [h["url"] for h in hits] == ["https://b.com/1", "https://c.com/1"]


def test_zero_limit_returns_empty() -> None:
    assert parse_search_response({"results": [result()]}, max_results=0) == []


def test_negative_limit_raises() -> None:
    with pytest.raises(ValueError):
        parse_search_response({"results": [result()]}, max_results=-1)


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
    parse_search_response(raw, max_results=1)

    assert raw == {"results": [result()]}
