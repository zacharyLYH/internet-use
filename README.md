Local, Stateless Search & Scraping Pipeline

## 1. Overview & Objectives

Build a self-hosted, deterministic, sequential search and extraction pipeline running locally on a Mac mini. The service accepts a search query or a direct URL, executes retrieval via non-JS static endpoints and real browser instances, cleans the extracted content into Markdown, and returns a structured payload.

The architecture adheres to a **state machine model** where each component acts as a stateless, pure function with strict, typed input and output contracts. Performance optimization is explicitly deprioritized in favor of simplicity, reliability, and ease of maintenance (KISS).

---

## 2. Core Architecture & Design Principles

* **Stateless Pure Functions:** Every pipeline stage is a decoupled module receiving explicit inputs and returning explicit outputs with no internal side effects.
* **Sequential Execution:** Single-threaded processing (one request/URL at a time) to eliminate race conditions, profile locks, and concurrency overhead.
* **Zero Third-Party APIs:** Hand-rolled retrieval using native static endpoints (`html.duckduckgo.com`), local parsing (`BeautifulSoup`), local browser automation (`Patchright` + real Chrome), and content extraction (`Trafilatura`).
* **Clean Interface Boundary:** The low-level browser driver is abstracted behind a clean interface adapter, ensuring future swappability (e.g., migrating to Camoufox or alternate runners) without touching orchestrator logic.

---

## 3. Component Specifications (State Machine Modules)

### Module 1: Search Fetcher (`fetch_serp_html`)

* **Role:** Fetches the raw HTML of DuckDuckGo's static search results page.
* **Input Contract:** `query` (string), optional `domain_denylist` (list of strings).
* **Processing Logic:**
1. Constructs query string with appended `-site:` operators for denylisted domains (e.g., Pinterest, Quora).
2. Sends a lightweight HTTP POST/GET request to `[https://html.duckduckgo.com/html/](https://html.duckduckgo.com/html/)`.


* **Output Contract:** `raw_html` (string).

### Module 2: SERP Parser (`parse_serp_dom`)

* **Role:** Parses the static search results HTML and extracts structured metadata.
* **Input Contract:** `raw_html` (string), `max_results` (integer).
* **Processing Logic:**
1. Loads HTML into `BeautifulSoup`.
2. Traverses DOM nodes matching result containers.
3. Extracts titles, snippets, and target URLs (unwrapping any tracking redirects if necessary).


* **Output Contract:** `List[SearchResult]` where each object contains:
* `title`: string
* `url`: string
* `snippet`: string



### Module 3: Target Page Scraper (`fetch_target_page`)

* **Role:** Navigates to a target URL using a stealth-patched real browser instance and retrieves the fully rendered DOM.
* **Input Contract:** `url` (string).
* **Processing Logic:**
1. Invokes the Patchright driver bound to a persistent local Chrome profile (`--user-data-dir`).
2. Navigates to `url`.
3. Waits for `networkidle` or basic DOM stability.
4. Grabs the final raw HTML state via `page.content()`.


* **Output Contract:** `raw_page_html` (string).

### Module 4: Content Cleaner (`clean_to_markdown`)

* **Role:** Strips boilerplate, navigation, footers, and ads, converting raw HTML into clean Markdown.
* **Input Contract:** `raw_page_html` (string), `url` (string).
* **Processing Logic:**
1. Passes `raw_page_html` to **Trafilatura**.
2. Executes main-body text extraction heuristics.
3. Formats output into clean Markdown.


* **Output Contract:** `markdown_content` (string).

---

## 4. Pipeline Execution Flows

### Path A: Direct URL Ingestion

1. **Input:** `{"mode": "url", "target_url": "[https://example.com/article](https://example.com/article)"}`
2. **Step 1:** Call **Module 3 (`fetch_target_page`)** with `target_url`.
3. **Step 2:** Pass output to **Module 4 (`clean_to_markdown`)**.
4. **Output:** Returns JSON payload containing the URL and `markdown_content`.

### Path B: Search Query Discovery & Extraction

1. **Input:** `{"mode": "search", "query": "best mechanical keyboard", "denylist": ["pinterest.com", "quora.com"]}`
2. **Step 1:** Call **Module 1 (`fetch_serp_html`)** with query and denylist.
3. **Step 2:** Pass SERP HTML to **Module 2 (`parse_serp_dom`)** to get top $N$ clean URLs.
4. **Step 3 (Sequential Loop):** For each URL in the parsed list:
* Call **Module 3 (`fetch_target_page`)** to get raw page HTML.
* Call **Module 4 (`clean_to_markdown`)** to get clean Markdown.


5. **Output:** Returns a structured JSON array:
```json
[
  {
    "title": "Example Title",
    "url": "https://example.com/article",
    "content_markdown": "# Example Title\n\nCleaned markdown body..."
  }
]

```



---

## 5. Operational Constraints & Error Handling

* **Concurrency Limit:** Hard restriction to $N=1$ active browser operation. No async worker pools or parallel tab execution.
* **Domain Jitter / Rate Limiting:** Enforce a minimum 1.5-second sleep delay between successive target page fetches to avoid tripping origin server rate limits.
* **Graceful Degradation:** If **Module 3** or **Module 4** fails on an individual target URL (e.g., HTTP 404, connection timeout, or Trafilatura returning empty content), log the error locally, skip that specific URL, and continue sequentially to the next result in the list.
