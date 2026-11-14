import logging
import secrets
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)


class AuthConfigError(RuntimeError):
    """Raised when the API key file cannot provide usable keys."""


def load_api_keys(path: Path) -> tuple[bytes, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AuthConfigError("API key file is unavailable") from exc

    keys = tuple(
        line.strip().encode("utf-8")
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not keys:
        raise AuthConfigError("API key file contains no usable keys")

    try:
        if path.stat().st_mode & 0o077:
            logger.warning("API key file permissions are broader than 0600")
    except OSError:
        pass
    return keys


def is_authorized(
    authorization: str | None, keys: Sequence[bytes]
) -> bool:
    if not authorization:
        return False
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    candidate = token.strip().encode("utf-8")
    if not candidate:
        return False
    matched = False
    for key in keys:
        matched |= secrets.compare_digest(candidate, key)
    return matched
