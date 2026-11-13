from app.search import fetch_search_response


def test_live_search_returns_results() -> None:
    data = fetch_search_response("python programming language", max_results=3)
    results = data["results"]

    assert isinstance(results, list)
    assert results
    assert len(results) <= 3
    assert {"title", "url"} <= results[0].keys()
