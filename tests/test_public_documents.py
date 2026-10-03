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
                self.assertLessEqual(len(part.encode("utf-16-le")) // 2, 3800)
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


class FullDocumentDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def load_handler(self, tariffs=None):
        public, catalog = load_public_router(tariffs)
        keyboard_module = types.ModuleType("tgbot.keyboards.inline")
        keyboard_module.legal_menu_keyboard = load_inline_module().legal_menu_keyboard
        stubs = {**public._test_stubs, "tgbot.keyboards.inline": keyboard_module}
        with patch.dict(sys.modules, stubs):
            spec = importlib.util.spec_from_file_location(
                "bot_legal_full_document_under_test", PROJECT / "tgbot/handlers/user/legal.py",
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module, catalog, stubs

    async def test_full_bot_documents_are_sent_without_dns_and_include_matching_file(self):
        from utils.legal import document_plaintext
        module, _, stubs = self.load_handler()
        with patch.dict(sys.modules, stubs), patch("socket.getaddrinfo", side_effect=OSError("DNS unavailable")):
            for document in (PRIVACY, TERMS):
                with self.subTest(document=document.title):
                    message = SimpleNamespace(answer=AsyncMock(), answer_document=AsyncMock())
                    state = SimpleNamespace(set_state=AsyncMock())
                    await module.send_document(message, state, document)
                    parts = [call.args[0] for call in message.answer.await_args_list]
                    self.assertEqual(parts, document_messages(document))
                    self.assertGreater(len(parts), 1)
                    for call in message.answer.await_args_list:
                        self.assertIn("documents", {target for _, _, target in buttons(call.kwargs["reply_markup"])})
                    upload = message.answer_document.await_args.args[0]
                    self.assertEqual(upload.data.decode("utf-8"), document_plaintext(document))
                    self.assertTrue(upload.filename.endswith(".txt"))
                    self.assertIn(DOCUMENT_DATE, message.answer_document.await_args.kwargs["caption"])
                    state.set_state.assert_awaited_once_with(None)

    async def test_each_public_tariff_has_a_price_and_existing_purchase_path(self):
        tariffs = [
            SimpleNamespace(id=21, name="Месяц", price=249.5, duration_days=30,
                            loyalty_price=None, data_limit_gb=100, price_stars=None),
            SimpleNamespace(id=22, name="Год", price=1890, duration_days=365,
                            loyalty_price=1790, data_limit_gb=0, price_stars=None),
        ]
        module, catalog, stubs = self.load_handler(tariffs)
        message = SimpleNamespace(answer=AsyncMock())
        state = SimpleNamespace(set_state=AsyncMock())
        with patch.dict(sys.modules, stubs):
            await module.show_tariffs(message, state)
        self.assertEqual(message.answer.await_count, 3)
        for tariff, call in zip(tariffs, message.answer.await_args_list[1:]):
            self.assertIn(tariff.name, call.args[0])
            self.assertIn(f"{tariff.price:.2f} RUB", call.args[0])
            self.assertIn((f"💳 Оплатить {tariff.price:.2f} ₽", "callback", f"select_tariff_{tariff.id}"),
                          buttons(call.kwargs["reply_markup"]))
            self.assertIn("documents", {target for _, _, target in buttons(call.kwargs["reply_markup"])})
            self.assertIn("итоговую сумму", call.args[0])
        catalog.get_active.assert_awaited_once()


    async def test_reading_documents_stops_input_state_but_preserves_discounted_checkout(self):
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage
        module, _, stubs = self.load_handler()
        pricing = stubs["tgbot.services.device_pricing"]
        settings = pricing.DeviceSettings(price=49, base_limit=5, max_extra=3)
        cart = {"discount": 20, "promo_code": "SAVE20", "payment_message_id": 456,
                "tariff_id": 21, "slots": 2}
        before_total = pricing.build_checkout(settings, 399, 90, cart["slots"], cart["discount"]).total
        storage = MemoryStorage()
        state = FSMContext(storage, StorageKey(bot_id=1, chat_id=2, user_id=3))
        try:
            with patch.dict(sys.modules, stubs):
                for view in ("menu", "privacy", "terms", "tariffs"):
                    with self.subTest(view=view):
                        await state.set_data(cart)
                        await state.set_state("UserState:entering_promo")
                        message = SimpleNamespace(answer=AsyncMock(), answer_document=AsyncMock())
                        if view == "menu":
                            await module.show_documents(message, state)
                        elif view == "tariffs":
                            await module.show_tariffs(message, state)
                        else:
                            await module.send_document(message, state, PRIVACY if view == "privacy" else TERMS)
                        self.assertIsNone(await state.get_state())
                        after = await state.get_data()
                        self.assertEqual(after, cart)
                        after_total = pricing.build_checkout(settings, 399, 90, after["slots"], after["discount"]).total
                        self.assertEqual(after_total, before_total)
                        self.assertLess(after_total, pricing.build_checkout(settings, 399, 90, cart["slots"]).total)
        finally:
            await storage.close()


class FullLegalContentTests(unittest.TestCase):
    def test_full_documents_cover_service_data_rights_and_billing(self):
        self.assertGreaterEqual(len(PRIVACY.sections), 10)
        self.assertGreaterEqual(len(TERMS.sections), 10)
        privacy = "\n".join(text for _, text in PRIVACY.sections)
        terms = "\n".join(text for _, text in TERMS.sections)
        for expected in ("Telegram ID", "хеш пароля", "Platega", "Cookies", "cookies", "отозвать согласие", "/support"):
            self.assertIn(expected, privacy + "\n".join(heading for heading, _ in PRIVACY.sections))
        for expected in ("сохранность", "возвраты", "Интеллектуальная собственность", "Прекращение", "Тарифы"):
            self.assertIn(expected.lower(), (terms + "\n".join(heading for heading, _ in TERMS.sections)).lower())
        self.assertIn("не ограничивает обращение сроком в 24 часа", terms)
        self.assertIn("не подключает автоматические списания", terms)

    def test_large_single_section_keeps_content_and_valid_escaped_html(self):
        import re
        from html import unescape
        from utils.legal import LegalDocument
        heading = "1. Пункт & условия"
        body = "Начало <проверки> & текст. " * 700 + "я" * 8000 + " Финальный уникальный пункт."
        doc = LegalDocument("Длинный документ", ((heading, body),))
        parts = document_messages(doc, max_length=500)
        self.assertGreater(len(parts), 10)
        bodies = []
        for index, part in enumerate(parts, 1):
            self.assertLessEqual(len(part), 500)
            self.assertIn(f"Часть {index} из {len(parts)}", part)
            # Единственные HTML-теги — сбалансированные выделения заголовков.
            self.assertEqual(part.count("<b>"), part.count("</b>"))
            self.assertNotIn("<проверки>", part)
            plain = unescape(re.sub(r"</?b>", "", part))
            bodies.append(plain.split(heading + "\n", 1)[1])
        reconstructed = re.sub(r"\s", "", "".join(bodies))
        self.assertEqual(reconstructed, re.sub(r"\s", "", body))

    def test_long_astral_emoji_document_fits_utf16_limit_and_roundtrips(self):
        import re
        from html import unescape
        from utils.legal import LegalDocument, _escaped_chunks
        heading = "1. 🧾 Пункт 😀 & условия"
        body = "😀🧾👩\u200d💻<>&" * 1500 + "🏁Конец"
        doc = LegalDocument("😀 Документ 🧾", ((heading, body),))
        parts = document_messages(doc, max_length=500)
        self.assertGreater(len(parts), 10)
        bodies = []
        for index, part in enumerate(parts, 1):
            self.assertLessEqual(len(part.encode("utf-16-le")) // 2, 500)
            self.assertIn(f"Часть {index} из {len(parts)}", part)
            self.assertEqual(part.count("<b>"), part.count("</b>"))
            plain = unescape(re.sub(r"</?b>", "", part))
            bodies.append(plain.split(heading + "\n", 1)[1].rstrip("\n"))
        self.assertEqual("".join(bodies), body)
        # При разбиении отдельно не исчезают даже пробелы и комбинированные emoji.
        spaced = "😀 & 👩\u200d💻 \n" * 1000
        chunks = _escaped_chunks(spaced, 200)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 200 for chunk in chunks))
        self.assertEqual(unescape("".join(chunks)), spaced)

    def test_message_length_too_small_is_rejected_instead_of_omitting_sections(self):
        with self.assertRaises(ValueError):
            document_messages(PRIVACY, max_length=30)

    def test_site_and_bot_have_same_full_sections(self):
        module, _ = load_public_router()
        app = FastAPI()
        app.include_router(module.router)
        with TestClient(app) as client:
            for path, doc in (("/privacy", PRIVACY), ("/terms", TERMS)):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                for heading, text in doc.sections:
                    self.assertIn(heading, response.text)
                    self.assertIn(text, response.text)
                    self.assertIn(text, "\n".join(document_messages(doc)))
