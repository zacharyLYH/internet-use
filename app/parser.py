from collections.abc import Mapping
from typing import Any, TypedDict

from app.urls import canonical_url, parse_http_url


class SearchHit(TypedDict):
    title: str
    url: str
    snippet: str
    engines: list[str]
    category: str
    score: float
    published: str | None


def parse_search_response(search_response: Mapping[str, Any]) -> list[SearchHit]:
    results = search_response.get("results", [])
    if not isinstance(results, list):
        raise ValueError("search response 'results' must be a list")

    hits: list[SearchHit] = []
    seen: set[str] = set()

    for item in results:
        if not isinstance(item, Mapping):
            continue

        title = _text(item.get("title"))
        url = _text(item.get("url"))
        parsed = parse_http_url(url)
        if not title or parsed is None or not parsed.hostname:
            continue

        key = canonical_url(parsed)
        if key in seen:
            continue
        seen.add(key)

        published = _text(item.get("publishedDate")) or _text(item.get("pubdate"))
        hits.append(
            {
                "title": title,
                "url": url,
                "snippet": _text(item.get("content")),
                "engines": _engines(item),
                "category": _text(item.get("category")),
                "score": _float(item.get("score")),
                "published": published or None,
            }
        )

    return hits


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _float(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _engines(item: Mapping[str, Any]) -> list[str]:
    engines = item.get("engines")
    if isinstance(engines, list):
        return [e for e in engines if isinstance(e, str)]
    engine = item.get("engine")
    return [engine] if isinstance(engine, str) else []
