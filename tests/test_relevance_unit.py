from typing import Any, cast

import httpx
import pytest

from app.parser import SearchHit
from app.relevance import (
    LLM_BASE_URL,
    LLM_MODEL,
    RelevanceFailure,
    RelevanceSelection,
    select_relevant_hits,
)


def hit(
    url: str, *, title: str = "Title", snippet: str = "Snippet"
) -> SearchHit:
    return SearchHit(
        title=title,
        url=url,
        snippet=snippet,
        engines=["test"],
        category="general",
        score=1.0,
        published=None,
    )


def request() -> httpx.Request:
    return httpx.Request("POST", f"{LLM_BASE_URL}/chat/completions")


def chat_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
        request=request(),
    )


def patch_post(
    monkeypatch: pytest.MonkeyPatch, *responses: httpx.Response
) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    client = httpx.Client()
    queue = list(responses)

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        captured.append({"url": url, **kwargs})
        return queue.pop(0)

    monkeypatch.setattr(client, "post", fake_post)
    monkeypatch.setattr("app.relevance._get_client", lambda: client)
    monkeypatch.setattr("app.relevance.LLM_API_KEY", "test-key")
    return captured


def payload(captured: list[dict[str, Any]], index: int = 0) -> dict[str, Any]:
    return cast(dict[str, Any], captured[index]["json"])


