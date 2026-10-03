"""Публичность условий и реальные параметры каталога до входа в аккаунт."""
import importlib.util
import logging
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils.legal import (
    DOCUMENT_DATE, PRIVACY, TERMS, document_messages,
    platega_review_mode, telegram_support_url,
)
from test_ui_mode import buttons, load_inline_module

PROJECT = Path(__file__).resolve().parents[1]


def load_public_router(tariffs=None, error=None):
    catalog = SimpleNamespace(get_active=AsyncMock(return_value=tariffs or [], side_effect=error))
    device_service = SimpleNamespace(settings=AsyncMock(return_value=SimpleNamespace(
        price=49, base_limit=5, max_extra=3,
    )))
    db_module = types.ModuleType("database")
    db_module.tariff_repo = catalog
    loader_module = types.ModuleType("loader")
    loader_module.config = SimpleNamespace(tg_bot=SimpleNamespace(tg_bot_username="HamzaTestBot"))
    loader_module.logger = logging.getLogger("test_public_documents")
    service_module = types.ModuleType("tgbot.services")
    service_module.__path__ = []
    service_module.device_slot_service = device_service
    pricing_spec = importlib.util.spec_from_file_location(
        "tgbot.services.device_pricing", PROJECT / "tgbot/services/device_pricing.py",
    )
    pricing_module = importlib.util.module_from_spec(pricing_spec)
    stubs = {
        "database": db_module, "loader": loader_module,
        "tgbot.services": service_module,
        "tgbot.services.device_pricing": pricing_module,
    }
    with patch.dict(sys.modules, stubs):
        pricing_spec.loader.exec_module(pricing_module)
        router_spec = importlib.util.spec_from_file_location("public_legal_router", PROJECT / "webapp/routers/legal.py")
        module = importlib.util.module_from_spec(router_spec)
        router_spec.loader.exec_module(module)
    module._test_stubs = stubs
    return module, catalog


class PublicDocumentsTests(unittest.TestCase):
    def client_for(self, module):
        app = FastAPI()
        app.include_router(module.router)
        return TestClient(app)

    def test_every_public_page_is_available_without_login(self):
        module, _ = load_public_router()
        with self.client_for(module) as client:
            for path in ("/documents", "/privacy", "/terms", "/support", "/tariffs"):
                with self.subTest(path=path):
                    response = client.get(path, follow_redirects=False)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("HamzAVPN", response.text)
                    self.assertIn("Платега тест", response.text)
                    self.assertNotIn("href=\"#\"", response.text)

    def test_review_word_can_be_removed_without_editing_documents(self):
        module, _ = load_public_router()
        with patch.dict(os.environ, {"PLATEGA_REVIEW_MODE": "false"}), self.client_for(module) as client:
            self.assertNotIn("Платега тест", client.get("/documents").text)
            self.assertNotIn("Платега тест", client.get("/privacy").text)

    def test_tariffs_show_preserved_database_prices_duration_and_devices(self):
        tariff = SimpleNamespace(name="Квартал", price=399.5, duration_days=90,
                                 loyalty_price=349, data_limit_gb=100, price_stars=199)
        module, catalog = load_public_router([tariff])
        with patch.dict(sys.modules, module._test_stubs), self.client_for(module) as client:
            response = client.get("/tariffs")
        self.assertEqual(response.status_code, 200)
        for expected in ("Квартал", "399.50 RUB", "349.00 RUB", "90 дней", "Включено устройств: 5",
                         "100 ГБ в месяц", "147.00 RUB", "199 XTR"):
            self.assertIn(expected, response.text)
        catalog.get_active.assert_awaited_once()

    def test_empty_catalog_does_not_invent_prices_or_offer_checkout(self):
        module, _ = load_public_router()
        with self.client_for(module) as client:
            text = client.get("/tariffs").text
        self.assertIn("Продажи подписок пока не открыты", text)
        self.assertNotIn("Оформить подписку", text)

    def test_catalog_error_keeps_documents_available(self):
        module, _ = load_public_router(error=RuntimeError("unavailable"))
        with self.client_for(module) as client:
            with self.assertLogs("test_public_documents", level="ERROR"):
                response = client.get("/tariffs")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Тарифы временно недоступны", response.text)
            self.assertEqual(client.get("/terms").status_code, 200)

    def test_support_is_a_personal_bot_ticket(self):
        module, _ = load_public_router()
        with self.client_for(module) as client:
            text = client.get("/support").text
        self.assertIn("https://t.me/HamzaTestBot?start=support", text)
        self.assertIn("/support", text)
        self.assertIn("не требуется", text)


class BotDocumentTests(unittest.TestCase):
    def test_documents_menu_is_available_in_bot_and_mini_app_modes(self):
        for ui_mode in ("bot", "tma"):
            module = load_inline_module(ui_mode=ui_mode)
            main = buttons(module.main_menu_keyboard(has_active_sub=True, has_email=True))
            self.assertIn(("📄 Документы и тарифы", "callback", "documents"), main)
            destinations = {target for _, _, target in buttons(module.legal_menu_keyboard())}
            self.assertTrue({"privacy_policy", "terms_of_service", "public_tariffs", "support_chat_start"} <= destinations)

    def test_complete_documents_fit_telegram_and_keep_revision(self):
        self.assertEqual(DOCUMENT_DATE, "03.10.2026")
        for document in (PRIVACY, TERMS):
            parts = document_messages(document)
            for part in parts:
                self.assertLessEqual(len(part), 3800)
                self.assertIn(DOCUMENT_DATE, part)
            combined = "\n".join(parts)
            for heading, text in document.sections:
                self.assertIn(heading, combined)
                self.assertIn(text, combined)

    def test_support_link_rejects_unconfigured_or_non_username_values(self):
        self.assertIsNone(telegram_support_url(None))
        self.assertIsNone(telegram_support_url("https://example.com"))
        self.assertIsNone(telegram_support_url("test/other"))
        self.assertEqual(telegram_support_url("@HamzaTestBot"), "https://t.me/HamzaTestBot?start=support")

    def test_review_mode_defaults_enabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(platega_review_mode())
