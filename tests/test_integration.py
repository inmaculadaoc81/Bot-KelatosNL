"""Integration tests – exercise the full webhook → response pipeline.

These tests hit the real FastAPI endpoints via httpx.AsyncClient, with only
external I/O mocked (OpenAI, Google Sheets, WhatsApp, Chatwoot, Calendar).
The SQLite database is real (tmp_path) so we verify actual DB persistence.
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from httpx import AsyncClient, ASGITransport

from tests.conftest import make_completion


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _whatsapp_webhook_body(sender: str, text: str) -> dict:
    """Build a minimal Meta WhatsApp webhook payload."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": sender,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


def _chatwoot_webhook_body(
    conversation_id: int,
    content: str,
    phone: str = "34612345678",
    contact_id: int = 42,
) -> dict:
    """Build a minimal Chatwoot agent-bot webhook payload."""
    return {
        "event": "message_created",
        "message_type": "incoming",
        "content": content,
        "content_type": "text",
        "conversation": {
            "id": conversation_id,
            "contact_inbox": {"source_id": phone},
        },
        "sender": {"id": contact_id, "name": "Test User", "email": "", "phone_number": phone},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def _isolated_db(tmp_path):
    """Replace the global Database with one backed by a temp SQLite file."""
    import main
    from database import Database

    real_db = Database()
    real_db.db_path = str(tmp_path / "integration.db")
    await real_db.init()

    original_db = main.db
    main.db = real_db
    yield real_db
    main.db = original_db
    await real_db.close()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset the IP rate limiter between tests to avoid cross-test 429s."""
    import main
    main.limiter.reset()
    yield


@pytest.fixture(autouse=True)
def _mock_external_services():
    """Patch every external-I/O service so nothing leaves the test process."""
    with (
        patch("main.whatsapp_svc") as wa,
        patch("main.chatwoot_svc") as cw,
        patch("main.sheets_svc") as sh,
        patch("main.espocrm_svc") as espo,
        patch("main.calendar_svc") as cal,
    ):
        wa.send_message = AsyncMock(return_value={"messages": [{"id": "msg1"}]})

        cw.send_message = AsyncMock(return_value={"id": 1})
        cw.get_contact_phone = AsyncMock(return_value="34612345678")
        cw.find_conversation_by_phone = AsyncMock(return_value=999)
        cw.handoff_to_agent = AsyncMock(return_value={"status": "open"})
        cw.assign_handoff_agent = AsyncMock(return_value=13)

        sh.get_repairs_by_phone = AsyncMock(return_value=[])
        sh.get_all_prices = AsyncMock(return_value=[])
        sh.format_repairs_for_prompt = MagicMock(return_value="[REPAIRS]")
        sh.format_prices_for_prompt = MagicMock(return_value="[PRICES]")

        espo.create_lead = AsyncMock(return_value="lead-42")
        espo.schedule_lead_from_conversation = MagicMock()

        cal.get_appointment_context = MagicMock(return_value="[APPOINTMENT CONTEXT]")
        cal.create_event = AsyncMock(return_value={"id": "evt1"})

        yield {
            "whatsapp": wa,
            "chatwoot": cw,
            "sheets": sh,
            "espocrm": espo,
            "calendar": cal,
        }


@pytest.fixture
def mock_intent():
    """Patch classify_intent to return a controllable result."""
    with patch("main.classify_intent", new_callable=AsyncMock) as mock:
        from intent_classifier import IntentResult
        mock.return_value = IntentResult()
        yield mock


@pytest.fixture
def mock_openai_generate():
    """Patch openai_svc.generate_response to return a controllable string."""
    with patch("main.openai_svc") as svc:
        svc.generate_response = AsyncMock(return_value="Hola, bienvenido a Kelatos.")
        svc.client = MagicMock()
        yield svc


@pytest.fixture
async def client():
    """httpx AsyncClient wired to the FastAPI app (no real server)."""
    from main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ===========================================================================
# WhatsApp Webhook – full pipeline
# ===========================================================================


class TestWhatsAppWebhookFlow:
    """Integration: POST /webhook with a WhatsApp text message."""

    async def test_basic_text_message(self, client, mock_intent, mock_openai_generate, _isolated_db, _mock_external_services):
        """Message arrives → intent classified → AI responds → WhatsApp sends → DB saved."""
        mock_openai_generate.generate_response.return_value = "Hola, te puedo ayudar."

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "hola"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # AI was called
        mock_openai_generate.generate_response.assert_called_once()

        # WhatsApp received the reply
        _mock_external_services["whatsapp"].send_message.assert_called_once_with(
            to="34600111222", text="Hola, te puedo ayudar."
        )

        # Messages persisted in DB (user + assistant)
        history = await _isolated_db.get_history("34600111222")
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    async def test_repair_intent_injects_context(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """When intent.needs_repair_lookup, sheets data is fetched and injected."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_repair_lookup=True)
        _mock_external_services["sheets"].get_repairs_by_phone.return_value = [
            {"resguardo": "R001", "equipo_modelo": "HP Pavilion", "estado": "En reparación"}
        ]

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "como va mi reparacion"))

        assert resp.status_code == 200
        _mock_external_services["sheets"].get_repairs_by_phone.assert_called_once_with("34600111222")
        _mock_external_services["sheets"].format_repairs_for_prompt.assert_called_once()

        # extra_context passed to generate_response
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert call_kwargs["extra_context"] is not None
        assert "[REPAIRS]" in call_kwargs["extra_context"]

    async def test_prices_intent_injects_context(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """When intent.needs_prices, price data is fetched and injected."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_prices=True)
        _mock_external_services["sheets"].get_all_prices.return_value = [{"marca": "HP"}]

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "cuanto cuesta"))

        assert resp.status_code == 200
        _mock_external_services["sheets"].get_all_prices.assert_called_once()
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert "[PRICES]" in call_kwargs["extra_context"]

    async def test_appointment_intent_injects_calendar_context(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """When intent.wants_appointment, calendar context is injected."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(wants_appointment=True)

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "quiero una cita"))

        assert resp.status_code == 200
        _mock_external_services["calendar"].get_appointment_context.assert_called_once()
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert "[APPOINTMENT CONTEXT]" in call_kwargs["extra_context"]

    async def test_combined_intents(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """Multiple intents combine their context."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(
            needs_repair_lookup=True, needs_prices=True, wants_appointment=True
        )
        _mock_external_services["sheets"].get_repairs_by_phone.return_value = [{"resguardo": "R001"}]
        _mock_external_services["sheets"].get_all_prices.return_value = [{"marca": "HP"}]

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "todo"))

        assert resp.status_code == 200
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        ctx = call_kwargs["extra_context"]
        assert "[REPAIRS]" in ctx
        assert "[PRICES]" in ctx
        assert "[APPOINTMENT CONTEXT]" in ctx

    async def test_brand_faq_loaded(self, client, mock_intent, mock_openai_generate):
        """When intent has a brand, load_brand_faq is called and passed to generate."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(brand="dyson")

        with patch("main.load_brand_faq", return_value="Dyson FAQ content") as mock_faq:
            resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "mi dyson"))

        assert resp.status_code == 200
        mock_faq.assert_called_once_with("dyson")
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert call_kwargs["brand_faq"] == "Dyson FAQ content"

    async def test_resguardo_lookup_when_no_phone_match(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """When no repairs by phone, a resguardo number in message triggers resguardo lookup."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_repair_lookup=True)
        _mock_external_services["sheets"].get_repairs_by_phone.return_value = []
        _mock_external_services["sheets"].get_repair_by_resguardo = AsyncMock(
            return_value={"resguardo": "17058", "equipo_modelo": "HP Pavilion", "estado": "En reparación"}
        )

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "mi resguardo es 17058"))

        assert resp.status_code == 200
        _mock_external_services["sheets"].get_repair_by_resguardo.assert_called_once_with("17058", "34600111222")
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert call_kwargs["extra_context"] is not None

    async def test_resguardo_wrong_phone_security_message(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """When resguardo doesn't belong to caller, security message is injected."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_repair_lookup=True)
        _mock_external_services["sheets"].get_repairs_by_phone.return_value = []
        _mock_external_services["sheets"].get_repair_by_resguardo = AsyncMock(return_value=None)

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "17058"))

        assert resp.status_code == 200
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert "seguridad" in call_kwargs["extra_context"]

    async def test_no_repairs_at_all_security_message(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """When no repairs found by phone and no resguardo in message, security message shown."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_repair_lookup=True)
        _mock_external_services["sheets"].get_repairs_by_phone.return_value = []

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "quiero saber el estado"))

        assert resp.status_code == 200
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert "seguridad" in call_kwargs["extra_context"]
        assert "numero de movil registrado" in call_kwargs["extra_context"]

    async def test_cita_creates_calendar_event(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """AI responds with CONFIRMAR_CITA → calendar event created, clean response sent."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(wants_appointment=True)
        mock_openai_generate.generate_response.return_value = (
            "Tu cita queda registrada.\nCONFIRMAR_CITA|2026-04-01T10:00:00+02:00|Juan Garcia|Reparacion portatil"
        )

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "quiero cita manana"))

        assert resp.status_code == 200

        # Calendar event was created
        _mock_external_services["calendar"].create_event.assert_called_once()
        call_kwargs = _mock_external_services["calendar"].create_event.call_args.kwargs
        assert call_kwargs["title"] == "Cita: Juan Garcia - Reparacion portatil"

        # WhatsApp received clean response (no CONFIRMAR_CITA line)
        wa_text = _mock_external_services["whatsapp"].send_message.call_args.kwargs["text"]
        assert "CONFIRMAR_CITA" not in wa_text
        assert "Tu cita queda registrada." in wa_text

    async def test_envio_creates_calendar_event_with_address(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """AI responds with CONFIRMAR_ENVIO → calendar event with address and cost."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(wants_appointment=True)
        mock_openai_generate.generate_response.return_value = (
            "Recogida confirmada.\n"
            "CONFIRMAR_ENVIO|2026-04-01T10:00:00+02:00|Maria Lopez|Dyson V15|Calle Gran Via 10, Madrid"
        )

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "quiero envio"))

        assert resp.status_code == 200

        call_kwargs = _mock_external_services["calendar"].create_event.call_args.kwargs
        assert call_kwargs["title"] == "Envío: Maria Lopez - Dyson V15"
        assert "Dirección: Calle Gran Via 10, Madrid" in call_kwargs["description"]
        assert "15€" in call_kwargs["description"]

        wa_text = _mock_external_services["whatsapp"].send_message.call_args.kwargs["text"]
        assert "CONFIRMAR_ENVIO" not in wa_text

    async def test_calendar_failure_appends_error(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """If calendar event creation fails, user gets error message appended."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(wants_appointment=True)
        mock_openai_generate.generate_response.return_value = (
            "Ok.\nCONFIRMAR_CITA|2026-04-01T10:00:00+02:00|Juan|Repair"
        )
        _mock_external_services["calendar"].create_event.return_value = None

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "cita"))

        wa_text = _mock_external_services["whatsapp"].send_message.call_args.kwargs["text"]
        assert "Hubo un problema" in wa_text

    async def test_non_text_message_ignored(self, client, _mock_external_services):
        """Non-text messages get a polite rejection."""
        body = {
            "entry": [{"changes": [{"value": {"messages": [{"from": "34600111222", "type": "image"}]}}]}]
        }
        resp = await client.post("/webhook", json=body)

        assert resp.status_code == 200
        assert resp.json()["status"] == "non-text ignored"
        _mock_external_services["whatsapp"].send_message.assert_called_once()

    async def test_empty_webhook_body(self, client):
        """Webhook with no entry returns gracefully."""
        resp = await client.post("/webhook", json={"entry": []})
        assert resp.status_code == 200
        assert resp.json()["status"] == "no entry"

    async def test_status_update_no_messages(self, client):
        """Webhook status update (no messages) handled gracefully."""
        body = {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
        resp = await client.post("/webhook", json=body)
        assert resp.status_code == 200
        assert resp.json()["status"] == "no messages"


# ===========================================================================
# Human mode
# ===========================================================================


class TestHumanMode:
    async def test_human_mode_skips_ai(self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services):
        """When conversation is in human mode, message is saved but AI is not called."""
        await _isolated_db.set_conversation_mode("34600111222", "human")

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "hola"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "human mode"
        mock_openai_generate.generate_response.assert_not_called()
        _mock_external_services["whatsapp"].send_message.assert_not_called()

        # Message was still saved
        history = await _isolated_db.get_history("34600111222")
        assert len(history) == 1
        assert history[0]["content"] == "hola"


# ===========================================================================
# Rate limiting (per-phone)
# ===========================================================================


class TestPhoneRateLimit:
    async def test_rate_limit_blocks_after_threshold(self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services):
        """After 10 messages in 60s, subsequent messages are rate-limited."""
        # Simulate 10 prior messages
        for i in range(10):
            await _isolated_db.save_message("34600111222", "user", f"msg {i}")

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "one more"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "phone_rate_limited"
        mock_openai_generate.generate_response.assert_not_called()


# ===========================================================================
# Chatwoot Webhook – full pipeline
# ===========================================================================


class TestChatwootWebhookFlow:
    async def test_basic_chatwoot_message(self, client, mock_intent, mock_openai_generate, _isolated_db, _mock_external_services):
        """Chatwoot incoming message → AI responds → Chatwoot reply sent → DB saved."""
        mock_openai_generate.generate_response.return_value = "Hola desde Chatwoot."

        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(101, "hola"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Chatwoot received the reply
        _mock_external_services["chatwoot"].send_message.assert_called_once_with(101, "Hola desde Chatwoot.")

        # DB has messages under the chatwoot sender key
        history = await _isolated_db.get_history("chatwoot_101")
        assert len(history) == 2

    async def test_chatwoot_first_message_schedules_espocrm_lead(self, client, mock_intent, mock_openai_generate, _isolated_db, _mock_external_services):
        """First message schedules a delayed EspoCRM lead (10-min volcado)."""
        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(102, "necesito reparar mi portatil"))

        assert resp.status_code == 200
        _mock_external_services["espocrm"].schedule_lead_from_conversation.assert_called_once()
        call_kwargs = _mock_external_services["espocrm"].schedule_lead_from_conversation.call_args.kwargs
        assert "lead_label" in call_kwargs

    async def test_chatwoot_second_message_no_lead(self, client, mock_intent, mock_openai_generate, _isolated_db, _mock_external_services):
        """Second message does not schedule a new EspoCRM lead."""
        # Pre-populate history so it looks like a returning conversation
        await _isolated_db.save_message("chatwoot_103", "user", "previous msg")

        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(103, "hola otra vez"))

        assert resp.status_code == 200
        _mock_external_services["espocrm"].schedule_lead_from_conversation.assert_not_called()

    async def test_chatwoot_repair_intent_uses_phone_from_source(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """Repair lookup uses phone from contact_inbox.source_id."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_repair_lookup=True)
        _mock_external_services["sheets"].get_repairs_by_phone.return_value = [{"resguardo": "R100"}]

        body = _chatwoot_webhook_body(104, "como va mi reparacion", phone="34699887766")
        resp = await client.post("/chatwoot/webhook", json=body)

        assert resp.status_code == 200
        _mock_external_services["sheets"].get_repairs_by_phone.assert_called_once_with("34699887766")

    async def test_chatwoot_envio_with_calendar(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """Chatwoot flow also handles CONFIRMAR_ENVIO correctly."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(wants_appointment=True)
        mock_openai_generate.generate_response.return_value = (
            "Listo.\nCONFIRMAR_ENVIO|2026-04-01T10:00:00+02:00|Ana|MacBook|Calle Sol 5, Madrid"
        )

        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(105, "quiero envio"))

        assert resp.status_code == 200
        _mock_external_services["calendar"].create_event.assert_called_once()
        call_kwargs = _mock_external_services["calendar"].create_event.call_args.kwargs
        assert "Envío" in call_kwargs["title"]

        # Chatwoot got clean response
        cw_text = _mock_external_services["chatwoot"].send_message.call_args[0][1]
        assert "CONFIRMAR_ENVIO" not in cw_text

    async def test_chatwoot_ignores_non_incoming(self, client, _mock_external_services):
        """Chatwoot outgoing/activity events are ignored."""
        body = {"event": "message_created", "message_type": "outgoing", "content": "hi"}
        resp = await client.post("/chatwoot/webhook", json=body)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    async def test_chatwoot_human_mode(self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services):
        """Chatwoot conversation in human mode saves message but skips AI."""
        await _isolated_db.set_conversation_mode("chatwoot_106", "human")

        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(106, "ayuda"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "human mode"
        mock_openai_generate.generate_response.assert_not_called()

    async def test_bot_own_outgoing_message_ignored(self, client, _isolated_db, _mock_external_services):
        """Bot's own outgoing messages should NOT activate human mode."""
        body = {
            "event": "message_created",
            "message_type": "outgoing",
            "content": "Hola, bienvenido a Kelatos",
            "sender": {"id": 1, "name": "Kelatos Bot", "type": "agent_bot"},
            "conversation": {
                "id": 109,
                "contact_inbox": {"source_id": "34600999666"},
            },
        }
        resp = await client.post("/chatwoot/webhook", json=body)

        assert resp.status_code == 200
        assert resp.json()["status"] == "bot_echo_ignored"

        # Mode should still be bot (default)
        mode_cw = await _isolated_db.get_conversation_mode("chatwoot_109")
        assert mode_cw == "bot"
        mode_phone = await _isolated_db.get_conversation_mode("34600999666")
        assert mode_phone == "bot"

    async def test_agent_outgoing_message_activates_human_mode(self, client, _isolated_db, _mock_external_services):
        """When an agent sends a message from Chatwoot, bot switches to human mode."""
        body = {
            "event": "message_created",
            "message_type": "outgoing",
            "content": "Hola, soy el tecnico",
            "sender": {"id": 12, "name": "Cielo", "type": "user"},
            "conversation": {
                "id": 107,
                "contact_inbox": {"source_id": "34600999888"},
            },
        }
        resp = await client.post("/chatwoot/webhook", json=body)

        assert resp.status_code == 200
        assert resp.json()["status"] == "agent_mode"

        # Both keys set to human
        mode_cw = await _isolated_db.get_conversation_mode("chatwoot_107")
        assert mode_cw == "human"
        mode_phone = await _isolated_db.get_conversation_mode("34600999888")
        assert mode_phone == "human"

    async def test_agent_outgoing_then_client_reply_bot_silent(self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services):
        """After agent writes from Chatwoot, client reply via WhatsApp doesn't trigger bot."""
        # Agent writes
        body = {
            "event": "message_created",
            "message_type": "outgoing",
            "content": "Hola, te informo sobre tu equipo",
            "sender": {"id": 12, "name": "Cielo", "type": "user"},
            "conversation": {
                "id": 108,
                "contact_inbox": {"source_id": "34600999777"},
            },
        }
        await client.post("/chatwoot/webhook", json=body)

        # Client replies via WhatsApp
        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600999777", "gracias"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "human mode"
        mock_openai_generate.generate_response.assert_not_called()


# ===========================================================================
# Conversation history persistence
# ===========================================================================


class TestHistoryPersistence:
    async def test_history_passed_to_ai(self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services):
        """Previous messages are loaded and passed to generate_response."""
        # Simulate prior conversation
        await _isolated_db.save_message("34600111222", "user", "msg anterior")
        await _isolated_db.save_message("34600111222", "assistant", "respuesta anterior")

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "y ahora?"))

        assert resp.status_code == 200
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        history = call_kwargs["history"]
        assert len(history) == 2
        assert history[0]["content"] == "msg anterior"
        assert history[1]["content"] == "respuesta anterior"

    async def test_multiple_messages_accumulate(self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services):
        """Each message round-trip adds to the history."""
        mock_openai_generate.generate_response.return_value = "resp1"
        await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "msg1"))

        mock_openai_generate.generate_response.return_value = "resp2"
        await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "msg2"))

        history = await _isolated_db.get_history("34600111222")
        assert len(history) == 4  # user+assistant × 2
        assert history[0]["content"] == "msg1"
        assert history[1]["content"] == "resp1"
        assert history[2]["content"] == "msg2"
        assert history[3]["content"] == "resp2"


