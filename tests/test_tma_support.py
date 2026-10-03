# tests/test_tma_support.py
"""
Валидация и rate-limit формы поддержки в Telegram Mini App (README.md
фаза 4, webapp/routers/tma.py:: POST /tma/support). Логика вынесена в
webapp/core/support.py (stdlib only, без БД/сети) — тесты импортируют её
напрямую, как и tests/test_tma_auth.py для webapp/core/tma_auth.py.
"""
import unittest
from datetime import datetime, timedelta

from webapp.core.support import (
    SUPPORT_MESSAGE_MAX_LEN,
    SupportValidationError,
    check_rate_limit,
    format_new_ticket_message,
    format_support_message,
    validate_support_text,
)


class ValidateSupportTextTests(unittest.TestCase):
    def test_valid_text_is_stripped(self):
        result = validate_support_text("  Не работает VPN на iPhone  ")
        self.assertEqual(result, "Не работает VPN на iPhone")

    def test_empty_text_is_rejected(self):
        with self.assertRaises(SupportValidationError) as ctx:
            validate_support_text("")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_whitespace_only_text_is_rejected(self):
        with self.assertRaises(SupportValidationError) as ctx:
            validate_support_text("   \n\t  ")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_none_text_is_rejected(self):
        with self.assertRaises(SupportValidationError):
            validate_support_text(None)

    def test_text_at_limit_is_accepted(self):
        text = "x" * SUPPORT_MESSAGE_MAX_LEN
        result = validate_support_text(text)
        self.assertEqual(len(result), SUPPORT_MESSAGE_MAX_LEN)

    def test_text_over_limit_is_rejected(self):
        text = "x" * (SUPPORT_MESSAGE_MAX_LEN + 1)
        with self.assertRaises(SupportValidationError) as ctx:
            validate_support_text(text)
        self.assertEqual(ctx.exception.status_code, 400)


class CheckRateLimitTests(unittest.TestCase):
    def test_first_message_is_allowed(self):
        store = {}
        check_rate_limit(store, user_id=1, now=datetime(2026, 1, 1, 12, 0, 0))
        self.assertIn(1, store)

    def test_second_message_within_window_is_rejected(self):
        store = {}
        base = datetime(2026, 1, 1, 12, 0, 0)
        check_rate_limit(store, user_id=1, now=base)

        with self.assertRaises(SupportValidationError) as ctx:
            check_rate_limit(store, user_id=1, now=base + timedelta(seconds=10))
        self.assertEqual(ctx.exception.status_code, 429)

    def test_message_after_window_is_allowed(self):
        store = {}
        base = datetime(2026, 1, 1, 12, 0, 0)
        check_rate_limit(store, user_id=1, now=base)

        # Ровно на границе окна (30 сек) — не должно блокироваться следующим прогоном.
        check_rate_limit(store, user_id=1, now=base + timedelta(seconds=31))
        self.assertEqual(store[1], base + timedelta(seconds=31))

    def test_rate_limit_is_per_user(self):
        store = {}
        base = datetime(2026, 1, 1, 12, 0, 0)
        check_rate_limit(store, user_id=1, now=base)
        # Другой пользователь не должен зависеть от лимита первого.
        check_rate_limit(store, user_id=2, now=base)
        self.assertIn(1, store)
        self.assertIn(2, store)


class FormatSupportMessageTests(unittest.TestCase):
    """Бот шлёт с parse_mode=HTML (loader.py), поэтому пользовательские данные
    обязаны быть экранированы — иначе `<` в обращении ломает разбор entities
    (Telegram 400 "can't parse entities") и обращение теряется."""

    def test_angle_brackets_in_text_are_escaped(self):
        result = format_support_message("Иван", 123, "почему 5 < 10 не работает?")
        self.assertIn("5 &lt; 10", result)
        self.assertNotIn("5 < 10", result)

    def test_html_injection_in_text_is_neutralized(self):
        result = format_support_message("Иван", 123, '<a href="http://evil">клик</a>')
        self.assertNotIn('<a href', result)
        self.assertIn("&lt;a href=", result)

    def test_ampersand_is_escaped(self):
        result = format_support_message("Иван", 123, "тариф R&D")
        self.assertIn("R&amp;D", result)

    def test_display_name_is_escaped(self):
        # full_name задаётся пользователем в Telegram — тоже небезопасен.
        result = format_support_message("<b>Админ</b>", 123, "привет")
        self.assertNotIn("<b>Админ", result)
        self.assertIn("&lt;b&gt;Админ", result)

    def test_own_markup_and_id_survive(self):
        result = format_support_message("Иван", 123, "привет")
        self.assertIn("<b>Иван</b>", result)
        self.assertIn("<code>123</code>", result)
        self.assertTrue(result.endswith("привет"))

    def test_quotes_are_not_escaped(self):
        # quote=False: кавычки безопасны и не должны превращаться в &quot;
        result = format_support_message("Иван", 123, 'ошибка "timeout"')
        self.assertIn('"timeout"', result)

    def test_new_ticket_message_escapes_display_name(self):
        result = format_new_ticket_message("<i>hack</i>", 456)
        self.assertNotIn("<i>hack", result)
        self.assertIn("&lt;i&gt;hack", result)
        self.assertIn("<code>456</code>", result)


if __name__ == "__main__":
    unittest.main()
