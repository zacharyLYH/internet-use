import pytest

from app.fetcher import FetchFailure, fetch_target_page


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://user:pass@example.com",
        "http://example.com:99999",
    ],
)
def test_invalid_urls(url: str) -> None:
    outcome = fetch_target_page(url)

    assert isinstance(outcome, FetchFailure)
    assert outcome.error_code == "invalid_url"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
    ],
)
def test_private_and_metadata_destinations_are_blocked(url: str) -> None:
    outcome = fetch_target_page(url)

    assert isinstance(outcome, FetchFailure)
    assert outcome.error_code == "blocked_destination"