# ===========================================================================
# Webhook verification (GET /webhook)
# ===========================================================================


class TestWebhookVerification:
    async def test_valid_verification(self, client):
        from config import settings
        resp = await client.get("/webhook", params={
            "hub.mode": "subscribe",
            "hub.verify_token": settings.VERIFY_TOKEN,
            "hub.challenge": "test_challenge_123",
        })
        assert resp.status_code == 200
        assert resp.text == "test_challenge_123"

    async def test_invalid_token_rejected(self, client):
        resp = await client.get("/webhook", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong_token",
            "hub.challenge": "test_challenge_123",
        })
        assert resp.status_code == 403


# ===========================================================================
# Health check
# ===========================================================================


class TestHealthCheck:
    async def test_health_endpoint(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ===========================================================================
# Error resilience
# ===========================================================================


class TestErrorResilience:
    async def test_sheets_error_does_not_break_flow(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """If sheets throws, the message flow still completes (without repair data)."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_repair_lookup=True)
        _mock_external_services["sheets"].get_repairs_by_phone.side_effect = Exception("Sheets API down")

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "mi reparacion"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        # AI was still called, just without extra context
        mock_openai_generate.generate_response.assert_called_once()

    async def test_whatsapp_send_error_does_not_crash(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """If WhatsApp send fails, endpoint still returns 200 (error caught)."""
        _mock_external_services["whatsapp"].send_message.side_effect = Exception("WA API down")

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "hola"))

        # The endpoint catches all exceptions and returns status: error
        assert resp.status_code == 200


# ===========================================================================
# Handoff to human agent
# ===========================================================================


class TestHandoff:
    @pytest.fixture(autouse=True)
    def _force_business_hours(self, mocker):
        """Ensure handoff tests always run as if within business hours."""
        mocker.patch("main.is_within_business_hours", return_value=True)

    async def test_whatsapp_handoff_sends_message_and_sets_human_mode(
        self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services
    ):
        """When needs_human=True, bot sends handoff message, sets mode to human, calls Chatwoot handoff."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_human=True)

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "quiero hablar con un tecnico"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "handoff"

        # Handoff message sent via WhatsApp
        wa_text = _mock_external_services["whatsapp"].send_message.call_args.kwargs["text"]
        assert "transfiero" in wa_text.lower()

        # Chatwoot searched for conversation and called handoff
        _mock_external_services["chatwoot"].find_conversation_by_phone.assert_called_once_with("34600111222")
        _mock_external_services["chatwoot"].handoff_to_agent.assert_called_once_with(999)

        # Mode set to human
        mode = await _isolated_db.get_conversation_mode("34600111222")
        assert mode == "human"

        # AI was NOT called
        mock_openai_generate.generate_response.assert_not_called()

    async def test_whatsapp_handoff_message_saved_to_history(
        self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services
    ):
        """Handoff message is persisted in chat history."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_human=True)

        await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "pasame con alguien"))

        history = await _isolated_db.get_history("34600111222")
        # user message + handoff assistant message
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert "transfiero" in history[1]["content"].lower()

    async def test_chatwoot_handoff(
        self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services
    ):
        """Chatwoot webhook also handles handoff correctly."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_human=True)

        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(201, "necesito hablar con alguien"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "handoff"

        # Message sent via Chatwoot
        cw_args = _mock_external_services["chatwoot"].send_message.call_args
        assert "transfiero" in cw_args[0][1].lower()

        # Handoff called with the conversation_id from the webhook
        _mock_external_services["chatwoot"].handoff_to_agent.assert_called_once_with(201)

        # Mode set to human
        mode = await _isolated_db.get_conversation_mode("chatwoot_201")
        assert mode == "human"

    async def test_after_handoff_bot_silent(
        self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services
    ):
        """After handoff, subsequent messages don't trigger AI."""
        from intent_classifier import IntentResult

        # First: handoff
        mock_intent.return_value = IntentResult(needs_human=True)
        await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "quiero hablar con alguien"))

        # Reset mocks
        mock_openai_generate.generate_response.reset_mock()
        _mock_external_services["whatsapp"].send_message.reset_mock()

        # Second: another message — should be silent
        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "hola sigues ahi?"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "human mode"
        mock_openai_generate.generate_response.assert_not_called()

    async def test_conversation_resolved_restores_bot_mode(
        self, client, _isolated_db, _mock_external_services
    ):
        """Chatwoot conversation_status_changed resolved → bot mode restored."""
        # Set up human mode
        await _isolated_db.set_conversation_mode("chatwoot_301", "human")

        body = {
            "event": "conversation_status_changed",
            "status": "resolved",
            "id": 301,
            "contact_inbox": {"source_id": "34600111222"},
        }
        resp = await client.post("/chatwoot/webhook", json=body)

        assert resp.status_code == 200
        assert resp.json()["status"] == "bot_restored"

        # Both keys restored to bot
        mode_cw = await _isolated_db.get_conversation_mode("chatwoot_301")
        assert mode_cw == "bot"
        mode_phone = await _isolated_db.get_conversation_mode("34600111222")
        assert mode_phone == "bot"

    async def test_conversation_resolved_without_phone(
        self, client, _isolated_db, _mock_external_services
    ):
        """Resolved event without source_id still restores chatwoot key."""
        await _isolated_db.set_conversation_mode("chatwoot_302", "human")

        body = {
            "event": "conversation_status_changed",
            "status": "resolved",
            "id": 302,
        }
        resp = await client.post("/chatwoot/webhook", json=body)

        assert resp.status_code == 200
        mode = await _isolated_db.get_conversation_mode("chatwoot_302")
        assert mode == "bot"

    async def test_handoff_chatwoot_failure_still_sets_human_mode(
        self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services
    ):
        """Even if Chatwoot handoff API fails, mode is still set to human."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_human=True)
        _mock_external_services["chatwoot"].handoff_to_agent.side_effect = Exception("Chatwoot down")

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "pasame con un tecnico"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "handoff"

        # Mode still set to human despite Chatwoot failure
        mode = await _isolated_db.get_conversation_mode("34600111222")
        assert mode == "human"
