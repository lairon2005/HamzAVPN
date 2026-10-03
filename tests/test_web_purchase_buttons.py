"""Публичный путь к оплате и цена на кнопках до создания счета."""
import importlib.util
import logging
import shutil
import subprocess
import sys
import types
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from test_public_documents import load_public_router
from test_ui_mode import load_inline_module

PROJECT = Path(__file__).resolve().parents[1]


class PurchaseLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.current = [dict(attrs).get('href'), '']

    def handle_data(self, text):
        if self.current is not None:
            self.current[1] += text

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.links.append(tuple(self.current))
            self.current = None


class PublicPurchaseButtonTests(unittest.TestCase):
    def render_tariffs(self, username):
        tariffs = [SimpleNamespace(name=name, price=price, duration_days=days,
                    loyalty_price=None, data_limit_gb=None, price_stars=None)
                   for name, price, days in [('Неделя', 67, 7), ('Месяц', 129, 30)]]
        module, _ = load_public_router(tariffs)
        module.config.tg_bot.tg_bot_username = username
        app = FastAPI()
        app.include_router(module.router)
        with patch.dict(sys.modules, module._test_stubs), TestClient(app) as client:
            response = client.get('/tariffs')
        self.assertEqual(response.status_code, 200)
        parser = PurchaseLinks()
        parser.feed(response.text)
        return [(url, label) for url, label in parser.links if label.startswith('Оплатить ')]

    def test_each_public_card_shows_price_and_reaches_configured_bot(self):
        self.assertEqual(self.render_tariffs('@HamzaTestBot'), [
            ('https://t.me/HamzaTestBot?start=tariffs', 'Оплатить 67.00 ₽'),
            ('https://t.me/HamzaTestBot?start=tariffs', 'Оплатить 129.00 ₽'),
        ])

    def test_no_bot_username_keeps_web_registration_fallback(self):
        for username in (None, '', 'https://example.com'):
            with self.subTest(username=username):
                self.assertEqual([url for url, _ in self.render_tariffs(username)],
                                 ['/register', '/register'])

    def test_dashboard_button_shows_effective_price_and_waits_for_quote(self):
        env = Environment(loader=FileSystemLoader(PROJECT / 'webapp/templates'))
        env.filters['timestamp_to_date'] = str
        text = env.get_template('dashboard.html').render(
            user=SimpleNamespace(full_name='Test', email=None, vpn_username=None),
            tariffs=[SimpleNamespace(id=7, name='Месяц', price=129, duration_days=30)],
            tariff_prices={7: 119.25},
            device_settings=SimpleNamespace(price=49, base_limit=3, max_extra=10),
            slot_quote=SimpleNamespace(current_extra=0),
        )
        self.assertIn('Оплатить 119.25 ₽', text)
        self.assertIn('data-payment-price="129"', text)
        self.assertRegex(text, r'data-payment-tariff="Месяц"[\s\S]*?disabled>')


class TariffsDeepLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_user_opens_catalog_without_trial_or_email_onboarding(self):
        loader = types.ModuleType('loader')
        loader.logger = logging.getLogger('tariffs_start_test')
        database = types.ModuleType('database')
        database.channel_repo = SimpleNamespace()
        database.user_repo = SimpleNamespace()
        database.stats_repo = SimpleNamespace()
        services = types.ModuleType('tgbot.services')
        services.__path__ = [str(PROJECT / 'tgbot/services')]
        register = AsyncMock(return_value=(SimpleNamespace(user_id=42), True))
        services.user_service = SimpleNamespace(register_or_get=register)
        services.referral_service = SimpleNamespace()
        services.subscription_service = SimpleNamespace()
        services.profile_service = SimpleNamespace()
        referral = types.ModuleType('tgbot.services.referral_service')
        referral.REFERRAL_TRIAL_DAYS = 7
        referral.REFERRER_LAUNCH_BONUS_DAYS = 7
        referral.REFERRER_PAYMENT_BONUS_DAYS = 30
        subscription = types.ModuleType('tgbot.services.subscription')
        subscription.check_subscription = AsyncMock()
        legal = types.ModuleType('tgbot.handlers.user.legal')
        legal.show_tariffs = AsyncMock()
        stubs = {
            'loader': loader, 'database': database, 'tgbot.services': services,
            'tgbot.services.referral_service': referral,
            'tgbot.services.subscription': subscription,
            'tgbot.keyboards.inline': load_inline_module(),
            'tgbot.handlers.user.legal': legal,
        }
        spec = importlib.util.spec_from_file_location('tgbot.handlers.user.start',
            PROJECT / 'tgbot/handlers/user/start.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, stubs):
            spec.loader.exec_module(module)
            module._start_onboarding = AsyncMock()
            module._show_main_menu = AsyncMock()
            message = SimpleNamespace(from_user=SimpleNamespace(
                id=42, full_name='Test', username='test_user'))
            state = SimpleNamespace()
            await module.process_start_command(message, SimpleNamespace(args='tariffs'),
                                               SimpleNamespace(), state)
        register.assert_awaited_once_with(42, 'Test', 'test_user')
        legal.show_tariffs.assert_awaited_once_with(message, state)
        module._start_onboarding.assert_not_awaited()
        module._show_main_menu.assert_not_awaited()


@unittest.skipUnless(shutil.which('node'), 'JavaScript runtime is unavailable')
class BrowserQuoteButtonTests(unittest.TestCase):
    def test_final_quote_updates_button_and_errors_never_enable_payment(self):
        result = subprocess.run(['node', '-e', r'''
const assert = require('assert/strict');
const fs = require('fs');
const vm = require('vm');
const label = {textContent: 'Оплатить 129.00 ₽'};
const button = {disabled: true, dataset: {}, querySelector: () => label};
const card = {dataset: {tariffName: 'Месяц', tariffPrice: '129'},
  querySelector: selector => selector === '[data-payment-tariff]' ? button : null};
let fetchQuote;
const context = vm.createContext({document: {
  addEventListener() {}, getElementById() {return null;},
  querySelectorAll() {return [card];}}, window: {}, console,
  fetch: async (...args) => fetchQuote(...args), setTimeout, clearTimeout});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
function response(amount) {return {ok: true, json: async () => ({final_amount: amount})};}
(async () => {
  fetchQuote = async () => response(168.37);
  await vm.runInContext('renderTariffPrices(0)', context);
  assert.equal(label.textContent, 'Оплатить 168.37 ₽');
  assert.equal(button.disabled, false);
  assert.equal(Number(button.dataset.quotedAmount), 168.37);
  fetchQuote = async () => ({ok: false, status: 503,
    json: async () => ({detail: 'Цена временно недоступна'})});
  await vm.runInContext('renderTariffPrices(0)', context);
  assert.equal(button.disabled, true);
  let releaseOld;
  fetchQuote = () => new Promise(resolve => {releaseOld = () => resolve(response(129));});
  const old = vm.runInContext('renderTariffPrices(0)', context);
  fetchQuote = async () => response(178.91);
  await vm.runInContext('renderTariffPrices(0)', context);
  releaseOld(); await old;
  assert.equal(label.textContent, 'Оплатить 178.91 ₽');
  assert.equal(button.disabled, false);
  assert.equal(Number(button.dataset.quotedAmount), 178.91);
})().catch(error => {console.error(error); process.exit(1);});
''', str(PROJECT / 'webapp/static/js/scripts.js')],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
