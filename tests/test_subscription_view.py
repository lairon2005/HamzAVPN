import unittest
from datetime import datetime, timedelta

from utils.subscription_view import (
    build_connection_view,
    build_db_subscription_view,
)


class DbSubscriptionViewTests(unittest.TestCase):
    def test_active_subscription_is_built_from_db_date(self):
        now = datetime(2026, 7, 20, 12, 0)
        view = build_db_subscription_view(now + timedelta(days=5), now=now)

        self.assertTrue(view.is_active)
        self.assertEqual(view.status_label, "активна")
        self.assertIn("осталось 5 дн.", view.expires_label)

    def test_missing_subscription_is_inactive(self):
        view = build_db_subscription_view(None, now=datetime(2026, 7, 20))

        self.assertFalse(view.is_active)
        self.assertEqual(view.status_label, "не оформлена")

    def test_expired_subscription_is_inactive(self):
        now = datetime(2026, 7, 20, 12, 0)
        view = build_db_subscription_view(now - timedelta(seconds=1), now=now)

        self.assertFalse(view.is_active)
        self.assertEqual(view.status_label, "истекла")


class ConnectionViewTests(unittest.TestCase):
    def test_connection_screen_contains_link_and_traffic(self):
        view = build_connection_view({
            "status": "active",
            "expire": datetime(2027, 1, 1, 12, 0).timestamp(),
            "used_traffic": 5 * 1024 ** 3,
            "data_limit": 100 * 1024 ** 3,
            "subscription_url": "https://panel.example/sub/abc",
        })

        self.assertEqual(view.subscription_url, "https://panel.example/sub/abc")
        self.assertIn("Использовано: <code>5.00 Гб</code>", view.text)
        self.assertIn("Лимит: <code>100.00 Гб</code>", view.text)
        self.assertIn("https://panel.example/sub/abc", view.text)

    def test_connection_screen_escapes_url_for_telegram_html(self):
        view = build_connection_view({
            "status": "active",
            "expire": None,
            "used_traffic": 0,
            "data_limit": 0,
            "subscription_url": "https://panel.example/sub?a=1&b=<value>",
        })

        self.assertIn("a=1&amp;b=&lt;value&gt;", view.text)
        self.assertEqual(
            view.subscription_url,
            "https://panel.example/sub?a=1&b=<value>",
        )


if __name__ == "__main__":
    unittest.main()
