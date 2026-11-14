from urllib.parse import SplitResult, urlsplit


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
