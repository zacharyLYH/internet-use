# Module 3: Target Page Scraper

**Function:** `fetch_target_page`

Open one HTTP(S) URL in the persistent Patchright browser, allow its JavaScript to run, and return the best bounded snapshot available. This module retrieves a page; Module 4 extracts its main content.

## Contract

**Input**

- `url`: required absolute `http` or `https` URL.

**Output**

- `FetchOutcome`, containing either a page snapshot or a typed failure. A page snapshot includes:
  - `requested_url`
  - `final_url` after redirects
  - `title`
  - `status_code`, when a main-document response was received
  - `content_type`, when available
  - `raw_page_html`
  - `render_state`: `complete` or `partial`
  - `warnings`: non-fatal issues encountered during navigation or capture

Do not return only a string: callers need the final URL, HTTP status, and render warnings to distinguish a useful page from an error or incomplete capture. Never include cookies, authorization values, or browser profile contents in the result.

## Browser lifecycle and ownership

1. Start one headed Patchright Chromium context for the service, using a dedicated persistent user-data directory. Configure its path and Chrome executable explicitly; do not attach to or reuse the operator's everyday Chrome profile.
2. Keep that context alive between fetches so its cookies and local storage persist. Create a fresh page for each fetch and close that page in a `finally` block.
3. Serialize browser work with one process-wide lock. A persistent profile cannot safely be opened by multiple browser processes; do not run concurrent navigations against this context.
4. Close the context cleanly on service shutdown. If the browser crashes or disconnects, fail the active fetch, discard stale handles, and recreate the context before accepting the next fetch. Do not automatically retry the active navigation, which could duplicate side effects.

Run one application worker for this profile unless a cross-process browser lock and single browser owner are added. If Chrome reports that the profile is already in use, fail startup as `browser_unavailable`; never delete profile lock files or launch against the same directory anyway.

The profile is a credential store: restrict filesystem permissions, keep it out of source control and backups not intended to hold credentials, and never expose profile paths or browser controls through the public API. The application API must authenticate and authorize a request before it reaches this module. Website login state in the browser profile is separate from API authentication.

### Manual sign-in

Use `make browser-profile` to open the same dedicated profile in a visible Chrome window. Sign in to social media or other sites manually, then close all browser windows. Stop the application first: Chrome must not use the same profile in the service and the sign-in launcher at the same time. The launcher stays attached until its browser window closes or it receives Ctrl+C. It does not automate login or expose credentials to the service API.

## URL and network safety

Treat the URL and every subsequent browser request as untrusted input.

1. Parse and validate the URL before acquiring the browser. Require `http` or `https`, a hostname, and a valid port. Reject embedded username/password, malformed URLs, and unsupported schemes such as `file:`, `data:`, `javascript:`, and `chrome:`.
2. In the default public-web mode, block loopback, private, link-local, multicast, unspecified, and reserved IP destinations, including IPv4-mapped IPv6 and cloud metadata addresses. Recheck every redirect and every subresource request; checking only the initial URL does not prevent SSRF.
3. Enforce destination checks at the browser/network boundary as well as in URL parsing, so DNS rebinding or a hostname resolving to a prohibited address cannot bypass the policy. If this cannot be enforced reliably in the browser, put the browser behind an egress filter that blocks those destinations.
4. Permit private destinations only through an explicit trusted-host configuration, if that use case is added. Do not silently broaden access after a blocked redirect.

Return `invalid_url` for malformed or unsupported URLs and `blocked_destination` for a destination rejected by the network policy. Do not navigate to it.

## Navigation and rendering

