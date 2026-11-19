"""Thin, deterministic link triage shared by search normalization and extraction.

No model calls: these are cheap rules that decide whether a URL is obviously
low value (login walls, tags, roots, ads, shorteners, file hosts) and what to do
with an inline article link. Noisy links are unwrapped rather than deleted, so
extracted prose stays intact.
"""

from typing import Literal
from urllib.parse import SplitResult

from app.urls import (
    canonical_url,
    host_matches,
    parse_http_url,
    parse_lenient_url,
    url_key,
)

LinkVerdict = Literal["keep", "unwrap", "drop"]

NOISY_HOSTS = frozenset(
    {
        # Login and consent walls: no article behind them.
        "accounts.google.com",
        "challenges.cloudflare.com",
        "consent.google.com",
        "consent.yahoo.com",
        "guce.yahoo.com",
        # Ad networks and affiliate redirectors.
        "adform.net",
        "adnxs.com",
        "adroll.com",
        "adservice.google.com",
        "amazon-adsystem.com",
        "anrdoezrs.net",
        "awin1.com",
        "clickbank.net",
        "criteo.com",
        "doubleclick.net",
        "dpbolvw.net",
        "googleadservices.com",
        "googlesyndication.com",
        "jdoqocy.com",
        "kqzyfj.com",
        "linksynergy.com",
        "media.net",
        "mgid.com",
        "outbrain.com",
        "popads.net",
        "propellerads.com",
        "quantserve.com",
        "revcontent.com",
        "scorecardresearch.com",
        "shareasale.com",
        "skimresources.com",
        "taboola.com",
        "tkqlhce.com",
        "viglink.com",
        # Analytics and tag managers: never the point of a result.
        "amplitude.com",
        "google-analytics.com",
        "googletagmanager.com",
        "hotjar.com",
        "mixpanel.com",
        "newrelic.com",
        "segment.io",
        "sentry.io",
        # Shorteners and redirectors: the destination is hidden.
        "bit.ly",
        "buff.ly",
        "cutt.ly",
        "goo.gl",
        "is.gd",
        "lnk.to",
        "ow.ly",
        "rb.gy",
        "rebrand.ly",
        "shorturl.at",
        "t.co",
        "t.ly",
        "tiny.cc",
        "tinyurl.com",
        "v.gd",
        # Caches, translators, and paywall proxies.
        "12ft.io",
        "translate.google.com",
        "translate.goog",
        "webcache.googleusercontent.com",
        # File hosts: nothing to extract.
        "4shared.com",
        "mediafire.com",
        "mega.nz",
        "rapidgator.net",
        "uploaded.net",
        "uploadhaven.com",
        "zippyshare.com",
    }
)

# Matched against individual path segments, so all entries stay lowercase.
NOISY_PATH_SEGMENTS = frozenset(
    """
    login log-in logout log-out signin sign-in signout signup sign-up
    register account accounts profile settings preferences cart checkout
    basket subscribe subscription newsletter unsubscribe privacy
    privacy-policy terms terms-of-service tos legal imprint cookies
    cookie-policy consent contact support help faq search tag tags
    category categories read-more share aclk adclick adserver rss feed
    sitemap wp-login.php wp-admin
    """.split()
)


def is_low_value_url(url: str) -> bool:
    """True when a search result is obviously not worth fetching."""
    parsed = parse_http_url(url)
    return parsed is None or _is_noisy_target(parsed, page_key=None)


def classify_link(
    text: str, url: str, page_url: str, seen: set[str]
) -> LinkVerdict:
    """Decide what to do with one inline link found in extracted content.

    Useful targets keep the link. Noisy targets keep their text but lose the
    href, so the surrounding sentence still reads normally. Only links with no
    visible text are removed outright.
    """
    if not " ".join(text.split()).strip():
        return "drop"
    parsed = parse_lenient_url(url)
    if parsed is None:
        return "unwrap"
    key = canonical_url(parsed)
    if key in seen or _is_noisy_target(parsed, page_key=url_key(page_url)):
        return "unwrap"
    seen.add(key)
    return "keep"


def _is_noisy_target(parsed: SplitResult, *, page_key: str | None) -> bool:
    host = (parsed.hostname or "").lower()
    if not host or host_matches(host, NOISY_HOSTS):
        return True
    if page_key is not None and canonical_url(parsed) == page_key:
        return True
    segments = [segment for segment in parsed.path.lower().split("/") if segment]
    if not segments:
        return True
    return any(segment in NOISY_PATH_SEGMENTS for segment in segments)
