import os
import unittest
from unittest.mock import patch

from environs import Env

from config import Remnawave, TgBot, Platega


class RemnawaveConfigTests(unittest.TestCase):
    def test_proxy_url_is_independent_from_telegram_proxy(self):
        values = {
            "REMNAWAVE_API_URL": "https://panel.example",
            "REMNAWAVE_API_TOKEN": "token",
            "REMNAWAVE_PROXY_URL": "socks5://proxy.example:1080",
            "TG_PROXY_URL": "http://telegram-proxy.example:8080",
        }

        with patch.dict(os.environ, values, clear=True):
            config = Remnawave.from_env(Env())

        self.assertEqual(config.proxy_url, values["REMNAWAVE_PROXY_URL"])

    def test_proxy_url_is_optional(self):
        values = {
            "REMNAWAVE_API_URL": "https://panel.example",
            "REMNAWAVE_API_TOKEN": "token",
            "REMNAWAVE_PROXY_URL": "",
        }

        with patch.dict(os.environ, values, clear=True):
            config = Remnawave.from_env(Env())

        self.assertIsNone(config.proxy_url)


class TgBotTmaConfigTests(unittest.TestCase):
    """
    Telegram Mini App (README.md: настройка Mini App): TG_BOT_USERNAME/TMA_APP_NAME
    нужны для реферальной ссылки t.me/<bot>/<app>?startapp=<id> в /tma/referral.
    Обе переменные опциональны — без TG_BOT_USERNAME шаблон скрывает блок "поделиться".
    """

    BASE_VALUES = {
        "BOT_TOKEN": "123456:TEST-TOKEN",
        "ADMINS": "1,2",
        "SUPPORT_CHAT_ID": "100",
        "TRANSACTION_LOG_TOPIC_ID": "1",
    }

    def test_tma_fields_default_when_unset(self):
        with patch.dict(os.environ, self.BASE_VALUES, clear=True):
            bot = TgBot.from_env(Env())

        self.assertIsNone(bot.tg_bot_username)
        self.assertEqual(bot.tma_app_name, "app")

    def test_tma_fields_can_be_overridden(self):
        values = dict(self.BASE_VALUES, TG_BOT_USERNAME="HamzAVPN_bot", TMA_APP_NAME="miniapp")

        with patch.dict(os.environ, values, clear=True):
            bot = TgBot.from_env(Env())

        self.assertEqual(bot.tg_bot_username, "HamzAVPN_bot")
        self.assertEqual(bot.tma_app_name, "miniapp")


class TgBotUiModeConfigTests(unittest.TestCase):
    """UI_MODE — глобальный переключатель интерфейса: 'bot' (callback-кнопки)
    или 'tma' (те же пункты меню открывают экраны Mini App)."""

    BASE_VALUES = TgBotTmaConfigTests.BASE_VALUES

    def test_defaults_to_bot_mode(self):
        # Дефолт обязан быть 'bot': режим 'tma' требует публичного домена и
        # приложения в BotFather, иначе Telegram отвергнет web_app-кнопки.
        with patch.dict(os.environ, self.BASE_VALUES, clear=True):
            bot = TgBot.from_env(Env())

        self.assertEqual(bot.ui_mode, "bot")

    def test_tma_mode_is_accepted(self):
        with patch.dict(os.environ, dict(self.BASE_VALUES, UI_MODE="tma"), clear=True):
            bot = TgBot.from_env(Env())

        self.assertEqual(bot.ui_mode, "tma")

    def test_value_is_normalized(self):
        with patch.dict(os.environ, dict(self.BASE_VALUES, UI_MODE="  TMA  "), clear=True):
            bot = TgBot.from_env(Env())

        self.assertEqual(bot.ui_mode, "tma")

    def test_unknown_value_fails_fast(self):
        # Опечатка не должна тихо откатываться в 'bot' — иначе это выглядит
        # как «переключатель не работает».
        with patch.dict(os.environ, dict(self.BASE_VALUES, UI_MODE="miniapp"), clear=True):
            with self.assertRaises(ValueError):
                TgBot.from_env(Env())


if __name__ == "__main__":
    unittest.main()


class PlategaConfigTests(unittest.TestCase):
    def test_credentials_are_optional_until_merchant_registration(self):
        with patch.dict(os.environ, {}, clear=True):
            gateway = Platega.from_env(Env())
        self.assertEqual(gateway.merchant_id, '')
        self.assertEqual(gateway.secret, '')
        self.assertEqual(gateway.api_url, 'https://app.platega.io')

    def test_explicit_credentials_are_trimmed(self):
        with patch.dict(os.environ, {'PLATEGA_MERCHANT_ID': ' merchant ',
                                    'PLATEGA_SECRET': ' secret '}, clear=True):
            gateway = Platega.from_env(Env())
        self.assertEqual(gateway.merchant_id, 'merchant')
        self.assertEqual(gateway.secret, 'secret')

    def test_blank_optional_telegram_values_normalize_to_none(self):
        values = dict(TgBotTmaConfigTests.BASE_VALUES,
                      TG_PROXY_URL='', TG_BOT_USERNAME='', INSTRUCTION_VIDEO_ID='')
        with patch.dict(os.environ, values, clear=True):
            bot = TgBot.from_env(Env())
        self.assertIsNone(bot.proxy_url)
        self.assertIsNone(bot.tg_bot_username)
        self.assertIsNone(bot.instruction_video_id)

    def test_unconfigured_admin_and_support_are_safe_defaults(self):
        with patch.dict(os.environ, {'BOT_TOKEN': '123:test'}, clear=True):
            bot = TgBot.from_env(Env())
        self.assertEqual(bot.admin_ids, [])
        self.assertEqual(bot.support_chat_id, 0)
        self.assertEqual(bot.transaction_log_topic_id, 0)
