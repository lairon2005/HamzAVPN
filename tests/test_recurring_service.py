"""Автопродление на настоящих моделях и репозиториях (SQLite), Platega подменена."""
import sys
import types
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from sqlalchemy import update

from payment_test_support import PaymentDatabaseTests
from test_stars_payment import _load_module, _real_device_pricing
from platega.client import PlategaError

SUB = '00000000-0000-0000-0000-0000000000a1'
SUB2 = '00000000-0000-0000-0000-0000000000a2'
CHARGE1 = '00000000-0000-0000-0000-0000000000c1'
CHARGE2 = '00000000-0000-0000-0000-0000000000c2'


def real_pricing():
    return _load_module('pricing_under_test', 'tgbot/services/pricing.py', {})


class RecurringTests(PaymentDatabaseTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.sessions() as session:
            session.add_all([
                self.models.Tariff(id=6, name='Дробная цена', duration_days=30, price=199.5, data_limit_gb=100),
                self.models.Tariff(id=7, name='Полтора месяца', duration_days=45, price=300, data_limit_gb=100),
                self.models.Tariff(id=8, name='Месяц', duration_days=30, price=149, data_limit_gb=100),
            ])
            await session.commit()
        self.logger = Mock()
        self.platega = SimpleNamespace(
            configured=True,
            create_subscription=AsyncMock(return_value=SimpleNamespace(
                id=SUB, url='https://pay.platega.io/subscription/' + SUB, status='PENDING')),
            cancel_subscription=AsyncMock(),
            get_subscription=AsyncMock(),
        )
        loader = types.ModuleType('loader')
        loader.logger = self.logger
        loader.platega_client = self.platega
        tgbot = types.ModuleType('tgbot'); tgbot.__path__ = []
        services = types.ModuleType('tgbot.services'); services.__path__ = []
        stubs = {
            'loader': loader, 'tgbot': tgbot, 'tgbot.services': services,
            'tgbot.services.device_pricing': _real_device_pricing(),
            'tgbot.services.pricing': real_pricing(),
            'tgbot.services.payment_service': self.service_module,
        }
        self.recurring_module = _load_module(
            'recurring_service_under_test', 'tgbot/services/recurring_service.py', stubs)
        repo_module = _load_module('real_recurring_repo_under_test',
            'database/repositories/recurring.py', {'db': self.models})
        self.recurring_repo = repo_module.RecurringRepository(self.sessions)
        self.recurring = self.recurring_module.RecurringService(
            self.recurring_repo, self.repo, self.service, self.tariffs, self.user_repo, self.slot_service,
            platega=self.platega)

    async def sub(self, identifier=SUB):
        return await self.recurring_repo.get_by_platega_id(identifier)

    # ------------------------------------------------------------ оформление

    async def test_disabled_flag_hides_and_blocks_autorenew(self):
        service = self.recurring_module.RecurringService(
            self.recurring_repo, self.repo, self.service, self.tariffs, self.user_repo,
            self.slot_service, platega=self.platega, enabled=False)
        self.assertFalse(service.is_available())
        with self.assertRaises(self.recurring_module.RecurringError):
            await service.start(42, 5, 0)
        self.platega.create_subscription.assert_not_awaited()

    async def test_start_sends_whole_amount_and_period_to_platega(self):
        sub, url = await self.recurring.start(42, 5, 0)
        self.assertEqual((sub.platega_subscription_id, sub.status, sub.amount), (SUB, 'pending', 399))
        self.assertTrue(url.startswith('https://pay.platega.io/'))
        args = self.platega.create_subscription.await_args.args
        self.assertEqual(args[0], 399)
        self.assertEqual(args[2:], (3, 3))  # 90 дней = 3 месяца по 30 дней

    async def test_extra_devices_are_included_in_fixed_amount(self):
        sub, _ = await self.recurring.start(42, 5, 2)
        # 399 ₽ тариф + 2 устройства × 49 ₽ × 3 месяца
        self.assertEqual((sub.amount, sub.extra_devices), (399 + 2 * 49 * 3, 2))

    async def test_fractional_price_and_unsupported_period_are_refused_before_platega(self):
        for tariff_id in (6, 7):
            with self.assertRaises(self.recurring_module.RecurringError):
                await self.recurring.start(42, tariff_id, 0)
        self.platega.create_subscription.assert_not_awaited()

    async def test_active_subscription_cannot_be_duplicated_but_pending_is_replaced(self):
        await self.recurring.start(42, 5, 0)
        self.platega.create_subscription.return_value = SimpleNamespace(
            id=SUB2, url='https://pay.platega.io/subscription/' + SUB2, status='PENDING')
        await self.recurring.start(42, 5, 0)  # незавершённая привязка заменяется
        self.platega.cancel_subscription.assert_awaited_once_with(SUB)
        self.assertEqual((await self.sub(SUB)).status, 'cancelled')
        self.assertEqual((await self.sub(SUB2)).status, 'pending')
        await self.recurring_repo.record_charge(SUB2, None)
        with self.assertRaises(self.recurring_module.RecurringError):
            await self.recurring.start(42, 5, 0)

    async def test_pending_manual_invoice_blocks_subscription(self):
        await self.create_invoice('p-1')
        with self.assertRaises(self.service_module.PendingPaymentError):
            await self.recurring.start(42, 5, 0)
        self.platega.create_subscription.assert_not_awaited()

    # ------------------------------------------------------------ списания

    async def test_confirmed_charge_extends_subscription_exactly_once(self):
        await self.recurring.start(42, 5, 1)
        before = (await self.user()).subscription_end_date
        sub = await self.sub()
        nxt = datetime.now() + timedelta(days=90)
        result = await self.recurring.on_charge_confirmed(sub, CHARGE1, sub.amount, 'RUB', nxt)
        self.assertIsNotNone(result)
        after = (await self.user()).subscription_end_date
        self.assertEqual(after - before, timedelta(days=90))
        payment = await self.payment(CHARGE1)
        self.assertEqual((payment.source, payment.status, payment.kind, payment.extra_devices),
                         ('auto', 'succeeded', 'subscription', 1))
        self.assertEqual((await self.user()).extra_devices, 1)
        # Повторная доставка того же callback ничего не добавляет.
        self.assertIsNone(await self.recurring.on_charge_confirmed(sub, CHARGE1, sub.amount, 'RUB', nxt))
        self.assertEqual((await self.user()).subscription_end_date, after)
        live = await self.sub()
        self.assertEqual((live.status, live.charges_ok), ('active', 1))
        self.assertEqual(live.next_charge_at.date(), nxt.date())

    async def test_next_charge_credits_another_period(self):
        await self.recurring.start(42, 5, 0)
        sub = await self.sub()
        await self.recurring.on_charge_confirmed(sub, CHARGE1, sub.amount, 'RUB', None)
        mid = (await self.user()).subscription_end_date
        await self.recurring.on_charge_confirmed(sub, CHARGE2, sub.amount, 'RUB', None)
        self.assertEqual((await self.user()).subscription_end_date - mid, timedelta(days=90))
        self.assertEqual((await self.sub()).charges_ok, 2)

    async def test_first_charge_is_a_regular_first_payment_with_referral_bonus(self):
        await self.recurring.start(42, 5, 0)
        sub = await self.sub()
        referrer_before = (await self.user(77)).subscription_end_date
        result = await self.recurring.on_charge_confirmed(sub, CHARGE1, sub.amount, 'RUB', None)
        self.assertTrue(result.is_first_payment)
        self.assertEqual(result.referrer_id, 77)
        self.assertGreater((await self.user(77)).subscription_end_date, referrer_before)
        self.assertTrue((await self.user()).is_first_payment_made)

    async def test_charge_with_foreign_amount_is_rejected_and_not_recorded(self):
        await self.recurring.start(42, 5, 0)
        sub = await self.sub()
        with self.assertRaises(ValueError):
            await self.recurring.on_charge_confirmed(sub, CHARGE1, sub.amount - 1, 'RUB', None)
        self.assertIsNone(await self.payment(CHARGE1))

    async def test_auto_charge_does_not_collide_with_manual_pending_invoice(self):
        await self.recurring.start(42, 5, 0)
        sub = await self.sub()
        # Человек успел выставить обычный счёт — списание подписки всё равно проходит.
        await self.create_invoice('p-1')
        result = await self.recurring.on_charge_confirmed(sub, CHARGE1, sub.amount, 'RUB', None)
        self.assertIsNotNone(result)
        self.assertEqual((await self.payment('p-1')).status, 'pending')
        self.assertEqual((await self.payment(CHARGE1)).status, 'succeeded')

    async def test_auto_payments_are_invisible_to_pending_and_stale_logic(self):
        await self.recurring.start(42, 5, 0)
        sub = await self.sub()
        await self.recurring.on_charge_confirmed(sub, CHARGE1, sub.amount, 'RUB', None)
        self.assertIsNone(await self.repo.get_user_pending(42))
        self.assertEqual(await self.repo.get_pending_older_than(0), [])
        self.assertEqual([p.provider_payment_id for p in await self.repo.get_unsettled()], [])

    async def test_failed_charge_marks_past_due_and_reports_change_once(self):
        await self.recurring.start(42, 5, 0)
        sub = await self.sub()
        await self.recurring.on_charge_confirmed(sub, CHARGE1, sub.amount, 'RUB', None)
        self.assertTrue(await self.recurring.on_charge_failed(sub))
        self.assertFalse(await self.recurring.on_charge_failed(sub))
        self.assertEqual((await self.sub()).status, 'past_due')
        self.assertFalse(await self.recurring.has_active(42))
        # Пользователь может оформить автопродление заново.
        self.platega.create_subscription.return_value = SimpleNamespace(
            id=SUB2, url='https://pay.platega.io/subscription/' + SUB2, status='PENDING')
        new, _ = await self.recurring.start(42, 5, 0)
        self.assertEqual(new.platega_subscription_id, SUB2)

    # ------------------------------------------------------------ отключение

    async def test_cancel_stops_platega_first_and_cancellation_is_final(self):
        await self.recurring.start(42, 5, 0)
        sub = await self.sub()
        await self.recurring.on_charge_confirmed(sub, CHARGE1, sub.amount, 'RUB', None)
        self.assertIsNotNone(await self.recurring.cancel(42))
        self.platega.cancel_subscription.assert_awaited_with(SUB)
        self.assertEqual((await self.sub()).status, 'cancelled')
        # Запоздавший callback «подписка активна» не воскрешает отменённую.
        self.assertFalse(await self.recurring_repo.set_status(SUB, 'active'))
        self.assertEqual((await self.sub()).status, 'cancelled')
        self.assertIsNone(await self.recurring.cancel(42))

    async def test_platega_failure_keeps_subscription_alive(self):
        await self.recurring.start(42, 5, 0)
        sub = await self.sub()
        await self.recurring.on_charge_confirmed(sub, CHARGE1, sub.amount, 'RUB', None)
        self.platega.cancel_subscription.side_effect = PlategaError('down')
        with self.assertRaises(PlategaError):
            await self.recurring.cancel(42)
        self.assertEqual((await self.sub()).status, 'active')

    async def test_buying_slots_disables_active_autorenew_only(self):
        await self.recurring.start(42, 5, 0)
        self.assertIsNone(await self.recurring.on_devices_changed(42))  # ещё pending
        self.assertEqual((await self.sub()).status, 'pending')
        sub = await self.sub()
        await self.recurring.on_charge_confirmed(sub, CHARGE1, sub.amount, 'RUB', None)
        self.assertIsNotNone(await self.recurring.on_devices_changed(42))
        self.assertEqual((await self.sub()).status, 'cancelled')

    # ------------------------------------------------------------ сверка

    async def backdate(self, minutes):
        async with self.sessions() as session:
            await session.execute(update(self.models.RecurringSubscription).values(
                created_at=datetime.now() - timedelta(minutes=minutes)))
            await session.commit()

    async def test_reconcile_closes_abandoned_binding(self):
        await self.recurring.start(42, 5, 0)
        self.platega.get_subscription.return_value = {
            'id': SUB, 'status': 'Failed', 'amount': 399, 'chargeMetrics': {'chargesSuccess': 0}}
        self.assertEqual(await self.recurring.reconcile(), [])  # ещё не истекли 30 минут
        await self.backdate(60)
        changes = await self.recurring.reconcile()
        self.assertEqual([(old, new) for _, old, new in changes], [('pending', 'failed')])
        self.assertEqual((await self.sub()).status, 'failed')

    async def test_reconcile_picks_up_external_cancellation_and_ignores_unreachable_api(self):
        await self.recurring.start(42, 5, 0)
        sub = await self.sub()
        await self.recurring.on_charge_confirmed(sub, CHARGE1, sub.amount, 'RUB', None)
        self.platega.get_subscription.side_effect = PlategaError('down')
        self.assertEqual(await self.recurring.reconcile(), [])
        self.assertEqual((await self.sub()).status, 'active')
        self.platega.get_subscription.side_effect = None
        self.platega.get_subscription.return_value = {
            'id': SUB, 'status': 'Cancelled', 'amount': 399, 'chargeMetrics': {'chargesSuccess': 1}}
        async with self.sessions() as session:  # забыть последнюю проверку
            await session.execute(update(self.models.RecurringSubscription).values(last_checked_at=None))
            await session.commit()
        changes = await self.recurring.reconcile()
        self.assertEqual([(old, new) for _, old, new in changes], [('active', 'cancelled')])

    async def test_reconcile_raises_alarm_when_platega_charged_but_callback_was_lost(self):
        await self.recurring.start(42, 5, 0)
        await self.recurring_repo.record_charge(SUB, None)  # подписка активна, но платежей у нас нет
        self.platega.get_subscription.return_value = {
            'id': SUB, 'status': 'Active', 'amount': 399, 'chargeMetrics': {'chargesSuccess': 1}}
        await self.recurring.reconcile()
        self.logger.error.assert_called()
        self.assertIn('Platega списала', self.logger.error.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
