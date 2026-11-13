import os
from typing import Any

import httpx

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:49731").rstrip("/")
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
DEFAULT_PAGENO = 1

_client: httpx.Client | None = None


class SearchError(RuntimeError):
    """Raised when the SearXNG search request fails."""


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(headers=HEADERS, timeout=15, follow_redirects=True)
    return _client


def fetch_search_response(query: str, pageno: int = DEFAULT_PAGENO) -> dict[str, Any]:
    text = query.strip()
    if not text:
        raise ValueError("query must not be empty")
    if pageno < 1:
        raise ValueError("pageno must be >= 1")
    try:
        response = _get_client().get(
            f"{SEARXNG_URL}/search",
            params={"q": text, "format": "json", "pageno": pageno},
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise SearchError(f"search request failed: {exc}") from exc
    except ValueError as exc:
        raise SearchError(f"search response was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SearchError("search response was not a JSON object")
    return data
