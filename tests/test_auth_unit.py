from pathlib import Path

import pytest

from app.auth import AuthConfigError, is_authorized, load_api_keys


def test_loads_trimmed_keys_and_ignores_comments(tmp_path: Path) -> None:
    path = tmp_path / "keys.txt"
    path.write_text("# comment\n\n first \nsecond\n", encoding="utf-8")

    assert load_api_keys(path) == (b"first", b"second")


def test_missing_key_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(AuthConfigError):
        load_api_keys(tmp_path / "missing.txt")


def test_empty_key_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "keys.txt"
    path.write_text("# only a comment\n\n", encoding="utf-8")

    with pytest.raises(AuthConfigError):
        load_api_keys(path)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer key", True),
        ("bearer key", True),
        ("Bearer   key  ", True),
        ("Basic key", False),
        ("Bearer wrong", False),
        ("Bearer ", False),
        ("", False),
        (None, False),
    ],
)
def test_authorization_header_check(
    header: str | None, expected: bool
) -> None:
    assert is_authorized(header, (b"key", b"other")) is expected
