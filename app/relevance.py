import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.parser import SearchHit
from app.urls import url_key

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
DEFAULT_TIMEOUT_S = 10.0
MAX_SNIPPET_CHARS = 400
MAX_TOKENS = 1500
MAX_CANDIDATES = 20

RelevanceErrorCode = Literal[
    "llm_not_configured", "llm_request_failed", "llm_invalid_response"
]

SYSTEM_PROMPT = (
    "You filter web search results for relevance. You receive a query and a "
    "numbered list of results, each with a title, URL, and snippet. Return the "
    "exact URLs of the results that are plausibly relevant to the query, copied "
    "verbatim from the list. Leave out ads, spam, login or paywall pages, "
    "aggregator listings, unrelated topics, and results whose title and snippet "
    "do not match the query. Never invent URLs. If no result is relevant, "
    "return an empty list."
)

RELEVANCE_SCHEMA: dict[str, Any] = {
    "name": "relevant_links",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "keep": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exact URLs of the relevant results.",
            }
        },
        "required": ["keep"],
        "additionalProperties": False,
    },
}

logger = logging.getLogger(__name__)

_client: httpx.Client | None = None


@dataclass(frozen=True, slots=True)
class RelevanceSelection:
    keep: frozenset[str]


@dataclass(frozen=True, slots=True)
class RelevanceFailure:
    error_code: RelevanceErrorCode


RelevanceOutcome = RelevanceSelection | RelevanceFailure


def is_configured() -> bool:
    """True when an endpoint, a model, and an API key are all present."""
    return bool(LLM_BASE_URL and LLM_MODEL and LLM_API_KEY)


def select_relevant_hits(
    query: str,
    hits: Sequence[SearchHit],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> RelevanceOutcome:
    text = query.strip()
    candidates = list(hits)[:MAX_CANDIDATES]
    if not text:
        raise ValueError("query must not be empty")
    if not candidates:
        raise ValueError("hits must not be empty")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be > 0")
    if not LLM_API_KEY:
        logger.warning("relevance filter enabled without LLM_API_KEY")
        return RelevanceFailure("llm_not_configured")

    started = time.monotonic()
    try:
        content = _post_chat(text, candidates, timeout_s=timeout_s)
        keep = _match_urls(content, candidates)
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "relevance request failed status=%s", exc.response.status_code
        )
        return RelevanceFailure("llm_request_failed")
    except httpx.HTTPError as exc:
        logger.warning("relevance request error=%s", type(exc).__name__)
        return RelevanceFailure("llm_request_failed")
    except ValueError as exc:
        logger.warning("relevance response invalid reason=%s", exc)
        return RelevanceFailure("llm_invalid_response")

    logger.info(
        "relevance candidate_count=%d kept=%d dropped=%d duration_ms=%d",
        len(candidates),
        len(keep),
        len(candidates) - len(keep),
        int((time.monotonic() - started) * 1000),
    )
    return RelevanceSelection(frozenset(keep))


def _post_chat(
    query: str, hits: Sequence[SearchHit], *, timeout_s: float
) -> str:
    body: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(query, hits)},
        ],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
    }
    structured: dict[str, Any] = {
        "type": "json_schema",
        "json_schema": RELEVANCE_SCHEMA,
    }
    try:
        data = _json_body(_post(body | {"response_format": structured}, timeout_s))
        response_format: dict[str, Any] = structured
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (400, 404, 422):
            raise
        logger.warning(
            "structured output unsupported status=%s, falling back to json_object",
            exc.response.status_code,
        )
        response_format = {"type": "json_object"}
        data = _json_body(
            _post(body | {"response_format": response_format}, timeout_s)
        )

    content = _message_content(data)
    if content.strip():
        return content

    # Reasoning models can spend the whole budget thinking before answering.
    logger.warning("empty relevance reply, retrying without max_tokens")
    retry = {key: value for key, value in body.items() if key != "max_tokens"}
    data = _json_body(
        _post(retry | {"response_format": response_format}, timeout_s)
    )
    content = _message_content(data)
    if not content.strip():
        raise ValueError("message content was empty")
    return content


def _post(body: dict[str, Any], timeout_s: float) -> httpx.Response:
    response = _get_client().post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json=body,
        timeout=timeout_s,
    )
    response.raise_for_status()
    return response


def _json_body(response: httpx.Response) -> Mapping[str, Any]:
    data = response.json()
    if not isinstance(data, Mapping):
        raise ValueError("response was not a JSON object")
    return data


def _message_content(data: Mapping[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response had no choices")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise ValueError("choice was not an object")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ValueError("choice had no message")
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _match_urls(content: str, candidates: Sequence[SearchHit]) -> set[str]:
    payload = _json_object(content)
    values = payload.get("keep")
    if not isinstance(values, list):
        raise ValueError("response had no keep list")

    by_key: dict[str, str] = {}
    for hit in candidates:
        key = url_key(hit["url"])
        if key is not None:
            by_key.setdefault(key, hit["url"])

    keep: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("keep entries must be URLs")
        key = url_key(value)
        matched = None if key is None else by_key.get(key)
        if matched is not None:
            keep.add(matched)
    if values and not keep:
        raise ValueError("no returned URL matched a candidate")
    return keep


def _json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text[:4].lower() == "json":
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("response contained no JSON object")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("response JSON was not an object")
    return payload


def _user_prompt(query: str, hits: Sequence[SearchHit]) -> str:
    lines = [f"Query: {query}", "", "Results:"]
    for index, hit in enumerate(hits, start=1):
        lines.append(f"{index}. title: {_clip(hit['title'])}")
        lines.append(f"   url: {hit['url']}")
        snippet = _clip(hit["snippet"])
        if snippet:
            lines.append(f"   snippet: {snippet}")
    return "\n".join(lines)


def _clip(text: str) -> str:
    return " ".join(text.split())[:MAX_SNIPPET_CHARS]


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=DEFAULT_TIMEOUT_S, follow_redirects=True)
    return _client