1. Open a new page in the persistent context and register request/response failure tracking before navigation.
2. Navigate using a finite navigation deadline and `domcontentloaded` as the initial readiness event. Do not require `networkidle`: analytics, streaming, polling, and long-lived connections can prevent it indefinitely. [Playwright documents `networkidle` as discouraged](https://playwright.dev/python/docs/api/class-page).
3. After DOM content loads, allow a short, bounded settling period. Sample a lightweight document signature (for example, URL, title, and body text/HTML length) until it is stable for two consecutive samples or the render deadline is reached. Use a bounded default overall budget (for example, 30 seconds) and make the navigation and settling limits configurable. Stability is a practical stopping signal, not proof that all lazy content has loaded; `complete` means the configured readiness check passed, not that every page element loaded.
4. Check the DOM size against a configurable maximum before materializing the full HTML string. Then capture `page.content()` once, subject to the remaining total deadline. Record the final URL and main-document response metadata. The snapshot is the rendered DOM at that moment; it does not imply that every image, iframe, or lazy section loaded.

Keep the navigation deadline separate from the total fetch deadline. Include time spent waiting for the browser lock and page cleanup in the total request budget. Never use an unbounded wait.

## Result and failure policy

| Condition | Outcome |
|---|---|
| Valid HTML response and stable DOM before deadline | Return snapshot with `render_state=complete`. |
| Navigation commits but load/settling deadline expires | Capture available DOM if possible; return `render_state=partial` with a `render_timeout` warning. Do not imply the page fully rendered. |
| HTTP 4xx or 5xx response with a readable HTML body | Return the response snapshot and status code. Let the caller decide whether it is useful; a 404 is not a browser transport error. |
| Timeout before a document commits, DNS/TLS failure, connection refusal, or main request failure | Return a typed failure such as `navigation_timeout` or `network_error`; do not pass an empty snapshot to Module 4. |
| Redirect loop or too many redirects | Return `redirect_error`; do not retry automatically. |
| Redirect or subresource violates destination policy | Abort that request and return `blocked_destination` (with a safe warning if the page otherwise loaded). Never follow the blocked navigation. |
| Browser shows a CAPTCHA, access-denied page, login wall, or bot challenge | Return the captured page when available and mark `possible_challenge` as a warning. Do not claim it was bypassed or attempt automated challenge solving. |
| Main document is not HTML (for example, PDF, image, or download) | Return `unsupported_content_type`. Do not feed browser chrome, a download, or binary bytes to Module 4. |
| Blank or near-empty DOM after settling | Return the snapshot as partial with `empty_render`; do not invent content or silently retry. |
| Individual scripts, images, fonts, or subresources fail but a usable main page remains | Return the snapshot with a bounded `resource_failures` warning/count. A failed asset alone does not fail the page. |
| Browser/context disconnects | Return `browser_unavailable`, mark the context unhealthy, and rebuild it before the next request. |
| Page cleanup fails | Log a sanitized cleanup error, continue context health checks, and do not mask the primary fetch outcome. |

Represent failures with stable machine-readable codes and a short safe message. Do not return browser exception text directly to callers; it can contain sensitive URLs or implementation details. A partial result is distinct from a failed fetch.

## Edge cases

- **Redirects:** Preserve both requested and final URLs. Apply URL safety checks at each hop. Module 4 should receive the final URL as its base URL so relative links resolve correctly.
- **Client-side navigation:** Re-check the final page URL and any new main-frame destination after JavaScript redirects. Do not assume `goto()`'s first response URL is the final page.
- **Single-page apps:** DOM content can load before the app renders. Use the bounded settling check; if it remains empty, report `empty_render` rather than waiting forever.
- **Never-settling pages:** Long polling and streaming are normal. Stop at the deadline and return a partial DOM if a document was committed.
- **Downloads, PDFs, and browser error pages:** Do not mistake Chrome's internal viewer/error document for the site's HTML. Classify based on response and navigation events.
- **Popups and tabs:** This module captures only the requested page. Ignore or close popups created during navigation; do not switch the result to a new tab implicitly.
- **Large pages:** Enforce a configurable maximum HTML byte/character size before returning the snapshot. If exceeded, fail with `response_too_large` or truncate only under an explicit policy and mark the result partial; never silently truncate.
- **Repeated URL / profile state:** A new page prevents DOM state from leaking between fetches, but cookies and local storage intentionally persist. Sites may change responses based on earlier visits or login state; record no claim of anonymous reproducibility.
- **Cancellation and client disconnect:** Stop the active navigation, close its page, release the browser lock, and keep the shared context if it remains healthy. Do not leave a background navigation running.
- **Sensitive URLs:** Avoid logging query strings or fragments by default; URLs can contain tokens. Log a redacted host/path and a request correlation ID instead.

## Observability

Record duration, queue/lock wait, navigation duration, status code, final host, render state, response size, and stable failure/warning codes. Avoid logging page HTML, cookies, headers, form values, or full URLs by default. Metrics should let us distinguish slow sites, browser failures, blocks, and empty renders without recording page contents.

## Integration-test coverage

Maintain a small set of read-only integration cases that exercise different rendering behaviors. Prefer controlled local fixtures for deterministic failures and timing; add representative public sites as smoke cases because live sites can change or block the test machine.

| Page type | What the case checks |
|---|---|
| Raw static HTML | Capture source content without requiring JavaScript. |
| Server-rendered article/page | Capture meaningful initial HTML and preserve title, status, and final URL. |
| Client-rendered SPA | Wait for JavaScript-created content to appear. |
| Hydrated or delayed content | Capture content added shortly after DOM content loads. |
| Redirected page | Follow a normal redirect and report requested and final URLs. |
| Login-gated page | Use a manually authenticated test profile where available; verify the session is reused. Keep credentials and profile data out of test fixtures and CI artifacts. |
| Long-polling or never-idle page | Return within the deadline instead of waiting for `networkidle` forever. |
| Error, unavailable, and malformed destinations | Return the expected typed failure or HTTP status without poisoning the next fetch. |
| Oversized page, download, or PDF | Apply the configured size limit or return `unsupported_content_type`. |
| Challenge or access-denied response | Preserve a useful response snapshot when possible and report a challenge warning without claiming success. |

Each integration case should assert the returned fields and content signals, not a full-page HTML snapshot that changes with ads, timestamps, or layout updates. Keep challenge and public-site cases informational or opt-in when their behavior is outside our control.

## Implementation notes

- `app/fetcher.py` exposes `fetch_target_page(url) -> FetchOutcome` and a `Browser` class for configuration and testing. The module owns one persistent context and serializes fetches with a process-wide lock.
- Redirect hops and subresources are blocked through the CDP `Fetch` domain. Chromium does not emit Playwright `page.route` events for server-redirect targets, so route interception alone cannot enforce destination safety.
- One Playwright driver is shared process-wide; `Browser.close()` closes only that browser's context. The driver stops at application shutdown.
- Tests: `tests/test_fetcher_unit.py` covers URL policy without a browser; `tests/test_fetcher_integration.py` uses local HTTP fixtures and a session-scoped headless `Browser` with private destinations explicitly allowed.

## Non-goals

Module 3 does not search for URLs, extract article text, click through arbitrary workflows, submit forms, solve CAPTCHAs, rotate proxies, or guarantee access to a site. Module 4 owns content extraction; any future interactive browsing should be a separate, explicitly scoped capability.
