import httpx
import pytest

from app.search import SEARXNG_URL, SearchError, fetch_search_response


def make_response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", f"{SEARXNG_URL}/search"),
    )


def patch_get(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> list[dict[str, object]]:
    captured: list[dict[str, object]] = []

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        captured.append({"url": url, **kwargs})
        return response

    monkeypatch.setattr("app.search.httpx.get", fake_get)
    return captured


def test_sends_trimmed_query_and_json_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = patch_get(monkeypatch, make_response({"results": [{"title": "a"}]}))
    data = fetch_search_response(" python ")

    assert data["results"] == [{"title": "a"}]
    assert captured[0]["url"] == f"{SEARXNG_URL}/search"
    assert captured[0]["params"] == {"q": "python", "format": "json"}


def test_uses_configured_searxng_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.search.SEARXNG_URL", "http://searx.internal:9999")
    captured = patch_get(monkeypatch, make_response({"results": []}))

    fetch_search_response("q")

    assert captured[0]["url"] == "http://searx.internal:9999/search"


def test_max_results_trims_results(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_get(monkeypatch, make_response({"results": [1, 2, 3]}))

    assert fetch_search_response("q", max_results=2)["results"] == [1, 2]


def test_max_results_larger_than_results_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_get(monkeypatch, make_response({"results": [1, 2]}))

    assert fetch_search_response("q", max_results=10)["results"] == [1, 2]


def test_missing_results_key_is_returned_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_get(monkeypatch, make_response({"answers": ["x"]}))

    assert fetch_search_response("q", max_results=2) == {"answers": ["x"]}


def test_empty_query_raises() -> None:
    with pytest.raises(ValueError):
        fetch_search_response("   ")


def test_http_failure_raises_search_error(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_get(
        monkeypatch,
        httpx.Response(502, request=httpx.Request("GET", SEARXNG_URL)),
    )

    with pytest.raises(SearchError):
        fetch_search_response("anything")


def test_invalid_json_raises_search_error(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_get(
        monkeypatch,
        httpx.Response(
            200, text="<html>error</html>", request=httpx.Request("GET", SEARXNG_URL)
        ),
    )

    with pytest.raises(SearchError):
        fetch_search_response("anything")


def test_non_object_json_raises_search_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_get(monkeypatch, make_response([]))

    with pytest.raises(SearchError):
        fetch_search_response("anything")
