# Internet Use

Self-hosted, sequential search and scraping pipeline. It accepts a query or direct URL, discovers pages with SearXNG, renders them with Patchright, extracts Markdown with Trafilatura, and returns structured JSON.

## Layout

- `app/main.py` — FastAPI endpoints, bearer auth, request lifecycle.
- `app/pipeline.py` — pure state/transition core.
- `app/orchestrator.py` — FIFO worker, retries, stage calls.
- `app/tracer.py` — SQLite trace events.
- `app/search.py`, `app/parser.py`, `app/fetcher.py`, `app/cleaner.py` — search, normalization, fetch, extraction.
- `app/relevance.py` — optional LLM relevance filter between search and fetch.
- `app/linkfilter.py` — deterministic low-value URL and noisy inline-link rules.
- `app/urls.py` — shared HTTP URL validation.

## Requirements

- Python 3.12
- Docker for SearXNG
- Chrome
- A GUI-enabled macOS session for production (Patchright uses headed Chrome)

## Setup

```bash
make start
```

`make start` installs dependencies, creates `data/api_keys.txt` (printing the bearer key) if missing, starts SearXNG with Docker Compose, and runs the API in the foreground. On the first run, copy the printed key for API requests.

On that first run it also asks for an optional OpenAI-compatible LLM endpoint, model, and API key, and stores the answers in `data/llm.env`. Leave the API key blank to skip the relevance filter and keep the default behavior. Change it later with `make llm-config`.

SearXNG runs on `localhost:49731`; the API runs on `127.0.0.1:8000`. Keep JSON output enabled in `searxng/settings.yml`.

## API

```bash
curl -sS http://127.0.0.1:8000/v1/chat/completions \
  -H 'Authorization: Bearer replace-with-a-long-random-key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"any","messages":[{"role":"user","content":"https://example.com/article"}]}'
```

The endpoint accepts the OpenAI chat completions request shape; `model` is ignored. The last user message is treated as a URL when it is a single absolute HTTP(S) URL, otherwise as a search query.

- `GET /health` — unauthenticated.
- `POST /v1/chat/completions` — requires `Authorization: Bearer <key>`.

One FIFO worker processes jobs sequentially. Queue full returns `503`; queue wait expiry returns `504`. Per-page failures do not fail the whole search request.

## Pipeline

- Direct URL: render → extract Markdown.
- Query: SearXNG search → normalize/dedupe → drop obviously low-value URLs → optional LLM relevance filter → render and extract each result.
- Pages are visited sequentially with a small randomized delay.
- Low-value URLs are dropped during normalization, so neither the LLM nor the browser ever sees them; the count is traced as `low_value_count`. Blocked classes: login/consent walls, account and tag/category listings, bare home pages, ad networks and affiliate redirectors, analytics and tag managers, link shorteners, caches/translators/paywall proxies, and file hosts. Social platforms are not filtered: they are scraped like any other site.
- Extraction drops tables and trailing reference lists, strips citation/superscript artifacts, keeps useful inline links while unwrapping noisy ones (log in, read more, share, tag, duplicate targets) so the href disappears but the sentence stays, caps each page at 20k characters on a paragraph boundary, and treats login walls or UI-chrome pages as empty.

When `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY` are all set, one structured-output chat request runs after search results are collected and before the first page is fetched; the model returns the exact URLs of the relevant results and the rest are never fetched. If filtering leaves fewer than `max_results` results and search pages remain, another SearXNG page is requested, filtered again, and the process repeats until the target is met or `max_pages` runs out (`relevance.backfill` in the trace). The filter fails open: on a missing key, timeout, error, or unparsable reply nothing is dropped, and `relevance.failed` records why.

Trace events are appended to `data/traces.sqlite3`. Fetched HTML and Markdown are not stored.

## Configuration

| Variable | Default |
| --- | --- |
| `SEARXNG_URL` | `http://localhost:49731` |
| `TRACE_DB_PATH` | `data/traces.sqlite3` |
| `API_KEYS_FILE` | `data/api_keys.txt` |
| `QUEUE_MAX_SIZE` | `100` |
| `QUEUE_WAIT_S` | `30` |
| `MAX_REQUEST_BYTES` | `1000000` |
| `LLM_BASE_URL` | `https://api.openai.com/v1` |
| `LLM_MODEL` | `gpt-4o-mini` |
| `LLM_API_KEY` | unset (filter off) |
| `LLM_FILTER_TIMEOUT_S` | `10` |
| `LLM_ENV_FILE` | `data/llm.env` |

The relevance filter has no separate enable flag: it runs when `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY` are all present, and is skipped otherwise.

## Commands

```bash
make install                 # install dependencies
make start                   # SearXNG + app (creates API key if missing)
make llm-config              # set or change the relevance filter endpoint/model/key
RELOAD= make start           # no file watcher
make stop                    # stop app + SearXNG
make test                    # mypy + full test suite
make browser-profile         # sign in to sites for the fetch stage
```

Production must run in a logged-in GUI session. SearXNG stays in Docker; the app and trace database stay on the host. `make stop` and `dev-down.sh` use `pkill`, so they may also stop other uvicorn processes.
