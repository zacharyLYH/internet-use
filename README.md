# Internet Use

Self-hosted search, scraping, and transcription API. Give it a query or URL; it returns Markdown. OpenAI-compatible chat and transcription endpoints are included.

## Requirements

- Python 3.12, Docker (SearXNG), Chrome, ffmpeg
- A GUI-enabled macOS session for production (Patchright uses headed Chrome)

## Setup

```bash
make start
```

This installs dependencies, creates `data/api_keys.txt` (printing the bearer key), starts SearXNG, and runs the API on `127.0.0.1:8000`. On the first run it asks for two optional OpenAI-compatible services, stored `0600`:

- `data/llm.env` — relevance filter (`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`); change with `make llm-config`.
- `data/stt.env` — media transcription (`STT_BASE_URL`, `STT_MODEL`, `STT_LANGUAGE`, `STT_API_KEY`); change with `make stt-config`. The provider must accept mp3 (wav/flac/ogg also work; webm and m4a do not, so the pipeline uploads mp3).

A blank API key disables that feature. Both fail open.

## API

Every endpoint except `/health` needs `Authorization: Bearer <key>`.

- `POST /v1/extract` — general query or URL extraction: `{"query": "..."}` or `{"url": "..."}`, optional `max_results` (1-20) and `max_pages` (1-10). Returns the same `ExtractResponse` as chat.
- `POST /v1/chat/completions` — OpenAI chat shape; the last user message is the query or URL.
- `POST /v1/audio/transcriptions` — OpenAI-compatible multipart upload (`file`, optional `language`; `response_format=json` only). Proxies to the configured STT and returns `{"text": "..."}`.

```bash
KEY=replace-with-your-key

curl -sS http://127.0.0.1:8000/v1/extract \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"query":"how do heat pumps work"}'

curl -sS http://127.0.0.1:8000/v1/audio/transcriptions \
  -H "Authorization: Bearer $KEY" -F file=@clip.mp3 -F language=en
```

`/v1/extract` and `/v1/chat/completions` share one FIFO worker; queue full returns `503`, queue wait expiry `504`. `/v1/audio/transcriptions` calls the STT provider directly.

## Pipeline

- Query: SearXNG -> normalize/dedupe -> drop low-value URLs -> optional LLM relevance filter -> render and extract each page.
- Direct URL: render -> extract. Pages are visited sequentially with a small randomized delay.
- Media: when STT is configured, allowlisted hosts skip the browser. yt-dlp reads metadata, skips live streams and media longer than `MAX_MEDIA_DURATION_S` (20 min), prefers existing subtitles, otherwise downloads audio and ffmpeg converts it to 16 kHz mono 64 kbps mp3 before `POST {STT_BASE_URL}/audio/transcriptions`. The transcript is the page Markdown under `## Transcript`; the `media` block reports platform, duration, source, state, and character count. Any failure after metadata returns the description only.
- Allowlist (`app/media.py`): YouTube, TikTok, Instagram, Facebook, X/Twitter, Vimeo, Dailymotion, Twitch, SoundCloud, Reddit, Streamable. The input host is checked, so yt-dlp cannot be used as a generic downloader.

Trace events go to `data/traces.sqlite3`; fetched HTML and audio are not stored.

## Configuration

| Variable | Default |
| --- | --- |
| `SEARXNG_URL` | `http://localhost:49731` |
| `TRACE_DB_PATH` | `data/traces.sqlite3` |
| `API_KEYS_FILE` | `data/api_keys.txt` |
| `QUEUE_MAX_SIZE` / `QUEUE_WAIT_S` | `100` / `30` |
| `MAX_REQUEST_BYTES` | `1000000` |
| `MAX_AUDIO_BYTES` | `25000000` |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | `https://api.openai.com/v1` / `gpt-4o-mini` / unset |
| `LLM_FILTER_TIMEOUT_S` | `10` |
| `STT_BASE_URL` / `STT_MODEL` / `STT_API_KEY` | `https://api.openai.com/v1` / `whisper-1` / unset |
| `STT_LANGUAGE` | unset (auto-detect) |
| `STT_TIMEOUT_S` / `MEDIA_TIMEOUT_S` | `300` / `120` |
| `MAX_MEDIA_DURATION_S` | `1200` |
| `MAX_TRANSCRIPT_CHARS` | `40000` |

## Commands

```bash
make start          # SearXNG + app
make stop           # stop app + SearXNG
make test           # mypy + pytest
make llm-config     # relevance filter endpoint/model/key
make stt-config     # STT endpoint/model/language/key
make browser-profile
```

Production must run in a logged-in GUI session. SearXNG stays in Docker; the app and trace database stay on the host. `make stop` uses `pkill`, so it may also stop other uvicorn processes.
