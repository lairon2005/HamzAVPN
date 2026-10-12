"""Кнопки автопродления в боте и callback_data, которые обрабатывают хендлеры."""
import types
import unittest
from types import SimpleNamespace

from test_stars_payment import _load_module


def load_keyboards():
    db = types.ModuleType('db')
    db.Tariff = db.PromoCode = db.Channel = type('Model', (), {})
    loader = types.ModuleType('loader')
    loader.config = SimpleNamespace()
    pricing = _load_module('kb_pricing_under_test', 'tgbot/services/pricing.py', {})
    tgbot = types.ModuleType('tgbot'); tgbot.__path__ = []
    services = types.ModuleType('tgbot.services'); services.__path__ = []
    return _load_module('keyboards_under_test', 'tgbot/keyboards/inline.py', {
        'db': db, 'loader': loader, 'tgbot': tgbot, 'tgbot.services': services,
        'tgbot.services.pricing': pricing})


def buttons(markup):
    return [(b.text, b.callback_data, b.url) for row in markup.inline_keyboard for b in row]


class AutorenewKeyboardTests(unittest.TestCase):
    def setUp(self):
        self.kb = load_keyboards()

    def test_checkout_offers_autorenew_only_when_amount_is_known(self):
        plain = buttons(self.kb.tariff_slots_keyboard(5, 1, 10, 3, total_amount=448.0))
        self.assertFalse([b for b in plain if b[1] and b[1].startswith('tauto_')])
        offered = buttons(self.kb.tariff_slots_keyboard(5, 1, 10, 3, total_amount=448.0, autorenew_amount=546))
        auto = [b for b in offered if b[1] and b[1].startswith('tauto_')]
        self.assertEqual(len(auto), 1)
        self.assertEqual(auto[0][1], 'tauto_5_1')
        self.assertIn('546', auto[0][0])
        # Разовая оплата остаётся рядом и не меняется.
        self.assertIn('tpay_5_1', [b[1] for b in offered])

    def test_screen_actions_follow_subscription_state(self):
        active = buttons(self.kb.autorenew_screen_keyboard('active'))
        self.assertIn('autorenew_off', [b[1] for b in active])
        pending = buttons(self.kb.autorenew_screen_keyboard('pending', 'https://pay.platega.io/s'))
        self.assertIn('https://pay.platega.io/s', [b[2] for b in pending])
        self.assertIn('autorenew_off', [b[1] for b in pending])
        none = buttons(self.kb.autorenew_screen_keyboard(None))
        self.assertNotIn('autorenew_off', [b[1] for b in none])
        self.assertIn('buy_subscription', [b[1] for b in none])

    def test_profile_links_to_autorenew_screen(self):
        profile = buttons(self.kb.profile_keyboard('https://sub.example/x'))
        self.assertIn('autorenew', [b[1] for b in profile])

    def test_notification_keyboards_point_to_existing_callbacks(self):
        self.assertEqual([b[1] for b in buttons(self.kb.autorenew_manage_keyboard())], ['autorenew'])
        self.assertIn('buy_subscription', [b[1] for b in buttons(self.kb.autorenew_prompt_keyboard())])
        self.assertEqual([b[1] for b in buttons(self.kb.autorenew_confirm_off_keyboard())],
                         ['autorenew_off_yes', 'autorenew'])


if __name__ == '__main__':
    unittest.main()
