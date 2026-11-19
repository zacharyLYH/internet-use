from collections.abc import Iterable
from urllib.parse import SplitResult, urlsplit, urlunsplit

_TRAILING_PUNCTUATION = ".,;:!?)]}'\""


def parse_http_url(url: str) -> SplitResult | None:
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    return parsed


def is_valid_http_url(url: str) -> bool:
    return parse_http_url(url) is not None


def parse_lenient_url(url: str) -> SplitResult | None:
    """Parse a URL that may carry markdown or model decorations."""
    return parse_http_url(url.strip().strip("<>").rstrip(_TRAILING_PUNCTUATION))


def url_key(url: str) -> str | None:
    """Canonical comparison key for a URL, or None when it cannot be parsed."""
    parsed = parse_lenient_url(url)
    if parsed is None:
        return None
    return canonical_url(parsed)


def canonical_url(parsed: SplitResult) -> str:
    """Stable key for dedupe and matching: lowercase scheme/host, no trailing
    slash, no fragment."""
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            parsed.query,
            "",
        )
    )


def host_matches(host: str, domains: Iterable[str]) -> bool:
    """True when host equals one of the domains or is a subdomain of one."""
    lowered = host.lower()
    return any(
        lowered == domain or lowered.endswith(f".{domain}") for domain in domains
    )
