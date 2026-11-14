# Module 4: Content Cleaner

**Function:** `clean_to_markdown`

Extract the main readable content from an HTML string and convert it to Markdown. Module 4 does no network access and does not decide where the HTML came from or whether the page is trustworthy or relevant.

## Contract

**Input**

- `raw_page_html`: HTML string to process. It may be empty, partial, malformed, or represent an HTTP error, login, or challenge page.
- `url`: the page's final absolute `http(s)` URL without credentials, used as extraction context and to resolve relative links.

**Output**

- `CleanOutcome`, either `CleanSuccess` or `CleanFailure`.
- `CleanSuccess` contains:
  - `markdown_content`: extracted Markdown, or an empty string when no main text is found.
  - `extraction_state`: `extracted` or `empty`.
  - `source_html_bytes`: UTF-8 byte length of the input HTML.
  - `markdown_bytes`: UTF-8 byte length of the returned Markdown.
- `CleanFailure` contains:
  - `error_code`: `invalid_input` or `extraction_error`.
  - `message`: a short, stable, sanitized message.

Do not return only a string: callers need the extraction state and byte counts to trace the result without storing page content. Never include HTML or Markdown in failure messages.

The Markdown is returned to the caller but is not written to the request trace. The trace records its byte length and extraction outcome only.

## Behavior

1. Validate that `raw_page_html` and `url` are strings and that `url` is an absolute `http(s)` URL. Do not fetch the URL, inspect Module 3's status, or apply Module 3's destination policy; the caller owns retrieval and status handling.
2. Short-circuit empty or whitespace-only HTML to an empty success with zero `markdown_bytes`. Do not call Trafilatura when there is nothing to extract.
3. Pass the HTML and final URL to Trafilatura's `extract` function with Markdown output enabled.
4. Normalize a `None` or whitespace-only result to `markdown_content=""` and `extraction_state="empty"`. Trim only leading and trailing whitespace; preserve meaningful internal structure.
5. Measure the input and output as UTF-8 bytes, not Python character counts, so metrics are comparable across languages. `source_html_bytes` measures the input string as received, not the original wire bytes.
6. Return `extraction_state="extracted"` only when non-whitespace Markdown remains.
7. Never mutate the input, write files, launch a browser, or perform network I/O.

Trafilatura supports Markdown as an output format and returns `None` when extraction finds no text. Its extraction options include comment control, tables, links, images, formatting, and a faster mode that skips fallback heuristics. [Trafilatura API](https://trafilatura.readthedocs.io/en/latest/corefunctions.html)

## Extraction configuration

Use a stable initial configuration:

```python
extract(
    raw_page_html,
    url=url,
    output_format="markdown",
    include_comments=False,
    include_tables=True,
    include_links=True,
    include_images=False,
    include_formatting=True,
    fast=False,
)
```

- Exclude comments because they are rarely part of the main content.
- Retain tables and links; links are resolved against the supplied final URL where possible.
- Exclude images; extraction is text-focused and must not introduce additional remote resources.
- Do not use `fast=True` initially. Fast mode skips fallback extraction and can miss content on difficult pages. Prefer reliability until measured performance shows otherwise.
- Do not enable metadata, language filtering, or deduplication caches. This module is stateless and returns content only.

## Failure handling

| Condition | Outcome |
|---|---|
| Empty or whitespace-only HTML | Return empty content with `extraction_state="empty"` and `markdown_bytes=0`. |
| Valid HTML but Trafilatura returns no text | Return empty content with `extraction_state="empty"`; do not retry automatically. |
| HTML is non-HTML text or malformed markup | Process it normally; return `empty` unless Trafilatura extracts text. Do not add a separate page classifier. |
| `raw_page_html` or `url` has the wrong type, or `url` is empty, not absolute `http(s)`, or contains credentials | Return `invalid_input`. |
| Trafilatura raises an exception | Return `extraction_error` with a generic sanitized message, such as `"content extraction failed"`. Log the exception type, not the input or raw exception text. Do not return partial output unless the library returned it normally. |
| HTML represents an HTTP error, login, or challenge page | Process it like any other HTML. Module 4 does not classify page status or content purpose. |

Module 4 failures are deterministic for the same input, configuration, and library version. Retrying the same HTML is not useful; the orchestrator may record the failure and continue to the next URL.

## Output quality and scope

- Trafilatura identifies likely main content; it does not guarantee semantic correctness or detect every page type.
- The output preserves useful structure such as headings, paragraphs, lists, tables, and links, but exact Markdown formatting is library-defined and may change between versions.
- `empty` is a meaningful result, not an exception. A non-empty result does not imply the page was a successful article fetch; the caller can interpret it alongside retrieval metadata.
- Relative links should become absolute when the supplied URL is usable. Links and any residual markup remain untrusted input; do not automatically fetch or render them downstream.
- Do not add LLM summaries, metadata/schema extraction, language translation, URL fetching, HTML sanitization, or site-specific selectors here.

Keeping extraction separate from Module 3 means text processing needs no browser, profile, lock, or network. It also keeps extraction deterministic and cheap to test with fixed HTML fixtures.

## Test coverage

Module 4 tests are offline and do not start a browser or HTTP server.

| Case | What it checks |
|---|---|
| Readable article HTML | Extracts headings and paragraphs; excludes navigation and comments. |
| Tables and links | Retains table structure and renders links using the supplied base URL. |
| Relative links | Resolves relative targets when the final URL is valid. |
| Empty or whitespace-only HTML | Returns `empty`, zero Markdown bytes, and the correct source byte count. |
| Script/style-only or textless HTML | Returns `empty` without raising. |
| Unicode content | Byte counts use UTF-8, not character counts. |
| Invalid input types or URL | Returns `invalid_input`. |
| Extraction exception | Returns `extraction_error` without leaking exception text or content. |
| HTTP error or challenge HTML | Processes the HTML normally without inferring HTTP status. |

Assert content signals and stable fields rather than an exact full Markdown string, which can vary by Trafilatura version.

## Trace fields

Emit the following safe facts for the orchestrator's append-only trace:

- extraction outcome (`extracted`, `empty`, or `failed`)
- source HTML byte count
- Markdown byte count
- elapsed time
- stable error code for failures

Do not emit HTML, Markdown, page URLs, or query strings into trace events or logs.

## Naming

Use `CleanOutcome` for the result union, `CleanSuccess` and `CleanFailure` for its variants, and `ExtractionState` for `extracted` or `empty`. Use `ExtractedPage` for a fetched and cleaned destination page. Avoid generic names such as `CleanResult`.

## Implementation notes

- `app/cleaner.py` exposes `clean_to_markdown(raw_page_html, url) -> CleanOutcome` and the `CleanSuccess` / `CleanFailure` dataclasses.
- The implementation trims the base URL, rejects credentials, short-circuits empty HTML, and converts Trafilatura exceptions into a generic `extraction_error`. It logs only the outcome, byte counts, duration, and exception type.
- Tests live in `tests/test_cleaner_unit.py`. They are offline, do not start a browser or HTTP server, and cover article extraction, comments/tables/links, relative URLs, Unicode byte counts, empty results, invalid inputs, and extraction exceptions.

## Non-goals

Module 4 does not fetch pages, interpret HTTP status, detect paywalls or challenges, clean or rewrite URLs beyond link resolution, sanitize Markdown for rendering, summarize text, extract structured metadata, or choose site-specific content selectors. Those belong to other modules or are out of scope.
