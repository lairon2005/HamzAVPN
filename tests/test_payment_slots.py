"""Атомарное зачисление, возврат и восстановление выдачи без реальных платежей."""
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from sqlalchemy import update, select
from payment_test_support import PaymentDatabaseTests


class AtomicFulfillmentTests(PaymentDatabaseTests):
    async def test_confirmed_subscription_and_referral_are_applied_once(self):
        await self.create_invoice(extra_devices=2)
        before = await self.user()
        ref_before = await self.user(77)
        first = await self.service.process_successful_payment('p-1', 399.0)
        second = await self.service.process_successful_payment('p-1', 399.0)
        user = await self.user()
        referrer = await self.user(77)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(user.subscription_end_date, before.subscription_end_date + timedelta(days=90))
        self.assertEqual(user.extra_devices, 2)
        self.assertEqual(user.subscription_data_limit_gb, 100)
        self.assertTrue(user.is_first_payment_made)
        self.assertEqual(referrer.subscription_end_date, ref_before.subscription_end_date + timedelta(days=7))
        self.assertEqual(referrer.referral_bonus_days, 7)
        self.assertFalse((await self.payment()).sync_pending)

    async def test_devices_do_not_extend_subscription_or_award_referral(self):
        await self.create_invoice(tariff_id=None, kind='devices', extra_devices=2,
                                  original_amount=98, final_amount=98)
        before = await self.user()
        result = await self.service.process_successful_payment('p-1', 98)
        await self.service.process_successful_payment('p-1', 98)
        user = await self.user()
        self.assertEqual(result.kind, 'devices')
        self.assertEqual(user.extra_devices, 2)
        self.assertEqual(user.subscription_end_date, before.subscription_end_date)
        self.assertFalse(user.is_first_payment_made)
        self.assertEqual((await self.user(77)).referral_bonus_days, 0)

    async def test_amount_and_currency_mismatch_do_not_poison_pending_invoice(self):
        await self.create_invoice()
        before = await self.user()
        for amount, currency in [(398.99, 'RUB'), (399, 'USD'), ('NaN', 'RUB')]:
            with self.assertRaises(ValueError):
                await self.service.process_successful_payment('p-1', amount, currency)
        self.assertEqual((await self.payment()).status, 'pending')
        self.assertEqual((await self.user()).subscription_end_date, before.subscription_end_date)
        await self.service.process_successful_payment('p-1', 399)
        self.assertEqual((await self.payment()).status, 'succeeded')

    async def test_payment_after_local_cancellation_is_credited(self):
        await self.create_invoice()
        self.assertIsNotNone(await self.service.cancel_pending_payment(42))
        before = await self.user()
        await self.service.process_successful_payment('p-1', 399)
        self.assertEqual((await self.user()).subscription_end_date,
                         before.subscription_end_date + timedelta(days=90))
        self.assertFalse(await self.repo.cancel_if_pending('p-1'))
        self.assertEqual((await self.payment()).status, 'succeeded')

    async def test_panel_failure_retries_absolute_expiry_without_extra_days(self):
        await self.create_invoice(extra_devices=2)
        self.panel.update_user.side_effect = RuntimeError('panel unavailable')
        with self.assertRaises(RuntimeError):
            await self.service.process_successful_payment('p-1', 399)
        paid_user = await self.user()
        paid_referrer = await self.user(77)
        self.assertEqual((await self.payment()).status, 'succeeded')
        self.assertTrue((await self.payment()).sync_pending)
        self.panel.update_user.side_effect = None
        await self.service.process_successful_payment('p-1', 399)
        self.assertEqual((await self.user()).subscription_end_date, paid_user.subscription_end_date)
        self.assertEqual((await self.user(77)).subscription_end_date, paid_referrer.subscription_end_date)
        self.assertFalse((await self.payment()).sync_pending)
        expiry = paid_user.subscription_end_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        self.assertEqual(self.panel.update_user.call_args_list[-2].kwargs['expire_at'], expiry)

    async def test_snapshot_preserves_paid_duration_when_tariff_changes(self):
        await self.create_invoice()
        async with self.sessions() as session:
            await session.execute(update(self.models.Tariff).where(self.models.Tariff.id == 5)
                                  .values(duration_days=1, data_limit_gb=1))
            await session.commit()
        before = await self.user()
        await self.service.process_successful_payment('p-1', 399)
        self.assertEqual((await self.user()).subscription_end_date,
                         before.subscription_end_date + timedelta(days=90))
        self.assertEqual((await self.user()).subscription_data_limit_gb, 100)

    async def test_only_one_pending_platega_invoice_can_be_created(self):
        await self.create_invoice()
        with self.assertRaises(Exception):
            await self.create_invoice('p-2')
        self.assertEqual((await self.service.get_pending_payment(42)).provider_payment_id, 'p-1')

    async def test_subscription_refund_and_duplicate_are_applied_once(self):
        await self.create_invoice()
        before = await self.user()
        await self.service.process_successful_payment('p-1', 399)
        refunded = await self.service.process_refund('p-1')
        self.assertIsNotNone(refunded)
        self.assertIsNone(await self.service.process_refund('p-1'))
        self.assertEqual((await self.user()).subscription_end_date, before.subscription_end_date)
        self.assertEqual((await self.payment()).status, 'refunded')
        self.assertIsNone(await self.service.process_successful_payment('p-1', 399))

    async def test_device_refund_removes_slots_once_without_deducting_days(self):
        await self.create_invoice(tariff_id=None, kind='devices', extra_devices=2,
                                  original_amount=98, final_amount=98)
        before = await self.user()
        await self.service.process_successful_payment('p-1', 98)
        await self.service.process_refund('p-1')
        await self.service.process_refund('p-1')
        self.assertEqual((await self.user()).extra_devices, 0)
        self.assertEqual((await self.user()).subscription_end_date, before.subscription_end_date)

    async def test_refund_before_confirmation_never_issues_service(self):
        await self.create_invoice()
        before = await self.user()
        await self.service.process_refund('p-1')
        self.assertIsNone(await self.service.process_successful_payment('p-1', 399))
        self.assertEqual((await self.user()).subscription_end_date, before.subscription_end_date)

    async def test_refund_panel_failure_can_recover_without_second_deduction(self):
        await self.create_invoice()
        await self.service.process_successful_payment('p-1', 399)
        self.panel.update_user.side_effect = RuntimeError('panel unavailable')
        with self.assertRaises(RuntimeError):
            await self.service.process_refund('p-1')
        date_after_refund = (await self.user()).subscription_end_date
        self.assertEqual((await self.payment()).status, 'refunded')
        self.panel.update_user.side_effect = None
        await self.service.process_refund('p-1')
        self.assertEqual((await self.user()).subscription_end_date, date_after_refund)
        self.assertFalse((await self.payment()).sync_pending)


