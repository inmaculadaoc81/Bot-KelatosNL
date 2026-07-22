"""Tests for intent_classifier.py - with mocked OpenAI client."""
import pytest
from unittest.mock import patch, AsyncMock

from intent_classifier import classify_intent, IntentResult
from tests.conftest import make_completion


class TestClassifyIntent:
    @pytest.fixture
    def client(self):
        c = AsyncMock()
        return c

    def _setup_response(self, client, json_str):
        client.chat.completions.create.return_value = make_completion(json_str)

    async def test_repair_lookup(self, client):
        self._setup_response(client, '{"needs_repair_lookup": true, "needs_prices": false, "wants_appointment": false, "brand": null}')
        result = await classify_intent(client, "como va mi reparacion")
        assert result.needs_repair_lookup is True
        assert result.needs_prices is False

    async def test_prices(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": true, "wants_appointment": false, "brand": null}')
        result = await classify_intent(client, "cuanto cuesta reparar una pantalla")
        assert result.needs_prices is True

    async def test_appointment(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": false, "wants_appointment": true, "brand": null}')
        result = await classify_intent(client, "quiero agendar una cita")
        assert result.wants_appointment is True

    async def test_valid_brand(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": false, "wants_appointment": false, "brand": "dyson"}')
        result = await classify_intent(client, "tengo un dyson que no funciona")
        assert result.brand == "dyson"

    async def test_unknown_brand_ignored(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": false, "wants_appointment": false, "brand": "nokia"}')
        result = await classify_intent(client, "tengo un nokia")
        assert result.brand is None

    async def test_brand_case_insensitive(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": false, "wants_appointment": false, "brand": "DYSON"}')
        result = await classify_intent(client, "mi DYSON")
        assert result.brand == "dyson"

    async def test_needs_human(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": false, "wants_appointment": false, "needs_human": true, "brand": null}')
        result = await classify_intent(client, "quiero hablar con un tecnico")
        assert result.needs_human is True

    async def test_multiple_intents(self, client):
        self._setup_response(client, '{"needs_repair_lookup": true, "needs_prices": true, "wants_appointment": true, "brand": "hp"}')
        result = await classify_intent(client, "cuanto cuesta y como va mi hp, quiero cita")
        assert result.needs_repair_lookup is True
        assert result.needs_prices is True
        assert result.wants_appointment is True
        assert result.brand == "hp"

    async def test_api_failure_returns_fail_open(self, client):
        client.chat.completions.create.side_effect = Exception("API error")
        result = await classify_intent(client, "hola")
        assert result.needs_repair_lookup is True
        assert result.needs_prices is True
        assert result.brand is None

    async def test_invalid_json_returns_fail_open(self, client):
        self._setup_response(client, "not valid json")
        result = await classify_intent(client, "hola")
        assert result.needs_repair_lookup is True
        assert result.needs_prices is True

    async def test_passes_history(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": false, "wants_appointment": false, "brand": null}')
        history = [
            {"role": "user", "content": "hola"},
            {"role": "assistant", "content": "bienvenido"},
            {"role": "user", "content": "tengo un problema"},
        ]
        await classify_intent(client, "con mi portatil", history=history)

        call_args = client.chat.completions.create.call_args
        messages = call_args.kwargs["messages"]
        # system + last 2 history + user message = 4 messages
        assert len(messages) == 4
        assert messages[0]["role"] == "system"
        assert messages[-1]["content"] == "con mi portatil"
