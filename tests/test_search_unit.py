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
    client = httpx.Client()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        captured.append({"url": url, **kwargs})
        return response

    monkeypatch.setattr(client, "get", fake_get)
    monkeypatch.setattr("app.search._get_client", lambda: client)
    return captured


def test_sends_trimmed_query_and_json_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = patch_get(monkeypatch, make_response({"results": [{"title": "a"}]}))
    data = fetch_search_response(" python ")

    assert data["results"] == [{"title": "a"}]
    assert captured[0]["url"] == f"{SEARXNG_URL}/search"
    assert captured[0]["params"] == {"q": "python", "format": "json", "pageno": 1}


def test_uses_configured_searxng_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.search.SEARXNG_URL", "http://searx.internal:9999")
    captured = patch_get(monkeypatch, make_response({"results": []}))

    fetch_search_response("q")

    assert captured[0]["url"] == "http://searx.internal:9999/search"


def test_pageno_is_sent_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = patch_get(monkeypatch, make_response({"results": []}))

    fetch_search_response("q", pageno=3)

    assert captured[0]["params"] == {"q": "q", "format": "json", "pageno": 3}


def test_invalid_pageno_raises() -> None:
    with pytest.raises(ValueError):
        fetch_search_response("q", pageno=0)


def test_missing_results_key_is_returned_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_get(monkeypatch, make_response({"answers": ["x"]}))

    assert fetch_search_response("q") == {"answers": ["x"]}


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
