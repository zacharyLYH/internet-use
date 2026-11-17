# Internet Use

Self-hosted, sequential search and scraping pipeline. It accepts a query or direct URL, discovers pages with SearXNG, renders them with Patchright, extracts Markdown with Trafilatura, and returns structured JSON.

## Layout

- `app/main.py` — FastAPI endpoints, bearer auth, request lifecycle.
- `app/pipeline.py` — pure state/transition core.
- `app/orchestrator.py` — FIFO worker, retries, stage calls.
- `app/tracer.py` — SQLite trace events.
- `app/search.py`, `app/parser.py`, `app/fetcher.py`, `app/cleaner.py` — search, normalization, fetch, extraction.
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

SearXNG runs on `localhost:49731`; the API runs on `127.0.0.1:8000`. Keep JSON output enabled in `searxng/settings.yml`.

## API

```bash
curl -sS http://127.0.0.1:8000/extract \
  -H 'Authorization: Bearer replace-with-a-long-random-key' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/article"}'
```

- `GET /health` — unauthenticated.
- `POST /extract` — requires `Authorization: Bearer <key>` and exactly one of `query` or `url`.

One FIFO worker processes jobs sequentially. Queue full returns `503`; queue wait expiry returns `504`. Per-page failures do not fail the whole search request.

## Pipeline

- Direct URL: render → extract Markdown.
- Query: SearXNG search → normalize/dedupe → render and extract each result.
- Pages are visited sequentially with a small randomized delay.

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

## Commands

```bash
make install                 # install dependencies
make start                   # SearXNG + app (creates API key if missing)
RELOAD= make start           # no file watcher
make stop                    # stop app + SearXNG
make test                    # mypy + full test suite
make browser-profile         # sign in to sites for the fetch stage
```

Production must run in a logged-in GUI session. SearXNG stays in Docker; the app and trace database stay on the host. `make stop` and `dev-down.sh` use `pkill`, so they may also stop other uvicorn processes.