class AtomicStarsTests(PaymentDatabaseTests):
    async def test_stars_duplicate_preserves_days_and_referral(self):
        before = await self.user()
        first = await self.service.process_stars_payment(42, 5, 150, 'CHARGE-1')
        duplicate = await self.service.process_stars_payment(42, 5, 150, 'CHARGE-1')
        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertEqual(first.payment.currency, 'XTR')
        self.assertEqual(first.payment.source, 'stars')
        self.assertEqual((await self.user()).subscription_end_date,
                         before.subscription_end_date + timedelta(days=90))
        self.assertEqual((await self.user(77)).referral_bonus_days, 7)
        self.assertEqual((await self.repo.get_total_revenue())['revenue'], 0)
        self.assertEqual((await self.repo.get_stars_revenue_total())['stars'], 150)

    async def test_stars_rejects_price_mismatch_before_record_creation(self):
        with self.assertRaises(ValueError):
            await self.service.process_stars_payment(42, 5, 1, 'CHARGE-2')
        self.assertIsNone(await self.payment('stars:CHARGE-2'))

    async def test_stars_panel_failure_retries_without_second_credit(self):
        self.panel.update_user.side_effect = RuntimeError('panel unavailable')
        with self.assertRaises(RuntimeError):
            await self.service.process_stars_payment(42, 5, 150, 'CHARGE-3')
        paid_expiry = (await self.user()).subscription_end_date
        self.panel.update_user.side_effect = None
        await self.service.process_stars_payment(42, 5, 150, 'CHARGE-3')
        self.assertEqual((await self.user()).subscription_end_date, paid_expiry)


