from app.parser import parse_search_response
from app.search import fetch_search_response


def test_live_search_returns_results() -> None:
    response = fetch_search_response("python programming language")
    hits = parse_search_response(response)

    assert hits
    assert all(h["title"] and h["url"] for h in hits)
