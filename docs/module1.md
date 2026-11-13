# Module 1: SearXNG Search Client

**Function:** `fetch_search_response`

Query the locally hosted SearXNG service and return its structured search response. SearXNG contacts its configured search engines; DuckDuckGo can be one of those engines.

## Contract

**Input**

- `query`: required search text.

**Output**

- `search_response`: parsed JSON response from SearXNG.

## Behavior

1. Trim surrounding whitespace and reject an empty query.
2. Send the query to the configured SearXNG `/search` endpoint with JSON output enabled.
3. Check the HTTP response and parse the JSON body.
4. Return the structured response for Module 2 to normalize into `SearchHit` items.

SearXNG's JSON format must be enabled in its settings. Configure its enabled engines there; the application does not scrape an engine's HTML or depend on a particular provider's result-page markup.

This module does not visit destination pages or extract article content. Those belong to later modules. It also does not limit, deduplicate, or denylist results; those are Module 2's job.

## Failure handling

Return a clear error if SearXNG is unavailable, returns an unsuccessful HTTP response, or produces invalid JSON. Do not treat an error page as a valid search response.

## Naming

Use `SearchHit` for a normalized item returned by search. Use `ExtractedPage` for a destination page after fetching and cleaning it. Avoid the generic name `SearchResult`, which can describe either stage.