if __name__ == '__main__':
    unittest.main()

class CheckoutReservationTests(PaymentDatabaseTests):
    async def test_external_payment_is_requested_only_after_order_is_durable(self):
        import types
        from unittest.mock import patch
        module = types.ModuleType('tgbot.services.payment')
        async def create_payment(**kwargs):
            pending = await self.service.get_pending_payment(42)
            self.assertTrue(pending.provider_payment_id.startswith('order:'))
            self.assertEqual(pending.order_id, kwargs['metadata']['orderId'])
            return 'https://pay.platega.io/payment', 'remote-1'
        module.create_payment = create_payment
        with patch.dict('sys.modules', {'tgbot.services.payment': module}):
            url, identifier = await self.service.create_checkout(42, 5, 399, 399,
                'Подписка HamzAVPN', 'https://example.test/')
        self.assertEqual(identifier, 'remote-1')
        self.assertEqual((await self.payment(identifier)).payment_url, url)
        self.assertIsNotNone((await self.payment(identifier)).order_id)

    async def test_parallel_checkout_creates_one_external_transaction(self):
        import asyncio
        import types
        from unittest.mock import patch
        module = types.ModuleType('tgbot.services.payment')
        module.create_payment = AsyncMock(return_value=('https://pay.platega.io/payment', 'remote-2'))
        with patch.dict('sys.modules', {'tgbot.services.payment': module}):
            results = await asyncio.gather(*[
                self.service.create_checkout(42, 5, 399, 399, 'Подписка', 'https://example.test/')
                for _ in range(2)], return_exceptions=True)
        self.assertEqual(sum(isinstance(x, tuple) for x in results), 1)
        self.assertEqual(sum(isinstance(x, self.service_module.PendingPaymentError) for x in results), 1)
        self.assertEqual(module.create_payment.await_count, 1)

    async def test_failed_creation_preserves_recoverable_order(self):
        import types
        from unittest.mock import patch
        captured = []
        module = types.ModuleType('tgbot.services.payment')
        async def create_payment(**kwargs):
            captured.append(kwargs['metadata']['orderId'])
            raise RuntimeError('network timeout after acceptance')
        module.create_payment = create_payment
        with patch.dict('sys.modules', {'tgbot.services.payment': module}):
            with self.assertRaises(RuntimeError):
                await self.service.create_checkout(42, 5, 399, 399, 'Подписка', 'https://example.test/')
        reservation = 'order:' + captured[0]
        self.assertEqual((await self.payment(reservation)).status, 'cancelled')
        await self.repo.bind_transaction(reservation, 'remote-3')
        await self.service.process_successful_payment('remote-3', 399)
        self.assertEqual((await self.payment('remote-3')).status, 'succeeded')


class PostgreSQLConcurrencyTests(PaymentDatabaseTests):
    async def test_simultaneous_confirmations_credit_one_subscription_and_one_referral(self):
        if self.engine.dialect.name != 'postgresql':
            self.skipTest('Настоящие row locks проверяются при PAYMENT_TEST_DSN PostgreSQL')
        import asyncio
        await self.create_invoice(extra_devices=2)
        before = await self.user()
        await asyncio.gather(*[self.service.process_successful_payment('p-1', 399) for _ in range(6)])
        self.assertEqual((await self.user()).subscription_end_date,
                         before.subscription_end_date + timedelta(days=90))
        self.assertEqual((await self.user()).extra_devices, 2)
        self.assertEqual((await self.user(77)).referral_bonus_days, 7)
        self.assertEqual(self.panel.reset_user_traffic.await_count, 1)

    async def test_concurrent_chargebacks_deduct_once(self):
        if self.engine.dialect.name != 'postgresql':
            self.skipTest('Настоящие row locks проверяются при PAYMENT_TEST_DSN PostgreSQL')
        import asyncio
        await self.create_invoice()
        before = await self.user()
        await self.service.process_successful_payment('p-1', 399)
        await asyncio.gather(*[self.service.process_refund('p-1') for _ in range(6)])
        self.assertEqual((await self.user()).subscription_end_date, before.subscription_end_date)
        self.assertEqual((await self.payment()).status, 'refunded')


