"""Реальная БД: выдача трафика, повтор callback, возврат и сбой панели."""
from datetime import timedelta
from unittest.mock import AsyncMock
from sqlalchemy import update
from payment_test_support import PaymentDatabaseTests


class TrafficPaymentsTests(PaymentDatabaseTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.sessions() as session:
            await session.execute(update(self.models.User).where(self.models.User.user_id == 42)
                .values(is_first_payment_made=True, subscription_data_limit_gb=500,
                        remnawave_uuid='uuid-user_42', vpn_username='user_42', extra_devices=2))
            await session.commit()

    async def invoice(self, identifier='traffic-1', gb=100):
        return await self.create_invoice(identifier, tariff_id=None, kind='traffic',
            original_amount=49, final_amount=49, extra_traffic_gb=gb)

    async def test_purchase_changes_only_traffic_and_duplicate_does_not_reset_usage(self):
        before = await self.user()
        await self.invoice()
        result = await self.service.process_successful_payment('traffic-1', 49)
        after = await self.user()
        self.assertEqual((after.extra_traffic_gb, after.extra_devices), (100, 2))
        self.assertEqual(after.subscription_end_date, before.subscription_end_date)
        self.assertEqual(after.referral_bonus_days, before.referral_bonus_days)
        self.assertEqual(result.kind, 'traffic')
        self.assertEqual(result.extra_traffic_gb, 100)
        self.panel.update_user.assert_awaited_once()
        self.assertEqual(self.panel.update_user.await_args.kwargs['traffic_limit_bytes'], 600 * 1024**3)
        self.panel.reset_user_traffic.assert_not_awaited()
        self.assertIsNone(await self.service.process_successful_payment('traffic-1', 49))
        self.assertEqual((await self.user()).extra_traffic_gb, 100)

    async def test_wrong_amount_or_currency_never_credits(self):
        await self.invoice()
        for amount, currency in [(48, 'RUB'), (49, 'USD')]:
            with self.assertRaises(ValueError):
                await self.service.process_successful_payment('traffic-1', amount, currency)
        self.assertEqual((await self.user()).extra_traffic_gb, 0)
        self.assertEqual((await self.payment('traffic-1')).status, 'pending')
        self.panel.update_user.assert_not_awaited()

    async def test_canceled_invoice_paid_later_is_credited_once(self):
        await self.invoice()
        await self.repo.cancel_if_pending('traffic-1')
        await self.service.process_successful_payment('traffic-1', 49)
        await self.service.process_successful_payment('traffic-1', 49)
        self.assertEqual((await self.user()).extra_traffic_gb, 100)

    async def test_panel_failure_keeps_retry_marker_without_duplicating_gb(self):
        await self.invoice()
        self.panel.update_user.side_effect = RuntimeError('panel offline')
        with self.assertRaises(RuntimeError):
            await self.service.process_successful_payment('traffic-1', 49)
        self.assertEqual((await self.user()).extra_traffic_gb, 100)
        self.assertTrue((await self.payment('traffic-1')).sync_pending)
        self.panel.update_user.side_effect = None
        await self.service.process_successful_payment('traffic-1', 49)
        self.assertEqual((await self.user()).extra_traffic_gb, 100)
        self.assertFalse((await self.payment('traffic-1')).sync_pending)
        self.panel.reset_user_traffic.assert_not_awaited()

    async def test_refund_removes_only_paid_traffic_once_and_leaves_dates(self):
        before = await self.user()
        await self.invoice('traffic-1', 100)
        await self.service.process_successful_payment('traffic-1', 49)
        await self.invoice('traffic-2', 200)
        await self.service.process_successful_payment('traffic-2', 49)
        await self.service.process_refund('traffic-1')
        await self.service.process_refund('traffic-1')
        after = await self.user()
        self.assertEqual(after.extra_traffic_gb, 200)
        self.assertEqual(after.subscription_end_date, before.subscription_end_date)
        self.assertEqual(after.extra_devices, 2)
        self.panel.reset_user_traffic.assert_not_awaited()

    async def test_refund_before_confirmation_prevents_grant(self):
        await self.invoice()
        await self.service.process_refund('traffic-1')
        self.assertIsNone(await self.service.process_successful_payment('traffic-1', 49))
        self.assertEqual((await self.user()).extra_traffic_gb, 0)

    async def test_subscription_snapshot_includes_traffic_even_if_catalog_changes(self):
        await self.create_invoice('renew', final_amount=546, extra_traffic_gb=200)
        async with self.sessions() as session:
            await session.execute(update(self.models.Tariff).where(self.models.Tariff.id == 5)
                .values(data_limit_gb=999, duration_days=365, price=999))
            await session.commit()
        await self.service.process_successful_payment('renew', 546)
        after = await self.user()
        self.assertEqual((after.subscription_data_limit_gb, after.extra_traffic_gb), (100, 200))
        self.assertEqual(self.panel.update_user.await_args.kwargs['traffic_limit_bytes'], 300 * 1024**3)
        self.panel.reset_user_traffic.assert_awaited_once()

    async def test_expiry_drops_extra_and_retry_does_not_reenable_vpn(self):
        await self.invoice()
        async with self.sessions() as session:
            await session.execute(update(self.models.User).where(self.models.User.user_id == 42)
                .values(subscription_end_date=self.now - timedelta(days=1)))
            await session.commit()
        await self.service.process_successful_payment('traffic-1', 49)
        self.assertEqual((await self.user()).extra_traffic_gb, 0)
        self.assertEqual(self.panel.update_user.await_args.kwargs['traffic_limit_bytes'], 500 * 1024**3)
        self.panel.disable_user.assert_awaited_once()
        self.panel.enable_user.assert_not_awaited()

    async def test_old_paid_order_not_clipped_to_new_settings_cap(self):
        await self.invoice(gb=2000)
        await self.service.process_successful_payment('traffic-1', 49)
        self.assertEqual((await self.user()).extra_traffic_gb, 2000)
        self.assertEqual(self.panel.update_user.await_args.kwargs['traffic_limit_bytes'], 2500 * 1024**3)

    async def test_invalid_traffic_order_and_reused_identifier_are_rejected(self):
        for gb in [-100, 0]:
            with self.assertRaises(ValueError):
                await self.invoice(gb=gb)
        await self.invoice()
        with self.assertRaises(ValueError):
            await self.invoice(gb=200)

    async def test_autorenew_cancellation_failure_is_retried_without_duplicate_traffic(self):
        from types import SimpleNamespace
        live=SimpleNamespace(extra_devices=2,extra_traffic_gb=0)
        recurring=SimpleNamespace(cancel_if_composition_changed=AsyncMock(side_effect=RuntimeError('gateway down')))
        self.service._recurring_service=recurring
        await self.invoice()
        with self.assertRaises(RuntimeError):
            await self.service.process_successful_payment('traffic-1',49)
        self.assertTrue((await self.payment('traffic-1')).sync_pending)
        self.assertEqual((await self.user()).extra_traffic_gb,100)
        recurring.cancel_if_composition_changed.side_effect=None
        await self.service.process_successful_payment('traffic-1',49)
        self.assertFalse((await self.payment('traffic-1')).sync_pending)
        self.assertEqual((await self.user()).extra_traffic_gb,100)
        self.assertEqual(recurring.cancel_if_composition_changed.await_count,2)

    async def test_limited_user_is_enabled_without_resetting_usage(self):
        self.panel.get_user_by_username.side_effect=None
        self.panel.get_user_by_username.return_value={'uuid':'uuid-user_42','status':'LIMITED'}
        await self.invoice()
        await self.service.process_successful_payment('traffic-1',49)
        self.panel.enable_user.assert_awaited_once_with('uuid-user_42')
        self.panel.reset_user_traffic.assert_not_awaited()

    async def test_concurrent_confirmations_are_atomic_on_postgresql(self):
        import asyncio
        if not self.schema:
            self.skipTest('Row locking requires PostgreSQL')
        await self.invoice('traffic-1',100)
        await self.repo.cancel_if_pending('traffic-1')
        await self.invoice('traffic-2',200)
        await asyncio.gather(
            self.service.process_successful_payment('traffic-1',49),
            self.service.process_successful_payment('traffic-2',49),
            self.service.process_successful_payment('traffic-1',49))
        self.assertEqual((await self.user()).extra_traffic_gb,300)
        self.assertEqual(self.panel.update_user.await_args.kwargs['traffic_limit_bytes'],800*1024**3)
        self.panel.reset_user_traffic.assert_not_awaited()

    async def test_refund_of_old_addon_does_not_remove_new_renewal_packs(self):
        await self.invoice()
        await self.service.process_successful_payment('traffic-1',49)
        await self.create_invoice('renew',extra_traffic_gb=100)
        await self.service.process_successful_payment('renew',399)
        await self.service.process_refund('traffic-1')
        self.assertEqual((await self.user()).extra_traffic_gb,100)
