"""Аутентификация и независимая проверка callback. Все внешние действия подменены."""
import json
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from platega.client import PlategaClient, PlategaError
from test_stars_payment import _load_module

ID = '00000000-0000-0000-0000-000000000001'
ORDER_ID = '00000000-0000-0000-0000-000000000002'


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = PlategaClient('merchant', 'secret')
        self.client.get_payment = AsyncMock(return_value={'id': ID, 'status': 'CONFIRMED',
            'paymentDetails': {'amount': 399, 'currency': 'RUB'}})
        self.payment = SimpleNamespace(provider_payment_id=ID, currency='RUB', final_amount=399, user_id=42)
        database = types.ModuleType('database')
        database.user_repo = AsyncMock()
        database.payment_repo = SimpleNamespace(
            get_by_provider_id=AsyncMock(return_value=self.payment),
            bind_transaction=AsyncMock(return_value=self.payment),
            cancel_if_pending=AsyncMock(),
        )
        services = types.ModuleType('tgbot.services')
        self.service = services.payment_service = SimpleNamespace(
            process_successful_payment=AsyncMock(return_value=None), process_refund=AsyncMock(return_value=None))
        loader = types.ModuleType('loader')
        loader.logger = Mock()
        loader.config = SimpleNamespace()
        loader.platega_client = self.client
        profile = types.ModuleType('tgbot.handlers.user.profile')
        profile.show_profile_logic = AsyncMock()
        self.stubs = {'database': database, 'tgbot.services': services,
                      'loader': loader, 'tgbot.handlers.user.profile': profile}
        self.module = _load_module('platega_callback_under_test', 'tgbot/handlers/webhook_handlers.py', self.stubs)
        self.repo = database.payment_repo

    async def asyncTearDown(self):
        await self.client.aclose()

    def request(self, body=None, headers=None):
        return SimpleNamespace(headers=headers if headers is not None else {
            'X-MerchantId': 'merchant', 'X-Secret': 'secret'},
            json=AsyncMock(return_value=body if body is not None else {
                'id': ID, 'amount': 399, 'currency': 'RUB', 'status': 'CONFIRMED', 'paymentMethod': 2}),
            app={'bot': AsyncMock(), 'remnawave': Mock()})

    async def test_unauthorized_callback_does_not_read_body_or_touch_api(self):
        request = self.request(headers={})
        result = await self.module.platega_webhook_handler(request)
        self.assertEqual(result.status, 401)
        request.json.assert_not_awaited()
        self.client.get_payment.assert_not_awaited()
        self.service.process_successful_payment.assert_not_awaited()

    async def test_verified_confirmed_is_credited_with_local_amount(self):
        result = await self.module.platega_webhook_handler(self.request())
        self.assertEqual(result.status, 200)
        self.client.get_payment.assert_awaited_once_with(ID)
        self.service.process_successful_payment.assert_awaited_once_with(ID, 399, 'RUB')

    async def test_callback_amount_currency_and_api_status_mismatch_are_rejected(self):
        self.client.get_payment.return_value['paymentDetails']['amount'] = 398
        result = await self.module.platega_webhook_handler(self.request())
        self.assertEqual(result.status, 409)
        self.service.process_successful_payment.assert_not_awaited()
        self.client.get_payment.return_value['paymentDetails']['amount'] = 399
        self.client.get_payment.return_value['status'] = 'PENDING'
        self.assertEqual((await self.module.platega_webhook_handler(self.request())).status, 409)
        self.assertEqual((await self.module.platega_webhook_handler(self.request(
            {'id': ID, 'amount': 399, 'currency': 'USD', 'status': 'CONFIRMED'}))).status, 400)

    async def test_unknown_or_malformed_callback_does_not_issue_service(self):
        for body in [[], {}, {'id': 'wrong'}, {'id': ID, 'amount': 'NaN', 'currency': 'RUB', 'status': 'CONFIRMED'}]:
            self.assertEqual((await self.module.platega_webhook_handler(self.request(body))).status, 400)
        self.service.process_successful_payment.assert_not_awaited()

    async def test_api_or_fulfillment_failure_requests_provider_retry(self):
        self.client.get_payment.side_effect = PlategaError('unavailable')
        self.assertEqual((await self.module.platega_webhook_handler(self.request())).status, 503)
        self.client.get_payment.side_effect = None
        self.service.process_successful_payment.side_effect = RuntimeError('panel unavailable')
        self.assertEqual((await self.module.platega_webhook_handler(self.request())).status, 503)

    async def test_canceled_never_downgrades_paid_payment_unconditionally(self):
        self.client.get_payment.return_value['status'] = 'CANCELED'
        result = await self.module.platega_webhook_handler(self.request(
            {'id': ID, 'amount': 399, 'currency': 'RUB', 'status': 'CANCELED'}))
        self.assertEqual(result.status, 200)
        self.repo.cancel_if_pending.assert_awaited_once_with(ID)
        self.service.process_successful_payment.assert_not_awaited()

    async def test_chargeback_uses_verified_payment_not_callback_metadata(self):
        self.client.get_payment.return_value['status'] = 'CHARGEBACKED'
        result = await self.module.platega_webhook_handler(self.request(
            {'id': ID, 'amount': 399, 'currency': 'RUB', 'status': 'CHARGEBACKED'}))
        self.assertEqual(result.status, 200)
        self.service.process_refund.assert_awaited_once_with(ID)

    async def test_callback_recovers_reserved_order_after_lost_creation_response(self):
        self.client.get_payment.return_value['payload'] = json.dumps({'orderId': ORDER_ID, 'userId': '42'})
        self.repo.get_by_provider_id.side_effect = [None, self.payment]
        result = await self.module.platega_webhook_handler(self.request())
        self.assertEqual(result.status, 200)
        self.repo.bind_transaction.assert_awaited_once_with('order:' + ORDER_ID, ID)
        self.service.process_successful_payment.assert_awaited_once_with(ID, 399, 'RUB')

    async def test_recovery_rejects_other_user_or_unmatched_amount(self):
        self.client.get_payment.return_value['payload'] = json.dumps({'orderId': ORDER_ID, 'userId': '999'})
        self.repo.get_by_provider_id.side_effect = [None, self.payment]
        result = await self.module.platega_webhook_handler(self.request())
        self.assertEqual(result.status, 409)
        self.repo.bind_transaction.assert_not_awaited()
        self.service.process_successful_payment.assert_not_awaited()
