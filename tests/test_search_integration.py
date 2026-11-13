from app.parser import parse_search_response
from app.search import fetch_search_response


def test_live_search_returns_results() -> None:
    response = fetch_search_response("python programming language")
    hits = parse_search_response(
        response, max_results=3, domain_denylist=["wikipedia.org"]
    )

    assert hits
    assert len(hits) <= 3
    assert all(h["title"] and h["url"] for h in hits)
    assert not any("wikipedia.org" in h["url"] for h in hits)
