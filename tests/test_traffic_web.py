"""Реальные маршруты и расчёт: серверная сумма и запрет поддельных параметров."""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from fastapi import HTTPException
from test_stars_payment import load_payment_router_module
from real_traffic_pricing import real_traffic_pricing


class TrafficWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_payment_router_module()
        self.module.payment_service.has_pending_payment.return_value = False
        self.module.payment_service.create_checkout = AsyncMock(return_value=('https://example.com/pay', 'id'))
        self.module.traffic_service.quote = AsyncMock(return_value=SimpleNamespace(
            ok=True, price=98.0, packs=2, added_gb=200, total_limit_gb=700, remaining_days=30))
        self.user = SimpleNamespace(user_id=42, subscription_end_date=datetime.now()+timedelta(days=30))

    async def test_quote_and_creation_use_server_amount_and_store_only_traffic(self):
        payload = self.module.TrafficRequest(packs=2, source='tma')
        quote = await self.module.quote_traffic_payment(payload, self.user)
        response = await self.module.create_traffic_payment(payload, self.user)
        self.assertEqual(quote['final_amount'], response['final_amount'])
        args = self.module.payment_service.create_checkout.await_args.kwargs
        self.assertEqual((args['kind'], args['tariff_id'], args['extra_traffic_gb'], args['final_amount']),
                         ('traffic', None, 200, 98))
        self.assertEqual(args['source'], 'tma')
        self.assertNotIn('extra_devices', args)

    async def test_no_auth_no_payment(self):
        for route in [self.module.quote_traffic_payment, self.module.create_traffic_payment]:
            with self.assertRaises(HTTPException) as raised:
                await route(self.module.TrafficRequest(), None)
            self.assertEqual(raised.exception.status_code, 401)
        self.module.payment_service.create_checkout.assert_not_awaited()

    async def test_pending_payment_conflict_never_calls_gateway(self):
        self.module.payment_service.has_pending_payment.return_value = True
        with self.assertRaises(HTTPException) as raised:
            await self.module.create_traffic_payment(self.module.TrafficRequest(), self.user)
        self.assertEqual(raised.exception.status_code, 409)
        self.module.traffic_service.quote.assert_not_awaited()
        self.module.payment_service.create_checkout.assert_not_awaited()

    async def test_ineligible_quote_never_calls_gateway(self):
        self.module.traffic_service.quote.return_value = SimpleNamespace(ok=False, error='Подписка истекла')
        with self.assertRaises(HTTPException) as raised:
            await self.module.create_traffic_payment(self.module.TrafficRequest(), self.user)
        self.assertEqual(raised.exception.status_code, 400)
        self.module.payment_service.create_checkout.assert_not_awaited()

    async def test_pending_race_is_reported_as_conflict(self):
        self.module.payment_service.create_checkout.side_effect = self.module.PendingPaymentError()
        with self.assertRaises(HTTPException) as raised:
            await self.module.create_traffic_payment(self.module.TrafficRequest(), self.user)
        self.assertEqual(raised.exception.status_code, 409)

    async def test_renewal_total_and_invoice_include_traffic_without_promo_discount(self):
        self.module.tariff_repo.get_by_name_and_price.return_value = SimpleNamespace(
            id=5, name='Тариф', price=399, loyalty_price=None, duration_days=90,
            data_limit_gb=500, is_active=True)
        self.module.device_slot_service.settings.return_value = SimpleNamespace(price=49, max_extra=10)
        self.module.traffic_service.settings.return_value = real_traffic_pricing().TrafficSettings()
        self.module.promo_service.validate.return_value = SimpleNamespace(is_valid=True,
            promo=SimpleNamespace(code='SAVE', discount_percent=50))
        request = self.module.PaymentRequest(tariff_name='Тариф', price=399,
            extra_devices=1, extra_traffic_packs=2, promo_code='SAVE', discount_percent=99)
        quote = await self.module.quote_payment_route(request, self.user)
        self.assertEqual((quote['tariff_amount'], quote['slots_amount'], quote['traffic_amount']), (199.5,147,294))
        self.assertEqual(quote['final_amount'], 640.5)
        response = await self.module.create_payment_route(request, self.user)
        args = self.module.payment_service.create_checkout.await_args.kwargs
        self.assertEqual((args['extra_traffic_gb'], args['final_amount']), (200,640.5))
        self.assertEqual(response['final_amount'], quote['final_amount'])

    async def test_unlimited_and_quantity_cap_are_enforced_on_server(self):
        tariff = SimpleNamespace(id=5, name='Тариф', price=399, loyalty_price=None, duration_days=90,
                                 data_limit_gb=0, is_active=True)
        self.module.tariff_repo.get_by_name_and_price.return_value = tariff
        self.module.device_slot_service.settings.return_value = SimpleNamespace(price=49,max_extra=10)
        request = self.module.PaymentRequest(tariff_name='Тариф',price=399,extra_traffic_packs=999)
        self.assertEqual((await self.module.quote_payment_route(request,self.user))['extra_traffic_gb'],0)
        tariff.data_limit_gb=500
        self.assertEqual((await self.module.quote_payment_route(request,self.user))['extra_traffic_packs'],10)
        request.extra_traffic_packs=-1
        self.assertEqual((await self.module.quote_payment_route(request,self.user))['extra_traffic_gb'],0)

    async def test_old_client_renewal_implicitly_keeps_paid_traffic(self):
        self.user.extra_traffic_gb=200
        self.module.tariff_repo.get_by_name_and_price.return_value=SimpleNamespace(
            id=5,name='Тариф',price=399,loyalty_price=None,duration_days=90,data_limit_gb=500,is_active=True)
        self.module.device_slot_service.settings.return_value=SimpleNamespace(price=49,max_extra=10)
        request=self.module.PaymentRequest(tariff_name='Тариф',price=399)
        self.assertEqual((await self.module.quote_payment_route(request,self.user))['extra_traffic_gb'],200)
        request.extra_traffic_packs=0
        self.assertEqual((await self.module.quote_payment_route(request,self.user))['extra_traffic_gb'],0)
