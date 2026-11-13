from collections.abc import Mapping, Sequence
from typing import Any, TypedDict
from urllib.parse import SplitResult, urlsplit, urlunsplit


class SearchHit(TypedDict):
    title: str
    url: str
    snippet: str
    engines: list[str]
    category: str
    score: float
    published: str | None


def parse_search_response(
    search_response: Mapping[str, Any],
    max_results: int | None = None,
    domain_denylist: Sequence[str] = (),
) -> list[SearchHit]:
    if max_results is not None and max_results < 0:
        raise ValueError("max_results must not be negative")
    results = search_response.get("results", [])
    if not isinstance(results, list):
        raise ValueError("search response 'results' must be a list")

    denied = {_domain(d) for d in domain_denylist}
    denied.discard("")
    hits: list[SearchHit] = []
    seen: set[str] = set()

    for item in results:
        if max_results == 0:
            break
        if not isinstance(item, Mapping):
            continue

        title = _text(item.get("title"))
        url = _text(item.get("url"))
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if not title or not url or parsed.scheme not in ("http", "https") or not host:
            continue
        if _blocked(host, denied):
            continue

        key = _canonical(parsed)
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
        if max_results is not None and len(hits) >= max_results:
            break

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


def _domain(value: str) -> str:
    return value.strip().lower().lstrip(".")


def _blocked(host: str, denied: set[str]) -> bool:
    return any(host == d or host.endswith(f".{d}") for d in denied)


def _canonical(parsed: SplitResult) -> str:
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, "")
    )
