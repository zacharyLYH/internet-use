import os
from typing import Any

import httpx

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:49731").rstrip("/")
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
DEFAULT_PAGENO = 1
DEFAULT_TIMEOUT_S = 15.0

_client: httpx.Client | None = None


class SearchError(RuntimeError):
    """Raised when the SearXNG search request fails."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(headers=HEADERS, timeout=15, follow_redirects=True)
    return _client


def fetch_search_response(
    query: str,
    pageno: int = DEFAULT_PAGENO,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    text = query.strip()
    if not text:
        raise ValueError("query must not be empty")
    if pageno < 1:
        raise ValueError("pageno must be >= 1")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be > 0")
    try:
        response = _get_client().get(
            f"{SEARXNG_URL}/search",
            params={"q": text, "format": "json", "pageno": pageno},
            timeout=timeout_s,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise SearchError(
            "search request failed", retryable=status == 429 or status >= 500
        ) from exc
    except httpx.HTTPError as exc:
        raise SearchError("search request failed") from exc
    except ValueError as exc:
        raise SearchError("search response was not valid JSON", retryable=False) from exc
    if not isinstance(data, dict):
        raise SearchError(
            "search response was not a JSON object", retryable=False
        )
    return data
