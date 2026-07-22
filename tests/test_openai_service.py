"""Tests for openai_service.py - _strip_filler pure function."""
from openai_service import _strip_filler


class TestStripFiller:
    def test_removes_si_necesitas(self):
        text = "Tu cita esta lista. Si necesitas algo mas, no dudes en escribirnos."
        result = _strip_filler(text)
        assert "Si necesitas" not in result
        assert result == "Tu cita esta lista."

    def test_removes_no_dudes(self):
        text = "Listo! No dudes en contactarnos."
        result = _strip_filler(text)
        assert "No dudes" not in result
        assert "Listo" in result

    def test_removes_estoy_aqui(self):
        text = "El equipo esta en reparacion. Estoy aquí para ayudarte."
        result = _strip_filler(text)
        assert "Estoy aquí" not in result

    def test_removes_algo_mas(self):
        text = "Tu equipo esta listo. ¿Algo más en lo que pueda ayudarte?"
        result = _strip_filler(text)
        assert "Algo más" not in result

    def test_no_filler_unchanged(self):
        text = "Tu equipo esta en reparacion."
        result = _strip_filler(text)
        assert result == text

    def test_adds_period_when_missing(self):
        text = "Tu equipo esta listo. Si necesitas algo mas, dime"
        result = _strip_filler(text)
        assert result.endswith(".")

    def test_preserves_exclamation(self):
        text = "Perfecto!"
        result = _strip_filler(text)
        assert result == "Perfecto!"

    def test_preserves_question_mark(self):
        text = "Cual es el modelo?"
        result = _strip_filler(text)
        assert result == "Cual es el modelo?"

    def test_removes_quedo_a_tu_disposicion(self):
        text = "El diagnostico es gratuito. Quedo a tu disposición para lo que necesites."
        result = _strip_filler(text)
        assert "Quedo a tu disposición" not in result
