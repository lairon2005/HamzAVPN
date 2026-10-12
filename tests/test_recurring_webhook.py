"""Callback'и автопродления: аутентификация, сверка через API, идемпотентность, ответы 200/401/409/503."""
import sys
import types
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from platega.client import PlategaClient, PlategaError
from test_stars_payment import _load_module

SUB = '00000000-0000-0000-0000-0000000000a1'
CHARGE = '00000000-0000-0000-0000-0000000000c1'
HEADERS = {'X-MerchantId': 'merchant', 'X-Secret': 'secret'}


def charge_body(**over):
    body = {'Id': CHARGE, 'Amount': 399, 'Currency': 'RUB', 'Status': 'CONFIRMED',
            'PaymentMethod': 6, 'SubscriptionId': SUB, 'NextChargeAt': '2026-12-09T09:10:00Z'}
    body.update(over)
    return body


class RecurringWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sub = SimpleNamespace(platega_subscription_id=SUB, user_id=42, amount=399, charges_ok=0,
                                   status='active', tariff_id=5)
        self.client = PlategaClient('merchant', 'secret')
        self.client.get_subscription = AsyncMock(return_value={'id': SUB, 'status': 'Active', 'amount': 399})
        self.client.get_payment = AsyncMock(return_value={
            'id': CHARGE, 'status': 'CONFIRMED', 'paymentDetails': {'amount': 399, 'currency': 'RUB'}})
        self.result = SimpleNamespace(
            tariff=SimpleNamespace(name='Три месяца'), extension=SimpleNamespace(is_new_user=False),
            payment=SimpleNamespace(final_amount=399.0, user_id=42), referrer_id=None, is_first_payment=False)
        self.user_repo = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
            subscription_end_date=datetime(2027, 1, 1))))
        self.recurring_repo = SimpleNamespace(get_by_platega_id=AsyncMock(return_value=self.sub))
        self.recurring = SimpleNamespace(
            on_charge_confirmed=AsyncMock(return_value=self.result),
            on_charge_failed=AsyncMock(return_value=True),
            apply_remote_state=AsyncMock(return_value=('pending', 'active')))
        self.payments = SimpleNamespace(process_refund=AsyncMock(return_value=None))
        self.logger = Mock()
        database = types.ModuleType('database')
        database.user_repo, database.recurring_repo = self.user_repo, self.recurring_repo
        loader = types.ModuleType('loader')
        loader.logger, loader.platega_client = self.logger, self.client
        services = types.ModuleType('tgbot.services')
        services.payment_service, services.recurring_service = self.payments, self.recurring
        self.module = _load_module('recurring_webhook_under_test', 'tgbot/handlers/recurring_webhook.py',
                                   {'database': database, 'loader': loader, 'tgbot.services': services})
        # Ленивые импорты внутри обработчиков.
        self.webhook = types.ModuleType('tgbot.handlers.webhook_handlers')
        self.webhook._log_transaction = AsyncMock()
        self.webhook._log_refund = AsyncMock()
        self.webhook._notify_tg_user = AsyncMock()
        self.webhook.notify_referrer = AsyncMock()
        keyboards = types.ModuleType('tgbot.keyboards.inline')
        keyboards.autorenew_manage_keyboard = lambda: 'manage-kb'
        keyboards.autorenew_prompt_keyboard = lambda: 'prompt-kb'
        patcher = patch.dict(sys.modules, {
            'tgbot.handlers.webhook_handlers': self.webhook, 'tgbot.keyboards.inline': keyboards})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.request = SimpleNamespace(app={'bot': self.bot, 'remnawave': Mock()})

    async def asyncTearDown(self):
        await self.client.aclose()

    async def call(self, body, authenticated=True):
        data = {str(k).lower(): v for k, v in body.items()}
        from platega.client import callback_kind
        return await self.module.handle_subscription_callback(
            self.request, data, callback_kind(data), authenticated)

    def sent_texts(self):
        return [call.args[1] for call in self.bot.send_message.await_args_list]

    # ----------------------------------------------------------- списания

    async def test_charge_without_secret_is_refused_before_any_work(self):
        response = await self.call(charge_body(), authenticated=False)
        self.assertEqual(response.status, 401)
        self.client.get_subscription.assert_not_awaited()
        self.recurring.on_charge_confirmed.assert_not_awaited()

    async def test_confirmed_charge_is_credited_with_values_and_user_is_told(self):
        response = await self.call(charge_body())
        self.assertEqual(response.status, 200)
        args = self.recurring.on_charge_confirmed.await_args.args
        self.assertEqual(args[:4], (self.sub, CHARGE, Decimal('399.00'), 'RUB'))
        self.assertEqual(args[4].year, 2026)
        self.assertTrue(any('Автопродление подключено' in t for t in self.sent_texts()))
        self.webhook._notify_tg_user.assert_awaited_once()  # первое списание = первая оплата
        self.webhook.notify_referrer.assert_awaited_once()
        self.webhook._log_transaction.assert_awaited_once()

    async def test_later_charge_sends_short_renewal_notice_without_profile(self):
        self.sub.charges_ok = 2
        await self.call(charge_body())
        self.webhook._notify_tg_user.assert_not_awaited()
        self.assertTrue(any('продлена автоматически' in t for t in self.sent_texts()))

    async def test_repeated_callback_is_acknowledged_without_second_notification(self):
        self.recurring.on_charge_confirmed.return_value = None
        response = await self.call(charge_body())
        self.assertEqual(response.status, 200)
        self.bot.send_message.assert_not_awaited()
        self.webhook._log_transaction.assert_not_awaited()

    async def test_amount_mismatch_is_rejected_without_crediting(self):
        response = await self.call(charge_body(Amount=1))
        self.assertEqual(response.status, 409)
        self.recurring.on_charge_confirmed.assert_not_awaited()

    async def test_transaction_check_disagreeing_with_callback_is_rejected(self):
        self.client.get_payment.return_value = {
            'id': CHARGE, 'status': 'CANCELED', 'paymentDetails': {'amount': 399, 'currency': 'RUB'}}
        self.assertEqual((await self.call(charge_body())).status, 409)
        self.recurring.on_charge_confirmed.assert_not_awaited()

    async def test_unavailable_transaction_lookup_does_not_lose_the_money(self):
        self.client.get_payment.side_effect = PlategaError('404')
        self.assertEqual((await self.call(charge_body())).status, 200)
        self.recurring.on_charge_confirmed.assert_awaited_once()

    async def test_subscription_amount_changed_in_platega_is_rejected(self):
        self.client.get_subscription.return_value = {'id': SUB, 'status': 'Active', 'amount': 500}
        self.assertEqual((await self.call(charge_body())).status, 409)
        self.recurring.on_charge_confirmed.assert_not_awaited()

    async def test_platega_api_down_asks_for_retry(self):
        self.client.get_subscription.side_effect = PlategaError('down')
        self.assertEqual((await self.call(charge_body())).status, 503)

    async def test_internal_failure_asks_for_retry(self):
        self.recurring.on_charge_confirmed.side_effect = RuntimeError('db down')
        self.assertEqual((await self.call(charge_body())).status, 503)

    async def test_amount_mismatch_in_service_is_conflict(self):
        self.recurring.on_charge_confirmed.side_effect = ValueError('сумма')
        self.assertEqual((await self.call(charge_body())).status, 409)

    async def test_unknown_subscription_is_acknowledged_and_reported(self):
        self.recurring_repo.get_by_platega_id.return_value = None
        self.assertEqual((await self.call(charge_body())).status, 200)
        self.logger.error.assert_called()
        self.recurring.on_charge_confirmed.assert_not_awaited()

    async def test_malformed_charge_is_bad_request(self):
        for body in (charge_body(Id='не uuid'), charge_body(Currency='USD'), charge_body(Status='PENDING')):
            self.assertEqual((await self.call(body)).status, 400)

    async def test_failed_charge_notifies_only_on_transition(self):
        await self.call(charge_body(Status='CANCELED', NextChargeAt=None))
        self.assertTrue(any('Не удалось списать' in t for t in self.sent_texts()))
        self.bot.send_message.reset_mock()
        self.recurring.on_charge_failed.return_value = False
        await self.call(charge_body(Status='CANCELED', NextChargeAt=None))
        self.bot.send_message.assert_not_awaited()

    async def test_chargeback_refunds_the_charge_payment(self):
        self.payments.process_refund.return_value = SimpleNamespace(user_id=42, final_amount=399.0)
        await self.call(charge_body(Status='CHARGEBACKED'))
        self.payments.process_refund.assert_awaited_once_with(CHARGE)
        self.webhook._log_refund.assert_awaited_once()

    # ----------------------------------------------------------- статус подписки

    async def test_status_callback_trusts_api_not_body(self):
        # В теле «активирована», но Platega говорит, что подписка отменена.
        remote = {'id': SUB, 'status': 'Cancelled', 'amount': 399}
        self.client.get_subscription.return_value = remote
        self.recurring.apply_remote_state.return_value = ('active', 'cancelled')
        response = await self.call({'Id': SUB, 'Status': 'SUBSCRIPTION_ACTIVATED', 'SubscriptionId': SUB},
                                   authenticated=False)
        self.assertEqual(response.status, 200)
        self.recurring.apply_remote_state.assert_awaited_once_with(self.sub, remote)
        self.assertTrue(any('Автопродление отключено' in t for t in self.sent_texts()))

    async def test_activation_notice_and_no_noise_without_transition(self):
        await self.call({'Id': SUB, 'Status': 'SUBSCRIPTION_ACTIVATED'}, authenticated=False)
        self.assertTrue(any('Автопродление подключено' in t for t in self.sent_texts()))
        self.bot.send_message.reset_mock()
        self.recurring.apply_remote_state.return_value = ('active', 'active')
        await self.call({'Id': SUB, 'Status': 'SUBSCRIPTION_ACTIVATED'}, authenticated=False)
        self.bot.send_message.assert_not_awaited()

    async def test_status_for_unknown_subscription_makes_no_api_call(self):
        self.recurring_repo.get_by_platega_id.return_value = None
        self.assertEqual((await self.call({'Id': SUB, 'Status': 'SUBSCRIPTION_FAILED'}, authenticated=False)).status, 200)
        self.client.get_subscription.assert_not_awaited()

    async def test_status_callback_with_api_down_asks_for_retry(self):
        self.client.get_subscription.side_effect = PlategaError('down')
        self.assertEqual((await self.call({'Id': SUB, 'Status': 'SUBSCRIPTION_PAST_DUE'}, authenticated=False)).status, 503)

    async def test_web_users_are_not_messaged(self):
        self.sub.user_id = -5
        await self.call(charge_body(Status='CANCELED'))
        self.bot.send_message.assert_not_awaited()


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    """platega_webhook_handler: куда уходит какой callback."""

    async def asyncSetUp(self):
        import test_platega_callback as base
        self.base = base.CallbackTests('test_verified_confirmed_is_credited_with_local_amount')
        await self.base.asyncSetUp()
        self.handler = AsyncMock(return_value=SimpleNamespace(status=200))
        fake = types.ModuleType('tgbot.handlers.recurring_webhook')
        fake.handle_subscription_callback = self.handler
        patcher = patch.dict(sys.modules, {'tgbot.handlers.recurring_webhook': fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.base.asyncTearDown()

    async def test_status_callback_without_headers_reaches_subscription_handler(self):
        request = self.base.request(headers={}, body={'Id': SUB, 'Status': 'SUBSCRIPTION_ACTIVATED'})
        await self.base.module.platega_webhook_handler(request)
        args = self.handler.await_args.args
        self.assertEqual(args[2:], ('subscription_status', False))

    async def test_charge_with_secret_reaches_subscription_handler_authenticated(self):
        request = self.base.request(body=charge_body())
        await self.base.module.platega_webhook_handler(request)
        self.assertEqual(self.handler.await_args.args[2:], ('subscription_charge', True))
        self.assertEqual(self.handler.await_args.args[1]['subscriptionid'], SUB)

    async def test_wrong_secret_is_refused_even_for_subscription_callbacks(self):
        request = self.base.request(headers={'X-MerchantId': 'merchant', 'X-Secret': 'wrong'}, body=charge_body())
        result = await self.base.module.platega_webhook_handler(request)
        self.assertEqual(result.status, 401)
        self.handler.assert_not_awaited()

    async def test_plain_payment_without_secret_stays_refused(self):
        result = await self.base.module.platega_webhook_handler(self.base.request(headers={}))
        self.assertEqual(result.status, 401)
        self.handler.assert_not_awaited()

    async def test_plain_payment_with_uppercase_keys_still_handled(self):
        body = {'Id': '00000000-0000-0000-0000-000000000001', 'Amount': 399, 'Currency': 'RUB',
                'Status': 'CONFIRMED', 'PaymentMethod': 2}
        result = await self.base.module.platega_webhook_handler(self.base.request(body=body))
        self.assertEqual(result.status, 200)
        self.handler.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
