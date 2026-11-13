# Module 2: Search Response Normalizer

**Function:** `parse_search_response`

Turn SearXNG's JSON into an ordered list of `SearchHit` items. Module 1 does the network I/O; this module is pure and never fetches anything.

## Contract

**Input**

- `search_response`: parsed JSON object returned by Module 1.
- `max_results`: optional final limit, applied after filtering.
- `domain_denylist`: optional domains to exclude, such as `pinterest.com`.

**Output**

- `List[SearchHit]`, each with:
  - `title`: display title.
  - `url`: fetchable `http(s)` URL.
  - `snippet`: summary text taken from SearXNG's `content`.
  - `engines`: engines that returned the hit, from `engines` or `[engine]`.
  - `category`: SearXNG category (for example `general` or `news`).
  - `score`: SearXNG's merged relevance score.
  - `published`: publication date string when SearXNG provides one, otherwise `null`.

## Behavior

1. Read `results` from `search_response`. A missing key means no hits; a non-list value is malformed.
2. For each entry, map `title`, `url`, `content` → `snippet`, `engines`, `category`, `score`, and `publishedDate`/`pubdate` → `published`.
3. Skip entries with a blank title or URL, or a URL that is not `http(s)`.
4. Drop duplicates by normalized URL; keep the first occurrence. Normalization ignores scheme/host case and URL fragments.
5. Drop URLs whose hostname equals or is a subdomain of a denylisted domain.
6. Apply `max_results` after filtering and deduplication, preserving SearXNG's order.
7. Never mutate the input response.

Domain matching is case-insensitive and hostname-based, so `pinterest.com` also blocks `www.pinterest.com` but not `notpinterest.com`. Missing optional fields become empty engines, empty category, `0.0` score, an empty snippet, or `null` published date.

## Denylist

SearXNG has no per-request exclude-domain parameter. Appending `-site:` to the query works only on engines that support that syntax, so it is unreliable across a mixed engine set. Module 1 sends the query unchanged; this module filters by hostname after results come back.

An instance-wide `hostname_replace` plugin can hide domains, but that is static SearXNG configuration, not a per-request denylist. Keep request denylists here.

## Failure handling

- Missing `results` returns an empty list.
- `results` present but not a list raises `ValueError`.
- Bad individual entries are skipped; they do not fail the whole response.
- Negative `max_results` raises `ValueError`.

## Why this stays separate

SearXNG returns structured JSON, but that is not our contract: snippets live under `content`, duplicate URLs appear across engines, entries can be partial, and Module 3 needs one stable shape. Keeping the projection here leaves Module 1 network-only and makes this logic trivial to unit test.

Limiting must happen here, after deduplication and denylisting. Module 1 must not trim results, or it can discard candidates that would have survived filtering and leave later stages short.

## Naming

Use `SearchHit` for a normalized search item. Use `ExtractedPage` for a fetched and cleaned destination page. Avoid the generic `SearchResult`.