class QuotaAndReconciliationTests(PaymentDatabaseTests):
    async def test_fresh_paid_quota_is_reset_once_and_bonus_quota_is_preserved(self):
        await self.create_invoice()
        await self.service.process_successful_payment('p-1', 399)
        await self.service.process_successful_payment('p-1', 399)
        self.panel.reset_user_traffic.assert_awaited_once_with('uuid-user_42')
        self.assertTrue((await self.payment()).traffic_reset_done)

    async def test_failed_quota_reset_remains_pending_and_is_retried(self):
        await self.create_invoice()
        self.panel.reset_user_traffic.side_effect = RuntimeError('panel unavailable')
        with self.assertRaises(RuntimeError):
            await self.service.process_successful_payment('p-1', 399)
        expiry = (await self.user()).subscription_end_date
        self.assertFalse((await self.payment()).traffic_reset_done)
        self.assertTrue((await self.payment()).sync_pending)
        self.panel.reset_user_traffic.side_effect = None
        await self.service.process_successful_payment('p-1', 399)
        self.assertEqual((await self.user()).subscription_end_date, expiry)
        self.assertTrue((await self.payment()).traffic_reset_done)

    async def test_reconcile_detects_missed_chargeback_for_successful_payment(self):
        import types
        from unittest.mock import patch
        await self.create_invoice()
        before = await self.user()
        await self.service.process_successful_payment('p-1', 399)
        client = SimpleNamespace(configured=True, get_payment=AsyncMock(return_value={
            'status': 'CHARGEBACKED', 'paymentDetails': {'amount': 399, 'currency': 'RUB'}}))
        with patch.dict('sys.modules', {'loader': SimpleNamespace(platega_client=client)}):
            await self.service.reconcile()
            await self.service.reconcile()
        self.assertEqual((await self.payment()).status, 'refunded')
        self.assertEqual((await self.user()).subscription_end_date, before.subscription_end_date)

class StarsSnapshotTests(PaymentDatabaseTests):
    async def test_invoice_snapshot_survives_tariff_price_and_duration_change(self):
        invoice = await self.service.prepare_stars_invoice(42, 5)
        async with self.sessions() as session:
            await session.execute(update(self.models.Tariff).where(self.models.Tariff.id == 5)
                                  .values(price_stars=200, duration_days=5))
            await session.commit()
        before = await self.user()
        result = await self.service.process_stars_payment(42, 5, 150, 'SNAPSHOT-1', invoice.order_id)
        self.assertEqual(result.payment.duration_days, 90)
        self.assertEqual((await self.user()).subscription_end_date,
                         before.subscription_end_date + timedelta(days=90))
        self.assertIsNone(await self.service.process_stars_payment(42, 5, 150, 'SNAPSHOT-1', invoice.order_id))

    async def test_unpaid_stars_invoice_is_not_reconciled_as_paid(self):
        invoice = await self.service.prepare_stars_invoice(42, 5)
        self.assertEqual(await self.repo.get_pending_stars(), [])
        self.assertEqual(invoice.status, 'pending')

    async def test_paid_stars_charge_cannot_be_reused_for_other_user_or_amount(self):
        invoice = await self.service.prepare_stars_invoice(42, 5)
        for uid, amount in [(77, 150), (42, 1)]:
            with self.assertRaises(ValueError):
                await self.service.process_stars_payment(uid, 5, amount, 'SNAPSHOT-2', invoice.order_id)
        self.assertIsNone((await self.payment('order:' + invoice.order_id)).telegram_payment_charge_id)
