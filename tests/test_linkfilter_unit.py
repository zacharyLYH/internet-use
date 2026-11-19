import pytest

from app.linkfilter import classify_link, is_low_value_url

PAGE = "https://example.com/posts/kafka-retention"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/login",
        "https://example.com/sign-in?next=/x",
        "https://example.com/",
        "https://example.com",
        "https://example.com/blog/tags/kafka",
        "https://example.com/category/engineering",
        "https://example.com/privacy",
        "https://facebook.com/share?u=x",
        "https://doubleclick.net/ad/123",
        "https://www.googlesyndication.com/pagead/js",
        "https://outbrain.com/click?x=1",
        "https://taboola.com/x",
        "https://ad.doubleclick.net/ddm/trackclk/abc",
        "https://example.com/adclick?target=x",
        "https://accounts.google.com/o/oauth2",
        "https://consent.google.com/ml?continue=x",
        "https://guce.yahoo.com/consent?x=1",
        "https://challenges.cloudflare.com/turnstile",
        "https://bit.ly/3abc",
        "https://t.co/abc",
        "https://www.tinyurl.com/abc",
        "https://mediafire.com/file/abc",
        "https://mega.nz/file/abc",
        "https://www.google-analytics.com/collect",
        "https://foo-com.translate.goog/page?_x_tr_sl=en",
        "https://webcache.googleusercontent.com/search?q=cache:x",
        "https://example.com/wp-login.php",
        "not-a-url",
        "/relative",
    ],
)
def test_low_value_search_urls(url: str) -> None:
    assert is_low_value_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/posts/kafka-retention",
        "https://kafka.apache.org/documentation/",
        "https://example.com/blog/login-security-best-practices",
        "https://example.com/guides/desk-setup",
        "https://www.pinterest.com/pin/1",
        "https://www.reddit.com/r/kafka/comments/x",
        "https://www.linkedin.com/pulse/some-article",
        "https://x.com/someone/status/123",
        "https://www.youtube.com/watch?v=abc",
        "https://stackoverflow.com/questions/123/how-to",
        "https://example.com/wp-content/uploads/report.pdf",
    ],
)
def test_useful_search_urls_are_kept(url: str) -> None:
    assert not is_low_value_url(url)


def test_useful_link_is_kept_and_recorded() -> None:
    seen: set[str] = set()

    verdict = classify_link(
        "official Kafka docs",
        "https://kafka.apache.org/documentation/",
        PAGE,
        seen,
    )

    assert verdict == "keep"
    assert seen == {"https://kafka.apache.org/documentation"}


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/login.",
        "https://taboola.com/x,",
        "<https://example.com/sign-in>",
    ],
)
def test_noisy_links_are_detected_with_decorations(url: str) -> None:
    assert classify_link("the page", url, PAGE, set()) == "unwrap"


def test_noisy_links_are_unwrapped() -> None:
    assert classify_link("log in", "https://example.com/login", PAGE, set()) == "unwrap"
    assert classify_link("Read more", "/read-more", PAGE, set()) == "unwrap"
    assert (
        classify_link("promoted offer", "https://taboola.com/x", PAGE, set())
        == "unwrap"
    )


def test_social_link_is_kept() -> None:
    verdict = classify_link(
        "thread", "https://www.reddit.com/r/kafka/comments/x", PAGE, set()
    )

    assert verdict == "keep"


def test_link_without_visible_text_is_dropped() -> None:
    assert classify_link("   ", "https://example.com/login", PAGE, set()) == "drop"


def test_noisy_link_with_real_prose_is_unwrapped() -> None:
    verdict = classify_link(
        "the retention section covers this",
        "https://example.com/login",
        PAGE,
        set(),
    )

    assert verdict == "unwrap"


def test_duplicate_target_is_unwrapped() -> None:
    seen: set[str] = set()

    first = classify_link("docs", "https://kafka.apache.org/a", PAGE, seen)
    second = classify_link("more docs", "https://Kafka.Apache.org/a/", PAGE, seen)

    assert (first, second) == ("keep", "unwrap")


def test_self_link_is_unwrapped() -> None:
    verdict = classify_link("this article", PAGE, PAGE, set())

    assert verdict == "unwrap"


def test_call_to_action_to_useful_url_keeps_the_link() -> None:
    verdict = classify_link(
        "read more", "https://example.com/whitepapers/retention", PAGE, set()
    )

    assert verdict == "keep"


@pytest.mark.parametrize(
    "target", ["mailto:hi@example.com", "#section-2", "/relative"]
)
def test_non_http_targets_are_never_kept(target: str) -> None:
    assert classify_link("write to the team", target, PAGE, set()) == "unwrap"
