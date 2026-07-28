import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Use asyncio for async tests, matching the application runtime."""

    return "asyncio"
