# tests/test_ui_mode.py
"""
UI_MODE — глобальный переключатель интерфейса бота (bot | tma).

Набор пунктов главного меню одинаков в обоих режимах, различается только тип
кнопки: callback-сценарий бота или web_app-экран Mini App.

Отдельно проверяется защита от локального домена: Telegram отвергает
web_app-кнопку с localhost ошибкой BUTTON_TYPE_INVALID, причём отклоняется ВСЯ
клавиатура — пользователь остался бы вообще без меню. Поэтому на локальном
домене режим 'tma' обязан деградировать в 'bot'.

tgbot/keyboards/inline.py на импорте тянет loader.py -> config.load_config(),
которому в тестовом окружении не хватает переменных, поэтому модуль грузится
через importlib со стабами (тот же приём, что в test_stars_payment.py).
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


def load_inline_module(ui_mode: str = "bot", domain: str = "example.com"):
    """Загружает tgbot/keyboards/inline.py с подменённым loader.config."""
    module_path = Path(__file__).resolve().parents[1] / "tgbot/keyboards/inline.py"

    db_module = types.ModuleType("db")
    db_module.Tariff = type("Tariff", (), {})
    db_module.PromoCode = type("PromoCode", (), {})
    db_module.Channel = type("Channel", (), {})

    loader_module = types.ModuleType("loader")
    loader_module.config = SimpleNamespace(
        tg_bot=SimpleNamespace(ui_mode=ui_mode),
        webhook=SimpleNamespace(domain=domain),
    )

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []
    tgbot_services_module = types.ModuleType("tgbot.services")
    tgbot_services_module.__path__ = []
    pricing_module = types.ModuleType("tgbot.services.pricing")
    pricing_module.effective_price = lambda tariff, user_has_active_sub=False: 100.0
    pricing_module.format_quota = lambda *a, **kw: ""

    stubs = {
        "db": db_module,
        "loader": loader_module,
        "tgbot": tgbot_module,
        "tgbot.services": tgbot_services_module,
        "tgbot.services.pricing": pricing_module,
    }

    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location("inline_under_test", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
    return module


def buttons(markup):
    """[(text, kind, target)] где kind: 'web_app' | 'callback' | 'url'."""
    result = []
    for row in markup.inline_keyboard:
        for b in row:
            if b.web_app:
                result.append((b.text, "web_app", b.web_app.url))
            elif b.url:
                result.append((b.text, "url", b.url))
            else:
                result.append((b.text, "callback", b.callback_data))
    return result


class TmaModeEnabledTests(unittest.TestCase):
    def test_bot_mode_disables_web_app(self):
        module = load_inline_module(ui_mode="bot", domain="example.com")
        self.assertFalse(module.tma_mode_enabled())

    def test_tma_mode_with_real_domain_enables_web_app(self):
        module = load_inline_module(ui_mode="tma", domain="example.com")
        self.assertTrue(module.tma_mode_enabled())

    def test_localhost_degrades_to_bot_mode(self):
        # dev-режим (CLAUDE.md: DOMAIN=localhost для polling) — иначе Telegram
        # отверг бы всю клавиатуру и юзер не получил бы меню вообще.
        for domain in ("localhost", "LOCALHOST", "127.0.0.1", "", "   "):
            with self.subTest(domain=domain):
                module = load_inline_module(ui_mode="tma", domain=domain)
                self.assertFalse(module.tma_mode_enabled())


class MainMenuModeTests(unittest.TestCase):
    ITEMS = ["💎 Оплатить", "🛜 Подключиться", "👥 Пригласить друга", "💬 Поддержка", "📄 Документы и тарифы"]

    def test_bot_mode_uses_callbacks_only(self):
        module = load_inline_module(ui_mode="bot")
        found = buttons(module.main_menu_keyboard(has_active_sub=True, has_email=True))

        self.assertEqual([t for t, _, _ in found], self.ITEMS)
        self.assertTrue(all(kind == "callback" for _, kind, _ in found))

    def test_tma_mode_keeps_same_items_as_web_app(self):
        module = load_inline_module(ui_mode="tma")
        found = buttons(module.main_menu_keyboard(has_active_sub=True, has_email=True))

        # Пункты те же — меняется только тип кнопки (решение пользователя).
        self.assertEqual([t for t, _, _ in found], self.ITEMS)
        self.assertTrue(all(kind == "web_app" for _, kind, _ in found[:4]))
        self.assertEqual(found[4], ("📄 Документы и тарифы", "callback", "documents"))

    def test_tma_mode_targets_matching_screens(self):
        module = load_inline_module(ui_mode="tma")
        targets = {t: url for t, _, url in buttons(module.main_menu_keyboard())}

        self.assertTrue(targets["💎 Оплатить"].endswith("/tma/tariffs"))
        self.assertTrue(targets["🛜 Подключиться"].endswith("/tma/import"))
        self.assertTrue(targets["👥 Пригласить друга"].endswith("/tma/referral"))
        self.assertTrue(targets["💬 Поддержка"].endswith("/tma/support"))
        self.assertNotIn(":443", targets["💬 Поддержка"])

    def test_bot_only_scenarios_stay_callbacks_in_tma_mode(self):
        # Триал требует проверки подписки на каналы через Bot API, привязка email —
        # FSM бота. Из Mini App их не сделать, поэтому они остаются callback-кнопками.
        module = load_inline_module(ui_mode="tma")
        found = buttons(module.main_menu_keyboard(
            has_active_sub=False, has_email=False,
        ))
        kinds = {text: kind for text, kind, _ in found}

        self.assertEqual(kinds["🌟 +7 дней за подписку"], "callback")
        self.assertEqual(kinds["📧 Привязать Email"], "callback")
        self.assertEqual(kinds["📄 Документы и тарифы"], "callback")

    def test_localhost_tma_mode_renders_bot_menu(self):
        module = load_inline_module(ui_mode="tma", domain="localhost")
        found = buttons(module.main_menu_keyboard())

        self.assertTrue(all(kind == "callback" for _, kind, _ in found))


class KeysScreenModeTests(unittest.TestCase):
    SUB_URL = "https://sub.example/abcdef"

    def test_bot_mode_has_no_web_app_button(self):
        module = load_inline_module(ui_mode="bot")
        found = buttons(module.keys_screen_keyboard(self.SUB_URL))

        self.assertNotIn("web_app", [kind for _, kind, _ in found])
        self.assertNotIn("📲 Открыть в приложении", [t for t, _, _ in found])

    def test_tma_mode_adds_web_app_button(self):
        module = load_inline_module(ui_mode="tma")
        found = buttons(module.keys_screen_keyboard(self.SUB_URL))
        by_text = {t: (kind, url) for t, kind, url in found}

        self.assertEqual(by_text["📲 Открыть в приложении"][0], "web_app")
        self.assertTrue(by_text["📲 Открыть в приложении"][1].endswith("/tma/import"))

    def test_link_points_to_subscription_page(self):
        # Кнопка ведёт на сам subscription_url — Remnawave отдаёт по нему
        # sub-страницу с импортом в приложения, без промежуточного /import.
        for mode in ("bot", "tma"):
            with self.subTest(mode=mode):
                module = load_inline_module(ui_mode=mode)
                by_text = {
                    t: (kind, url)
                    for t, kind, url in buttons(module.keys_screen_keyboard(self.SUB_URL))
                }
                self.assertEqual(
                    by_text["🔗 Открыть страницу подписки"], ("url", self.SUB_URL)
                )


if __name__ == "__main__":
    unittest.main()