def test_requests_structured_url_list(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = patch_post(
        monkeypatch,
        chat_response('{"keep": ["https://a.com/1", "https://b.com/2"]}'),
    )

    select_relevant_hits(
        "python asyncio", [hit("https://a.com/1"), hit("https://b.com/2")]
    )

    assert captured[0]["url"] == f"{LLM_BASE_URL}/chat/completions"
    assert captured[0]["headers"] == {"Authorization": "Bearer test-key"}
    body = payload(captured)
    assert body["model"] == LLM_MODEL
    assert body["temperature"] == 0
    assert body["response_format"]["type"] == "json_schema"
    schema = body["response_format"]["json_schema"]
    assert schema["name"] == "relevant_links"
    assert schema["strict"] is True
    keep_schema = schema["schema"]["properties"]["keep"]
    assert keep_schema["type"] == "array"
    assert keep_schema["items"] == {"type": "string"}
    assert keep_schema["description"] == "Exact URLs of the relevant results."
    assert schema["schema"]["required"] == ["keep"]
    assert [message["role"] for message in body["messages"]] == [
        "system",
        "user",
    ]


def test_prompt_numbers_results_and_includes_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = patch_post(
        monkeypatch, chat_response('{"keep": ["https://a.com/1"]}')
    )

    select_relevant_hits(
        "kafka",
        [
            hit("https://a.com/1", title="A", snippet="snippet one"),
            hit("https://b.com/2", title="B", snippet="snippet two"),
        ],
    )

    prompt = payload(captured)["messages"][1]["content"]
    assert prompt.startswith("Query: kafka")
    assert "1. title: A" in prompt
    assert "https://a.com/1" in prompt
    assert "snippet one" in prompt
    assert "2. title: B" in prompt


def test_returns_the_candidate_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_post(
        monkeypatch,
        chat_response('{"keep": ["https://a.com/1", "https://c.com/3"]}'),
    )

    outcome = select_relevant_hits(
        "q",
        [
            hit("https://a.com/1"),
            hit("https://b.com/2"),
            hit("https://c.com/3"),
        ],
    )

    assert outcome == RelevanceSelection(
        frozenset({"https://a.com/1", "https://c.com/3"})
    )


def test_url_matching_ignores_case_and_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_post(monkeypatch, chat_response('{"keep": ["https://A.com/1/"]}'))

    outcome = select_relevant_hits(
        "q", [hit("https://a.com/1"), hit("https://b.com/2")]
    )

    assert outcome == RelevanceSelection(frozenset({"https://a.com/1"}))


def test_url_matching_tolerates_trailing_punctuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_post(monkeypatch, chat_response('{"keep": ["https://b.com/2."]}'))

    outcome = select_relevant_hits(
        "q", [hit("https://a.com/1"), hit("https://b.com/2")]
    )

    assert outcome == RelevanceSelection(frozenset({"https://b.com/2"}))


def test_unknown_urls_in_the_reply_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_post(
        monkeypatch,
        chat_response('{"keep": ["https://a.com/1", "https://invented.com/x"]}'),
    )

    outcome = select_relevant_hits(
        "q", [hit("https://a.com/1"), hit("https://b.com/2")]
    )

    assert outcome == RelevanceSelection(frozenset({"https://a.com/1"}))


def test_reply_with_no_matching_url_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_post(monkeypatch, chat_response('{"keep": ["https://invented.com/x"]}'))

    outcome = select_relevant_hits("q", [hit("https://a.com/1")])

    assert isinstance(outcome, RelevanceFailure)
    assert outcome.error_code == "llm_invalid_response"


def test_empty_keep_list_drops_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_post(monkeypatch, chat_response('{"keep": []}'))

    outcome = select_relevant_hits("q", [hit("https://a.com/1")])

    assert outcome == RelevanceSelection(frozenset())


def test_fenced_json_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_post(monkeypatch, chat_response('```json\n{"keep": ["https://b.com/2"]}\n```'))

    outcome = select_relevant_hits(
        "q", [hit("https://a.com/1"), hit("https://b.com/2")]
    )

    assert outcome == RelevanceSelection(frozenset({"https://b.com/2"}))


def test_non_string_entries_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_post(monkeypatch, chat_response('{"keep": [1]}'))

    outcome = select_relevant_hits("q", [hit("https://a.com/1")])

    assert isinstance(outcome, RelevanceFailure)
    assert outcome.error_code == "llm_invalid_response"


def test_missing_keep_list_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_post(monkeypatch, chat_response('{"links": ["https://a.com/1"]}'))

    outcome = select_relevant_hits("q", [hit("https://a.com/1")])

    assert isinstance(outcome, RelevanceFailure)
    assert outcome.error_code == "llm_invalid_response"


def test_non_json_content_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_post(monkeypatch, chat_response("I think these are fine."))

    outcome = select_relevant_hits("q", [hit("https://a.com/1")])

    assert isinstance(outcome, RelevanceFailure)
    assert outcome.error_code == "llm_invalid_response"


def test_missing_choices_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_post(
        monkeypatch, httpx.Response(200, json={"choices": []}, request=request())
    )

    outcome = select_relevant_hits("q", [hit("https://a.com/1")])

    assert isinstance(outcome, RelevanceFailure)
    assert outcome.error_code == "llm_invalid_response"


def test_structured_output_rejection_falls_back_to_json_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = patch_post(
        monkeypatch,
        httpx.Response(400, json={"error": "unsupported"}, request=request()),
        chat_response('{"keep": ["https://a.com/1"]}'),
    )

    outcome = select_relevant_hits(
        "q", [hit("https://a.com/1"), hit("https://b.com/2")]
    )

    assert outcome == RelevanceSelection(frozenset({"https://a.com/1"}))
    assert len(captured) == 2
    assert payload(captured, 0)["response_format"]["type"] == "json_schema"
    assert payload(captured, 1)["response_format"] == {"type": "json_object"}


def test_empty_reply_is_retried_without_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = patch_post(
        monkeypatch,
        chat_response(""),
        chat_response('{"keep": ["https://a.com/1"]}'),
    )

    outcome = select_relevant_hits(
        "q", [hit("https://a.com/1"), hit("https://b.com/2")]
    )

    assert outcome == RelevanceSelection(frozenset({"https://a.com/1"}))
    assert len(captured) == 2
    assert "max_tokens" in payload(captured, 0)
    assert "max_tokens" not in payload(captured, 1)


def test_two_empty_replies_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_post(monkeypatch, chat_response(""), chat_response(""))

    outcome = select_relevant_hits("q", [hit("https://a.com/1")])

    assert isinstance(outcome, RelevanceFailure)
    assert outcome.error_code == "llm_invalid_response"


def test_server_error_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_post(monkeypatch, httpx.Response(500, json={}, request=request()))

    outcome = select_relevant_hits("q", [hit("https://a.com/1")])

    assert isinstance(outcome, RelevanceFailure)
    assert outcome.error_code == "llm_request_failed"


def test_network_error_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    client = httpx.Client()

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("down", request=request())

    monkeypatch.setattr(client, "post", fake_post)
    monkeypatch.setattr("app.relevance._get_client", lambda: client)
    monkeypatch.setattr("app.relevance.LLM_API_KEY", "test-key")

    outcome = select_relevant_hits("q", [hit("https://a.com/1")])

    assert isinstance(outcome, RelevanceFailure)
    assert outcome.error_code == "llm_request_failed"


def test_missing_api_key_skips_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_client() -> httpx.Client:
        pytest.fail("no request should be sent without an API key")

    monkeypatch.setattr("app.relevance.LLM_API_KEY", "")
    monkeypatch.setattr("app.relevance._get_client", fail_client)

    outcome = select_relevant_hits("q", [hit("https://a.com/1")])

    assert isinstance(outcome, RelevanceFailure)
    assert outcome.error_code == "llm_not_configured"


def test_timeout_is_passed_to_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = patch_post(
        monkeypatch, chat_response('{"keep": ["https://a.com/1"]}')
    )

    select_relevant_hits("q", [hit("https://a.com/1")], timeout_s=2.5)

    assert captured[0]["timeout"] == 2.5


def test_invalid_arguments_raise() -> None:
    with pytest.raises(ValueError):
        select_relevant_hits("   ", [hit("https://a.com/1")])
    with pytest.raises(ValueError):
        select_relevant_hits("q", [])
    with pytest.raises(ValueError):
        select_relevant_hits("q", [hit("https://a.com/1")], timeout_s=0)
