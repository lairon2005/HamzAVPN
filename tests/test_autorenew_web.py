"""Веб-API автопродления: котировка, создание подписки, статус, отключение."""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from platega.client import PlategaError
from test_stars_payment import load_payment_router_module

SUB_URL = 'https://pay.platega.io/subscription/x'


class AutorenewWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_payment_router_module()
        m = self.module
        self.tariff = SimpleNamespace(id=5, name='Три месяца', is_active=True, price=399,
                                      loyalty_price=None, duration_days=90)
        m.tariff_repo.get_by_name_and_price.return_value = self.tariff
        m.tariff_repo.get_by_id.return_value = self.tariff
        m.device_slot_service.settings.return_value = SimpleNamespace(price=49, base_limit=3, max_extra=10)
        m.payment_service.has_pending_payment.return_value = False
        m.recurring_service.is_available.return_value = True
        m.recurring_service.build_offer.return_value = SimpleNamespace(amount=399)
        self.sub = SimpleNamespace(amount=399, extra_devices=0, tariff_id=5, status='pending',
                                   next_charge_at=None, payment_url=SUB_URL)
        m.recurring_service.start.return_value = (self.sub, SUB_URL)
        self.user = SimpleNamespace(user_id=42, subscription_end_date=datetime.now() + timedelta(days=5))

    def request(self, **over):
        return self.module.PaymentRequest(tariff_name='Три месяца', price=399, **over)

    async def test_quote_reports_autorenew_amount_only_when_available_and_without_promo(self):
        quote = await self.module.quote_payment_route(self.request(), self.user)
        self.assertEqual(quote['autorenew_amount'], 399)
        self.module.recurring_service.is_available.return_value = False
        quote = await self.module.quote_payment_route(self.request(), self.user)
        self.assertIsNone(quote['autorenew_amount'])
        self.module.recurring_service.is_available.return_value = True
        self.module.promo_service.validate.return_value = SimpleNamespace(
            is_valid=True, promo=SimpleNamespace(code='P', discount_percent=10))
        quote = await self.module.quote_payment_route(self.request(promo_code='p'), self.user)
        self.assertIsNone(quote['autorenew_amount'])

    async def test_unsupported_tariff_hides_autorenew_in_quote(self):
        self.module.recurring_service.build_offer.side_effect = self.module.RecurringError('нет')
        quote = await self.module.quote_payment_route(self.request(), self.user)
        self.assertIsNone(quote['autorenew_amount'])

    async def test_create_with_autorenew_starts_subscription_not_one_time_invoice(self):
        result = await self.module.create_payment_route(self.request(autorenew=True, extra_devices=2), self.user)
        self.assertEqual(result['payment_url'], SUB_URL)
        self.assertTrue(result['autorenew'])
        self.assertEqual(result['final_amount'], 399)
        self.module.recurring_service.start.assert_awaited_once_with(42, 5, 2, source='web')
        self.module.payment_service.create_checkout.assert_not_awaited()

    async def test_autorenew_with_promo_is_refused_and_promo_is_not_claimed(self):
        self.module.promo_service.validate.return_value = SimpleNamespace(
            is_valid=True, promo=SimpleNamespace(code='P', discount_percent=10))
        with self.assertRaises(HTTPException) as raised:
            await self.module.create_payment_route(self.request(autorenew=True, promo_code='p'), self.user)
        self.assertEqual(raised.exception.status_code, 400)
        self.module.recurring_service.start.assert_not_awaited()
        self.module.promo_service.apply.assert_not_awaited()

    async def test_errors_map_to_http_statuses(self):
        cases = [(self.module.RecurringError('Автопродление уже подключено'), 400),
                 (self.module.PendingPaymentError('счёт'), 409),
                 (PlategaError('down'), 503)]
        for error, status in cases:
            self.module.recurring_service.start.side_effect = error
            with self.assertRaises(HTTPException) as raised:
                await self.module.create_payment_route(self.request(autorenew=True), self.user)
            self.assertEqual(raised.exception.status_code, status)

    async def test_status_route_describes_subscription(self):
        self.assertEqual(await self.module.autorenew_status_route(self.user), {'status': None})
        self.sub.status = 'active'
        self.sub.next_charge_at = datetime(2026, 12, 9)
        self.module.recurring_service.get_view.return_value = self.sub
        view = await self.module.autorenew_status_route(self.user)
        self.assertEqual((view['status'], view['amount'], view['period_days'], view['next_charge_at']),
                         ('active', 399, 90, '09.12.2026'))
        self.assertIsNone(view['payment_url'])  # ссылка нужна только пока ждём привязку
        with self.assertRaises(HTTPException):
            await self.module.autorenew_status_route(None)

    async def test_cancel_route(self):
        self.module.recurring_service.cancel.return_value = self.sub
        self.assertEqual(await self.module.autorenew_cancel_route(self.user), {'cancelled': True})
        self.module.recurring_service.cancel.return_value = None
        with self.assertRaises(HTTPException) as raised:
            await self.module.autorenew_cancel_route(self.user)
        self.assertEqual(raised.exception.status_code, 404)
        self.module.recurring_service.cancel.side_effect = PlategaError('down')
        with self.assertRaises(HTTPException) as raised:
            await self.module.autorenew_cancel_route(self.user)
        self.assertEqual(raised.exception.status_code, 503)


if __name__ == '__main__':
    unittest.main()
