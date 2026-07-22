"""Tests for faq_service.py - FAQ loading and brand listing."""
import os
from unittest.mock import patch

from faq_service import load_brand_faq, load_general_faq, list_available_brands


class TestLoadBrandFaq:
    def test_existing_brand(self, tmp_path):
        faq_dir = tmp_path / "faq"
        faq_dir.mkdir()
        (faq_dir / "dyson.txt").write_text("Dyson FAQ content", encoding="utf-8")

        with patch("faq_service.FAQ_DIR", str(faq_dir)):
            result = load_brand_faq("dyson")
        assert result == "Dyson FAQ content"

    def test_not_found_returns_none(self, tmp_path):
        faq_dir = tmp_path / "faq"
        faq_dir.mkdir()

        with patch("faq_service.FAQ_DIR", str(faq_dir)):
            result = load_brand_faq("nokia")
        assert result is None

    def test_case_insensitive(self, tmp_path):
        faq_dir = tmp_path / "faq"
        faq_dir.mkdir()
        (faq_dir / "dyson.txt").write_text("content", encoding="utf-8")

        with patch("faq_service.FAQ_DIR", str(faq_dir)):
            result = load_brand_faq("DYSON")
        assert result == "content"

    def test_strips_whitespace(self, tmp_path):
        faq_dir = tmp_path / "faq"
        faq_dir.mkdir()
        (faq_dir / "dyson.txt").write_text("content", encoding="utf-8")

        with patch("faq_service.FAQ_DIR", str(faq_dir)):
            result = load_brand_faq("  dyson  ")
        assert result == "content"


class TestLoadGeneralFaq:
    def test_existing(self, tmp_path):
        faq_dir = tmp_path / "faq"
        faq_dir.mkdir()
        (faq_dir / "general.txt").write_text("General FAQ", encoding="utf-8")

        with patch("faq_service.FAQ_DIR", str(faq_dir)):
            result = load_general_faq()
        assert result == "General FAQ"

    def test_missing_returns_empty(self, tmp_path):
        faq_dir = tmp_path / "faq"
        faq_dir.mkdir()

        with patch("faq_service.FAQ_DIR", str(faq_dir)):
            result = load_general_faq()
        assert result == ""


class TestListAvailableBrands:
    def test_lists_brands_excluding_general(self, tmp_path):
        faq_dir = tmp_path / "faq"
        faq_dir.mkdir()
        (faq_dir / "dyson.txt").write_text("", encoding="utf-8")
        (faq_dir / "hp.txt").write_text("", encoding="utf-8")
        (faq_dir / "general.txt").write_text("", encoding="utf-8")

        with patch("faq_service.FAQ_DIR", str(faq_dir)):
            result = list_available_brands()
        assert result == ["dyson", "hp"]

    def test_empty_directory(self, tmp_path):
        faq_dir = tmp_path / "faq"
        faq_dir.mkdir()

        with patch("faq_service.FAQ_DIR", str(faq_dir)):
            result = list_available_brands()
        assert result == []

    def test_sorted(self, tmp_path):
        faq_dir = tmp_path / "faq"
        faq_dir.mkdir()
        for name in ["xiaomi", "asus", "msi"]:
            (faq_dir / f"{name}.txt").write_text("", encoding="utf-8")

        with patch("faq_service.FAQ_DIR", str(faq_dir)):
            result = list_available_brands()
        assert result == ["asus", "msi", "xiaomi"]
