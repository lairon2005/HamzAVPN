"""Сбои сверки не меняют оплату и не задерживают остальные счета навсегда.

Настоящие модели и SQLite проверяют финансовое состояние; внешние API подменены.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from payment_test_support import PaymentDatabaseTests
from platega.client import PlategaError


class PaymentReconcileErrorTests(PaymentDatabaseTests):
    async def _reconcile(self, client):
        with patch.dict('sys.modules', {'loader': SimpleNamespace(platega_client=client)}):
            await self.service.reconcile()

    async def _financial_snapshot(self):
        fields = ('subscription_end_date', 'subscription_data_limit_gb',
                  'extra_devices', 'is_first_payment_made', 'referral_bonus_days',
                  'remnawave_uuid')
        snapshot = []
        for identifier in (42, 77):
            user = await self.user(identifier)
            snapshot.append(tuple(getattr(user, field) for field in fields))
        return snapshot

    def _assert_panel_untouched(self):
        for operation in vars(self.panel).values():
            operation.assert_not_awaited()

    async def _assert_error_preserves_state(self, status, error):
        await self.create_invoice()
        if status == 'cancelled':
            await self.repo.cancel_if_pending('p-1')
        elif status == 'succeeded':
            await self.service.process_successful_payment('p-1', 399)
        before = await self._financial_snapshot()
        payment_before = await self.payment()
        for operation in vars(self.panel).values():
            operation.reset_mock()
        client = SimpleNamespace(configured=True, get_payment=AsyncMock(side_effect=error))

        await self._reconcile(client)

        payment_after = await self.payment()
        self.assertEqual(payment_after.status, status)
        self.assertEqual(payment_after.completed_at, payment_before.completed_at)
        self.assertEqual(payment_after.sync_pending, payment_before.sync_pending)
        self.assertEqual(payment_after.traffic_reset_done, payment_before.traffic_reset_done)
        self.assertIsNotNone(payment_after.last_checked_at)
        self.assertEqual(await self._financial_snapshot(), before)
        client.get_payment.assert_awaited_once_with('p-1')
        self._assert_panel_untouched()
        self.service_module.logger.warning.assert_called_once()
        self.service_module.logger.exception.assert_not_called()

    async def test_timeout_keeps_pending_invoice_unpaid(self):
        await self._assert_error_preserves_state('pending', PlategaError(
            'Время ожидания истекло', reason='timeout', retryable=True))

    async def test_server_error_does_not_reopen_cancelled_invoice(self):
        await self._assert_error_preserves_state('cancelled', PlategaError(
            'HTTP 503', reason='http', status_code=503, retryable=True))

    async def test_server_error_neither_refunds_nor_grants_paid_invoice_twice(self):
        await self._assert_error_preserves_state('succeeded', PlategaError(
            'HTTP 503', reason='http', status_code=503, retryable=True))

    async def test_global_error_stops_batch_and_failed_invoice_yields_to_unchecked_one(self):
        await self.create_invoice('p-failed')
        await self.create_invoice('p-next', user_id=77)
        before = await self._financial_snapshot()

        async def status(identifier):
            if identifier == 'p-failed':
                raise PlategaError('HTTP 503', reason='http', status_code=503, retryable=True)
            return {'status': 'CONFIRMED',
                    'paymentDetails': {'amount': 399, 'currency': 'RUB'}}

        client = SimpleNamespace(configured=True, get_payment=AsyncMock(side_effect=status))
        await self._reconcile(client)

        client.get_payment.assert_awaited_once_with('p-failed')
        self.assertEqual(await self._financial_snapshot(), before)
        self.assertEqual((await self.payment('p-failed')).status, 'pending')
        self.assertIsNotNone((await self.payment('p-failed')).last_checked_at)
        self.assertIsNone((await self.payment('p-next')).last_checked_at)
        self.assertEqual((await self.payment('p-next')).status, 'pending')
        self._assert_panel_untouched()
        self.assertEqual([p.provider_payment_id for p in await self.repo.get_unsettled()],
                         ['p-next', 'p-failed'])

        # Следующий запуск обслуживает ранее не проверенный счет первым.
        await self._reconcile(client)

        self.assertEqual(client.get_payment.await_args_list,
                         [call('p-failed'), call('p-next'), call('p-failed')])
        self.assertEqual((await self.payment('p-next')).status, 'succeeded')
        self.assertEqual((await self.user(77)).subscription_end_date,
                         before[1][0] + timedelta(days=90))
        self.assertEqual((await self.payment('p-failed')).status, 'pending')
        self.assertEqual((await self.user()).subscription_end_date, before[0][0])

    async def test_authentication_failure_stops_batch_even_when_not_retryable(self):
        await self.create_invoice('p-first')
        await self.create_invoice('p-second', user_id=77)
        before = await self._financial_snapshot()
        for code in (401, 403):
            with self.subTest(status_code=code):
                self.service_module.logger.reset_mock()
                client = SimpleNamespace(configured=True, get_payment=AsyncMock(side_effect=
                    PlategaError(f'HTTP {code}', reason='http', status_code=code, retryable=False)))

                await self._reconcile(client)

                self.assertEqual(client.get_payment.await_count, 1)
                self.assertIsNotNone((await self.payment(
                    client.get_payment.await_args.args[0])).last_checked_at)
                self.assertEqual(await self._financial_snapshot(), before)
                self.assertEqual((await self.payment('p-first')).status, 'pending')
                self.assertEqual((await self.payment('p-second')).status, 'pending')
                self._assert_panel_untouched()
                self.service_module.logger.error.assert_called_once()
                self.service_module.logger.exception.assert_not_called()

    async def test_missing_transaction_does_not_block_next_payment_or_duplicate_later_callback(self):
        await self.create_invoice('p-missing')
        await self.repo.cancel_if_pending('p-missing')
        await self.create_invoice('p-confirmed', extra_devices=2)
        before = await self._financial_snapshot()

        async def status(identifier):
            if identifier == 'p-missing':
                raise PlategaError('HTTP 404', reason='http', status_code=404, retryable=False)
            return {'status': 'CONFIRMED',
                    'paymentDetails': {'amount': 399, 'currency': 'RUB'}}

        client = SimpleNamespace(configured=True, get_payment=AsyncMock(side_effect=status))
        await self._reconcile(client)

        self.assertEqual(client.get_payment.await_args_list,
                         [call('p-missing'), call('p-confirmed')])
        missing = await self.payment('p-missing')
        self.assertEqual(missing.status, 'cancelled')
        self.assertIsNone(missing.completed_at)
        self.assertFalse(missing.traffic_reset_done)
        self.assertIsNotNone(missing.last_checked_at)
        self.assertEqual((await self.payment('p-confirmed')).status, 'succeeded')
        self.assertEqual((await self.user()).subscription_end_date,
                         before[0][0] + timedelta(days=90))
        self.assertEqual((await self.user(77)).subscription_end_date,
                         before[1][0] + timedelta(days=7))
        self.assertEqual((await self.user()).extra_devices, 2)
        self.panel.reset_user_traffic.assert_awaited_once()
        self.service_module.logger.error.assert_called_once()
        self.service_module.logger.exception.assert_not_called()

        after = await self._financial_snapshot()
        self.assertIsNone(await self.service.process_successful_payment('p-confirmed', 399))
        self.assertIsNone(await self.service.process_successful_payment('p-confirmed', 399))
        self.assertEqual(await self._financial_snapshot(), after)
        self.panel.reset_user_traffic.assert_awaited_once()
