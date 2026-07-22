"""Tests for calendar_service.py - appointment context and event creation."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

from calendar_service import CalendarService, MADRID_TZ


class TestGetAppointmentContext:
    def setup_method(self):
        self.svc = CalendarService()

    def test_contains_schedule(self):
        result = self.svc.get_appointment_context()
        assert "Lunes a Viernes de 09:30 a 18:00" in result

    def test_contains_holidays_closed(self):
        result = self.svc.get_appointment_context()
        assert "festivos cerrados" in result

    def test_contains_confirmar_cita_format(self):
        result = self.svc.get_appointment_context()
        assert "CONFIRMAR_CITA|" in result

    def test_contains_confirmar_envio_format(self):
        result = self.svc.get_appointment_context()
        assert "CONFIRMAR_ENVIO|" in result

    def test_contains_envio_cost(self):
        result = self.svc.get_appointment_context()
        assert "15€" in result

    def test_contains_cita_vs_envio_distinction(self):
        result = self.svc.get_appointment_context()
        assert "CITA: el cliente viene al local" in result
        assert "ENVIO: se envia un mensajero" in result

    def test_contains_address_requirement_for_envio(self):
        result = self.svc.get_appointment_context()
        assert "direccion completa" in result

    def test_contains_current_date(self):
        result = self.svc.get_appointment_context()
        assert "Fecha actual:" in result


class TestCreateEvent:
    def setup_method(self):
        self.svc = CalendarService()

    async def test_success(self):
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.return_value = {"id": "evt1"}

        with patch.object(self.svc, "_get_service", return_value=mock_service):
            result = await self.svc.create_event(
                title="Cita: Juan - HP",
                start_iso="2026-04-01T10:00:00+02:00",
                description="Test",
                attendee_phone="34612345678",
            )
        assert result == {"id": "evt1"}

    async def test_failure_returns_none(self):
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.side_effect = Exception("API error")

        with patch.object(self.svc, "_get_service", return_value=mock_service):
            result = await self.svc.create_event(
                title="Cita: Juan - HP",
                start_iso="2026-04-01T10:00:00+02:00",
            )
        assert result is None

    async def test_end_time_is_30_minutes_after_start(self):
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.return_value = {"id": "evt1"}

        with patch.object(self.svc, "_get_service", return_value=mock_service):
            await self.svc.create_event(
                title="Test",
                start_iso="2026-04-01T10:00:00+02:00",
            )

        call_args = mock_service.events.return_value.insert.call_args
        body = call_args.kwargs["body"]
        start = datetime.fromisoformat(body["start"]["dateTime"])
        end = datetime.fromisoformat(body["end"]["dateTime"])
        assert (end - start).total_seconds() == 1800  # 30 minutes
