"""Tests for main.py - _process_appointment function."""
import pytest
from unittest.mock import patch, AsyncMock


class TestProcessAppointment:
    @pytest.fixture
    def mock_calendar(self):
        with patch("main.calendar_svc") as mock:
            mock.create_event = AsyncMock()
            yield mock

    async def test_cita_basic(self, mock_calendar):
        from main import _process_appointment

        mock_calendar.create_event.return_value = {"id": "evt1"}
        ai_response = "Tu cita queda registrada.\nCONFIRMAR_CITA|2026-04-01T10:00:00+02:00|Juan Garcia|Reparacion portatil"

        clean, original = await _process_appointment(ai_response, "34612345678")

        assert clean == "Tu cita queda registrada."
        assert "CONFIRMAR_CITA" not in clean
        mock_calendar.create_event.assert_called_once()
        call_kwargs = mock_calendar.create_event.call_args.kwargs
        assert call_kwargs["title"] == "Cita: Juan Garcia - Reparacion portatil"

    async def test_envio_basic(self, mock_calendar):
        from main import _process_appointment

        mock_calendar.create_event.return_value = {"id": "evt1"}
        ai_response = "Recogida confirmada.\nCONFIRMAR_ENVIO|2026-04-01T10:00:00+02:00|Maria Lopez|Dyson V15|Calle Gran Via 10, 28013 Madrid"

        clean, original = await _process_appointment(ai_response, "34612345678")

        assert clean == "Recogida confirmada."
        assert "CONFIRMAR_ENVIO" not in clean
        call_kwargs = mock_calendar.create_event.call_args.kwargs
        assert call_kwargs["title"] == "Envío: Maria Lopez - Dyson V15"
        assert "Dirección: Calle Gran Via 10, 28013 Madrid" in call_kwargs["description"]
        assert "15€" in call_kwargs["description"]

    async def test_cita_missing_optional_parts(self, mock_calendar):
        from main import _process_appointment

        mock_calendar.create_event.return_value = {"id": "evt1"}
        ai_response = "Ok.\nCONFIRMAR_CITA|2026-04-01T10:00:00+02:00"

        clean, original = await _process_appointment(ai_response, "34612345678")

        call_kwargs = mock_calendar.create_event.call_args.kwargs
        assert call_kwargs["title"] == "Cita: Cliente - Servicio técnico"

    async def test_no_confirmation_line(self, mock_calendar):
        from main import _process_appointment

        ai_response = "Hola, te puedo ayudar con tu equipo."

        clean, original = await _process_appointment(ai_response, "34612345678")

        assert clean == "Hola, te puedo ayudar con tu equipo."
        mock_calendar.create_event.assert_not_called()

    async def test_event_creation_fails(self, mock_calendar):
        from main import _process_appointment

        mock_calendar.create_event.return_value = None
        ai_response = "Ok.\nCONFIRMAR_CITA|2026-04-01T10:00:00+02:00|Juan|Repair"

        clean, original = await _process_appointment(ai_response, "34612345678")

        assert "Hubo un problema" in clean

    async def test_event_creation_exception(self, mock_calendar):
        from main import _process_appointment

        mock_calendar.create_event.side_effect = Exception("API error")
        ai_response = "Ok.\nCONFIRMAR_CITA|2026-04-01T10:00:00+02:00|Juan|Repair"

        clean, original = await _process_appointment(ai_response, "34612345678")

        assert "Hubo un problema" in clean

    async def test_confirm_line_in_middle(self, mock_calendar):
        from main import _process_appointment

        mock_calendar.create_event.return_value = {"id": "evt1"}
        ai_response = "Linea 1.\nCONFIRMAR_CITA|2026-04-01T10:00:00+02:00|Juan|HP\nLinea 3."

        clean, original = await _process_appointment(ai_response, "34612345678")

        assert clean == "Linea 1.\nLinea 3."
        assert "CONFIRMAR_CITA" not in clean

    async def test_envio_includes_phone(self, mock_calendar):
        from main import _process_appointment

        mock_calendar.create_event.return_value = {"id": "evt1"}
        ai_response = "Ok.\nCONFIRMAR_ENVIO|2026-04-01T10:00:00+02:00|Maria|Dyson|Calle Mayor 1"

        await _process_appointment(ai_response, "34612345678")

        call_kwargs = mock_calendar.create_event.call_args.kwargs
        assert call_kwargs["attendee_phone"] == "34612345678"
