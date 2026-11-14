# Local, Stateless Search & Scraping Pipeline

## 1. Overview

Build a self-hosted, sequential search and extraction pipeline for a Mac mini. It accepts a search query or a direct URL, discovers pages through local SearXNG, renders target pages in a persistent Patchright browser, cleans their content into Markdown, and returns structured results.

Each stage has a clear input and output contract. The initial design favors simplicity and reliability over concurrency.

## 2. Design principles

- **Small pipeline:** Keep each stage focused and easy to replace.
- **Sequential browser use:** Run one browser operation at a time to avoid profile locks and resource overhead.
- **Self-hosted search:** Use SearXNG as the search interface, with enabled engines configured centrally. SearXNG sends queries to those upstream engines; it avoids a paid search API, but is not offline.
- **Persistent browser session:** Use Patchright with a local Chrome profile for JavaScript rendering and reusable session state.
- **Local content extraction:** Use Trafilatura to turn rendered page HTML into readable Markdown.

## 3. Modules

### Module 1: SearXNG Search Client (`fetch_search_response`)

- **Role:** Query the locally hosted SearXNG `/search` endpoint.
- **Input:** `query`.
- **Behavior:** Request JSON output and return the parsed response. SearXNG must have JSON output enabled; its configured engines determine the upstream search sources. DuckDuckGo can be enabled as one engine.
- **Output:** `search_response` (structured JSON).

See [docs/module1.md](docs/module1.md) for the detailed contract.

### Module 2: Search Response Normalizer (`parse_search_response`)

- **Role:** Convert SearXNG's response into consistent search hits.
- **Input:** `search_response`, result limit, and optional domain denylist.
- **Behavior:** Normalize title, URL, and snippet; remove invalid or duplicate URLs; apply the denylist; keep the requested number of hits.
- **Output:** `List[SearchHit]`, each with `title`, `url`, `snippet`, `engines`, `category`, `score`, and `published`.

See [docs/module2.md](docs/module2.md) for the detailed contract.

### Module 3: Target Page Scraper (`fetch_target_page`)

- **Role:** Load a destination page in a JavaScript-capable browser and return a bounded rendered snapshot.
- **Input:** `url`.
- **Behavior:** Use Patchright with a persistent local Chrome profile, block private and metadata destinations at the network boundary, navigate to the URL, wait for the DOM to settle, then capture the rendered HTML.
- **Output:** `FetchOutcome` (`PageSnapshot` or `FetchFailure`) with the requested/final URLs, title, status, content type, HTML, render state, and warnings.

See [docs/module3.md](docs/module3.md) for browser lifecycle, URL safety, rendering limits, and failure handling.

### Module 4: Content Cleaner (`clean_to_markdown`)

- **Role:** Extract the main content and convert it to Markdown.
- **Input:** `raw_page_html` and `url`.
- **Behavior:** Use Trafilatura to remove page boilerplate and extract readable content.
- **Output:** `markdown_content`.

## 4. Pipeline flows

### Direct URL

1. Receive a target URL.
2. Fetch its rendered HTML with Module 3.
3. Clean it with Module 4.
4. Return an `ExtractedPage` containing the URL and Markdown.

### Search query

1. Query SearXNG with Module 1.
2. Normalize and filter its response with Module 2 to get the top hits.
3. Visit each hit sequentially with Module 3, then clean it with Module 4.
4. Return the extracted pages with their titles, URLs, and Markdown content.

## 5. Alternatives and scope decision

Coverage depends on the browser, session, network, and site defenses. No tool has a meaningful universal site-count guarantee.

| Tool | Coverage | Scope | Pros |
|---|---|---|---|
| **Our pipeline** | Patchright runs JavaScript and reuses a local browser profile. | Search → render → Markdown. | Focused workflow; persistent session control. |
| [CRW](https://docs.fastcrw.com/) | Multiple renderer paths; access still depends on the self-hosted network and site defenses. | Search, scrape, crawl, map, extraction, MCP, browser companion. | Broad features; renderer fallbacks. |
| [Firecrawl](https://github.com/firecrawl/firecrawl) | Browser-backed scraping; self-hosted coverage depends on deployment and network. | Search, scrape, crawl, map, interact, structured extraction, documents. | Broad, ready-made API. |
| [Crawl4AI](https://docs.crawl4ai.com/) | Configurable browser crawling and JavaScript execution. | Crawler, Markdown, CSS/XPath and LLM extraction. | Flexible crawler framework. |

### Why build this smaller pipeline

We want a small query-to-page pipeline with a persistent local browser session. That gives us direct control over the workflow and avoids adopting features we do not currently need. Coverage and fidelity remain things to measure against our target sites.

## 6. Operational constraints

- **Concurrency:** One active browser operation at a time.
- **Pacing:** Add a small randomized delay (about 0.5–1 second) between target pages. Browser navigation and extraction already take time; tune pacing based on observed responses.
- **Per-page failures:** Log an individual page failure and continue with the next hit. Do not let an inaccessible page fail the whole search request.

## 7. Commands

- **First-time setup (for LSP):** `make install`
- **Dev:** `./scripts/dev-up.sh` — starts SearXNG, waits for it to be healthy, then runs the app on `127.0.0.1:8000`. `Ctrl+C` tears both down.
- **Prod:** `RELOAD= ./scripts/dev-up.sh` — same thing, just no file watcher.
- **Test:** `make test` — starts SearXNG if needed, runs mypy + unit/integration tests, then tears down only what it started. Browser integration tests use local fixtures with headless Chrome.
- **Sign in to sites:** Stop the app, then run `make browser-profile`. This opens the persistent Chrome profile used by Module 3; close its windows when finished before starting the app again.
- **Teardown:** `./scripts/dev-down.sh` (safe to run twice).

## 8. Sharp edges

- The production app has to run in a **GUI-enabled, logged-in macOS session**. Patchright drives headed Chrome, so SSH or a headless production box won't work. Tests use headless Chrome against local fixtures.
- SearXNG lives in Docker; the app stays on the host.
- Keep `json` enabled in `searxng/settings.yml`, or Module 1 gets a clean `SearchError`.
- SearXNG is on `49731`, the app on `8000`. Change one and update `SEARXNG_URL` too.
- `make stop` and `dev-down.sh` use `pkill`, so they'll take down other uvicorn processes too.
- `tests/test_search_unit.py` and `tests/test_parser_unit.py` are offline. The integration files call live SearXNG and local browser fixtures; `make test` starts what they need.
