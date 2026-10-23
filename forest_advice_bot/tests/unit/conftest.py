import aiohttp
import pytest

from tests.fake_openrouter import FakeOpenRouter


@pytest.fixture
async def fake_api():
    fake = FakeOpenRouter()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def http_session():
    async with aiohttp.ClientSession() as session:
        yield session
