"""Shared test fixtures."""
import pytest
from unittest.mock import MagicMock, AsyncMock

from database import Database


@pytest.fixture
async def db(tmp_path):
    """Provide a real SQLite database in a temp directory."""
    instance = Database()
    instance.db_path = str(tmp_path / "test.db")
    await instance.init()
    yield instance
    await instance.close()


@pytest.fixture
def mock_openai_client():
    """Mock AsyncOpenAI client that returns controllable responses."""
    client = AsyncMock()
    return client


def make_completion(json_str: str):
    """Build a mock ChatCompletion with the given content string."""
    msg = MagicMock()
    msg.content = json_str
    choice = MagicMock()
    choice.message = msg
    response = MagicMock()
    response.choices = [choice]
    return response
