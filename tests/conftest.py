from collections.abc import Iterator

import pytest

from tests.test_utils import running_site


@pytest.fixture(scope="session")
def site() -> Iterator[str]:
    with running_site() as url:
        yield url
