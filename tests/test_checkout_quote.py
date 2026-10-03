"""Показанная цена и счет используют единый серверный расчет."""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from fastapi import HTTPException
from test_stars_payment import load_payment_router_module


class CheckoutQuoteTests(unittest.IsolatedAsyncioTestCase):
    def setup_quote(self, active=True):
        module = load_payment_router_module()
        module.tariff_repo.get_by_name_and_price.return_value = SimpleNamespace(
            id=5, name='Три месяца', is_active=True, price=399,
            loyalty_price=349, duration_days=90)
        module.device_slot_service.settings.return_value = SimpleNamespace(
            price=49, base_limit=3, max_extra=10)
        user = SimpleNamespace(user_id=42,
            subscription_end_date=datetime.now() + timedelta(days=5) if active else None)
        return module, user

    async def test_quote_resolves_loyalty_and_kopeks_on_server_without_claiming_promo(self):
        module, user = self.setup_quote()
        module.promo_service.validate.return_value = SimpleNamespace(is_valid=True,
            promo=SimpleNamespace(code='SAVE15', discount_percent=15))
        request = module.PaymentRequest(tariff_name='Три месяца', price=399,
            promo_code='save15', discount_percent=100, extra_devices=2)
        quote = await module.quote_payment_route(request, user)
        self.assertEqual(quote['tariff_amount'], 296.65)
        self.assertEqual(quote['slots_amount'], 294)
        self.assertEqual(quote['final_amount'], 590.65)
        self.assertEqual(quote['original_amount'], 643)
        self.assertEqual(quote['applied_discount'], 15)
        module.promo_service.apply.assert_not_awaited()
        module.payment_service.create_checkout.assert_not_awaited()

    async def test_client_cannot_assign_discount_without_valid_promo(self):
        module, user = self.setup_quote(active=False)
        quote = await module.quote_payment_route(module.PaymentRequest(
            tariff_name='Три месяца', price=399, discount_percent=99), user)
        self.assertEqual(quote['final_amount'], 399)
        self.assertEqual(quote['applied_discount'], 0)

    async def test_invalid_promo_does_not_create_or_claim_a_payment(self):
        module, user = self.setup_quote()
        module.promo_service.validate.return_value = SimpleNamespace(
            is_valid=False, error_message='Промокод закончился')
        with self.assertRaises(HTTPException) as raised:
            await module.quote_payment_route(module.PaymentRequest(
                tariff_name='Три месяца', price=399, promo_code='BAD'), user)
        self.assertEqual(raised.exception.status_code, 400)
        module.promo_service.apply.assert_not_awaited()
        module.payment_service.create_checkout.assert_not_awaited()

    async def test_create_returns_exact_same_amount_and_snapshot_as_quote(self):
        module, user = self.setup_quote()
        module.payment_service.has_pending_payment.return_value = False
        module.payment_service.create_checkout.return_value = ('https://pay.platega.io/', 'payment-1')
        request = module.PaymentRequest(tariff_name='Три месяца', price=399, extra_devices=2)
        quote = await module.quote_payment_route(request, user)
        response = await module.create_payment_route(request, user)
        self.assertEqual(response['final_amount'], quote['final_amount'])
        self.assertEqual(response['final_amount'], 643)
        call = module.payment_service.create_checkout.await_args.kwargs
        self.assertEqual(call['final_amount'], 643)
        self.assertEqual(call['extra_devices'], 2)
        self.assertEqual(call['return_url'], 'https://example.com/profile/')
