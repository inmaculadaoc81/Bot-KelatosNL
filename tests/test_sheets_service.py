"""Tests for sheets_service.py - pure functions and formatting."""
from sheets_service import normalize_phone, phones_match, _is_active, _extract_repair, SheetsService


# ── normalize_phone ──────────────────────────────────────────────────

class TestNormalizePhone:
    def test_with_plus(self):
        assert normalize_phone("+34612345678") == "34612345678"

    def test_with_spaces(self):
        assert normalize_phone("34 612 345 678") == "34612345678"

    def test_with_dashes(self):
        assert normalize_phone("34-612-345-678") == "34612345678"

    def test_leading_zeros(self):
        assert normalize_phone("0034612345678") == "34612345678"

    def test_already_clean(self):
        assert normalize_phone("34612345678") == "34612345678"

    def test_empty(self):
        assert normalize_phone("") == ""

    def test_combined(self):
        assert normalize_phone("+00 34-612 345 678") == "34612345678"


# ── phones_match ─────────────────────────────────────────────────────

class TestPhonesMatch:
    def test_exact_match(self):
        assert phones_match("34612345678", "34612345678") is True

    def test_with_plus(self):
        assert phones_match("+34612345678", "34612345678") is True

    def test_without_country_code(self):
        assert phones_match("650086734", "+34650086734") is True

    def test_without_country_code_reversed(self):
        assert phones_match("+34650086734", "650086734") is True

    def test_both_without_country_code(self):
        assert phones_match("650086734", "650086734") is True

    def test_with_spaces_and_dashes(self):
        assert phones_match("+34 650 086 734", "650-086-734") is True

    def test_different_numbers(self):
        assert phones_match("34612345678", "34699887766") is False

    def test_empty(self):
        assert phones_match("", "34612345678") is False

    def test_both_empty(self):
        assert phones_match("", "") is False


# ── _is_active ───────────────────────────────────────────────────────

class TestIsActive:
    def test_pendiente_is_active(self):
        assert _is_active({"estado_entrega": "PENDIENTE"}) is True

    def test_envio_is_active(self):
        assert _is_active({"estado_entrega": "ENVIO"}) is True

    def test_entregado_not_active(self):
        assert _is_active({"estado_entrega": "ENTREGADO"}) is False

    def test_reciclaje_not_active(self):
        assert _is_active({"estado_entrega": "RECICLAJE"}) is False

    def test_empty_dict_is_active(self):
        assert _is_active({}) is True

    def test_case_insensitive(self):
        assert _is_active({"estado_entrega": "entregado"}) is False

    def test_with_whitespace(self):
        assert _is_active({"estado_entrega": " ENTREGADO "}) is False


# ── _extract_repair ──────────────────────────────────────────────────

class TestExtractRepair:
    def test_filters_to_safe_columns(self):
        record = {
            "resguardo": "R001",
            "equipo_modelo": "Dyson V15",
            "sintoma": "No aspira",
            "estado": "En Reparacion",
            "cliente_telefono": "34612345678",  # should be excluded
            "cliente_email": "test@test.com",   # should be excluded
        }
        result = _extract_repair(record)
        assert "resguardo" in result
        assert "equipo_modelo" in result
        assert "cliente_telefono" not in result
        assert "cliente_email" not in result

    def test_skips_empty_values(self):
        record = {"resguardo": "R001", "sintoma": "", "estado": "  "}
        result = _extract_repair(record)
        assert "resguardo" in result
        assert "sintoma" not in result
        assert "estado" not in result

    def test_strips_whitespace(self):
        record = {"resguardo": "  R001  ", "equipo_modelo": " Dyson V15 "}
        result = _extract_repair(record)
        assert result["resguardo"] == "R001"
        assert result["equipo_modelo"] == "Dyson V15"


# ── format_repairs_for_prompt ────────────────────────────────────────

class TestFormatRepairs:
    def setup_method(self):
        self.svc = SheetsService()

    def test_empty_returns_empty(self):
        assert self.svc.format_repairs_for_prompt([]) == ""

    def test_active_repair_shown(self):
        repairs = [{"resguardo": "R001", "equipo_modelo": "HP Laptop", "sintoma": "No enciende", "estado": "En Reparacion", "estado_entrega": "PENDIENTE"}]
        result = self.svc.format_repairs_for_prompt(repairs)
        assert "REPARACIONES ACTIVAS (1)" in result
        assert "HP Laptop" in result
        assert "No enciende" in result

    def test_closed_repair_in_historial(self):
        repairs = [{"resguardo": "R001", "equipo_modelo": "HP Laptop", "estado": "Reparado", "estado_entrega": "ENTREGADO"}]
        result = self.svc.format_repairs_for_prompt(repairs)
        assert "No tiene reparaciones activas" in result
        assert "REPARACIONES ANTERIORES FINALIZADAS: 1" in result

    def test_mixed_repairs(self):
        repairs = [
            {"resguardo": "R001", "equipo_modelo": "HP", "estado": "En Reparacion", "estado_entrega": "PENDIENTE"},
            {"resguardo": "R002", "equipo_modelo": "Dell", "estado": "Reparado", "estado_entrega": "ENTREGADO"},
        ]
        result = self.svc.format_repairs_for_prompt(repairs)
        assert "REPARACIONES ACTIVAS (1)" in result
        assert "REPARACIONES ANTERIORES FINALIZADAS: 1" in result

    def test_envio_shows_en_camino(self):
        repairs = [{"resguardo": "R001", "equipo_modelo": "HP", "estado": "Reparado", "estado_entrega": "ENVIO"}]
        result = self.svc.format_repairs_for_prompt(repairs)
        assert "EN CAMINO" in result


# ── format_prices_for_prompt ─────────────────────────────────────────

class TestFormatPrices:
    def setup_method(self):
        self.svc = SheetsService()

    def test_empty_returns_empty(self):
        assert self.svc.format_prices_for_prompt([]) == ""

    def test_available_item(self):
        prices = [{"marca": "Samsung", "modelo": "S24", "tipo_reparacion": "Pantalla", "precio": "150", "disponible": "si"}]
        result = self.svc.format_prices_for_prompt(prices)
        assert "DISPONIBLE" in result
        assert "Samsung S24" in result
        assert "150" in result

    def test_not_available_item(self):
        prices = [{"marca": "Samsung", "modelo": "S24", "tipo_reparacion": "Pantalla", "precio": "150", "disponible": "no"}]
        result = self.svc.format_prices_for_prompt(prices)
        assert "NO DISPONIBLE" in result

    def test_header_present(self):
        prices = [{"marca": "X", "modelo": "Y", "tipo_reparacion": "Z", "precio": "10", "disponible": "si"}]
        result = self.svc.format_prices_for_prompt(prices)
        assert result.startswith("[TABLA DE PRECIOS DE REPARACIONES]")
