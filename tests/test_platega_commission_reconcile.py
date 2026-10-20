"""Сверка цены с комиссией: настоящая БД, идемпотентная выдача и возврат."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from payment_test_support import PaymentDatabaseTests


PAYMENT_ID = '00000000-0000-0000-0000-000000000001'
ORDER_ID = '00000000-0000-0000-0000-000000000002'


class PayerCommissionReconcileTests(PaymentDatabaseTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.create_invoice('order:' + ORDER_ID, extra_devices=2)
        await self.repo.bind_transaction('order:' + ORDER_ID, PAYMENT_ID)
        self.verified = {
            'id': PAYMENT_ID,
            'status': 'CONFIRMED',
            'paymentDetails': {'amount': 430.92, 'currency': 'RUB'},
            'comission': 31.92,
            'comissionType': 0,
            'payload': json.dumps({'orderId': ORDER_ID, 'userId': '42'}),
        }
        self.client = SimpleNamespace(configured=True,
            get_payment=AsyncMock(return_value=self.verified))

    async def _reconcile(self):
        with patch.dict('sys.modules', {'loader': SimpleNamespace(platega_client=self.client)}):
            await self.service.reconcile()

    async def test_reconcile_uses_net_order_price_and_duplicate_callback_adds_nothing(self):
        payer_before = await self.user()
        referrer_before = await self.user(77)

        await self._reconcile()

        payment = await self.payment(PAYMENT_ID)
        self.assertEqual(payment.order_id, ORDER_ID)
        self.assertEqual(payment.final_amount, 399)
        self.assertEqual(payment.status, 'succeeded')
        self.assertFalse(payment.sync_pending)
        self.assertIsNotNone(payment.last_checked_at)
        payer_after = await self.user()
        referrer_after = await self.user(77)
        self.assertEqual(payer_after.subscription_end_date,
                         payer_before.subscription_end_date + timedelta(days=90))
        self.assertEqual(referrer_after.subscription_end_date,
                         referrer_before.subscription_end_date + timedelta(days=7))
        self.assertEqual(referrer_after.referral_bonus_days, 7)
        self.assertEqual(payer_after.extra_devices, 2)
        self.assertEqual(payer_after.subscription_data_limit_gb, 100)
        self.panel.reset_user_traffic.assert_awaited_once()

        # Поздняя доставка callback после планировщика зачисляет ту же цену заказа.
        self.assertIsNone(await self.service.process_successful_payment(PAYMENT_ID, 399, 'RUB'))
        await self._reconcile()

        self.assertEqual((await self.user()).subscription_end_date,
                         payer_after.subscription_end_date)
        self.assertEqual((await self.user(77)).subscription_end_date,
                         referrer_after.subscription_end_date)
        self.assertEqual((await self.user(77)).referral_bonus_days, 7)
        self.assertEqual((await self.user()).extra_devices, 2)
        self.panel.reset_user_traffic.assert_awaited_once()

    async def test_commission_mismatch_never_grants_but_marks_the_attempt_checked(self):
        payer_before = await self.user()
        referrer_before = await self.user(77)
        for name, update in [
            ('different fee', {'comission': 31.91}),
            ('merchant fee', {'comissionType': 1}),
            ('boolean fee mode', {'comissionType': False}),
            ('another user', {'payload': json.dumps({'orderId': ORDER_ID, 'userId': '999'})}),
            ('another order', {'payload': json.dumps({'orderId': PAYMENT_ID, 'userId': '42'})}),
            ('missing payload', {'payload': None}),
            ('another currency', {'paymentDetails': {'amount': 430.92, 'currency': 'USD'}}),
        ]:
            with self.subTest(case=name):
                self.client.get_payment.return_value = {**deepcopy(self.verified), **update}
                await self._reconcile()

                payment = await self.payment(PAYMENT_ID)
                self.assertEqual(payment.status, 'pending')
                self.assertEqual(payment.final_amount, 399)
                self.assertIsNone(payment.completed_at)
                self.assertFalse(payment.sync_pending)
                self.assertFalse(payment.traffic_reset_done)
                self.assertIsNotNone(payment.last_checked_at)
                payer = await self.user()
                referrer = await self.user(77)
                self.assertEqual(payer.subscription_end_date, payer_before.subscription_end_date)
                self.assertEqual(referrer.subscription_end_date, referrer_before.subscription_end_date)
                self.assertEqual(payer.extra_devices, 0)
                self.assertFalse(payer.is_first_payment_made)
                self.assertEqual(referrer.referral_bonus_days, 0)
                for operation in vars(self.panel).values():
                    operation.assert_not_awaited()

    async def test_paid_fee_order_can_be_refunded_once_without_using_gross_as_price(self):
        payer_before = await self.user()
        await self._reconcile()
        self.client.get_payment.return_value['status'] = 'CHARGEBACKED'

        await self._reconcile()
        await self._reconcile()

        payment = await self.payment(PAYMENT_ID)
        self.assertEqual(payment.status, 'refunded')
        self.assertEqual(payment.final_amount, 399)
        self.assertFalse(payment.sync_pending)
        self.assertEqual((await self.user()).subscription_end_date,
                         payer_before.subscription_end_date)
        self.panel.reset_user_traffic.assert_awaited_once()

    async def test_canceled_invoice_with_gateway_minimum_amount_is_not_an_error(self):
        self.client.get_payment.return_value = {
            **deepcopy(self.verified), 'status': 'CANCELED',
            'paymentDetails': {'amount': 2.0, 'currency': 'RUB'}, 'comission': 0.08}

        with self.assertNoLogs('loader', level='ERROR'):
            await self._reconcile()
            await self._reconcile()

        payment = await self.payment(PAYMENT_ID)
        self.assertEqual(payment.status, 'cancelled')
        self.assertIsNone(payment.completed_at)
        self.assertIsNotNone(payment.last_checked_at)
        for operation in vars(self.panel).values():
            operation.assert_not_awaited()

    async def test_gross_rounded_up_to_whole_ruble_is_accepted_but_other_overpay_is_not(self):
        rounded = {**deepcopy(self.verified),
                   'paymentDetails': {'amount': 140.0, 'currency': 'RUB'}, 'comission': 10.32}
        # Цена заказа 399, поэтому проверяем правило на 399 + комиссия.
        rounded['paymentDetails']['amount'] = 410.0
        rounded['comission'] = 10.32
        payer_before = await self.user()
        for name, update in [
            ('rounded to 411', {'paymentDetails': {'amount': 411.0, 'currency': 'RUB'}}),
            ('fee changed', {'comission': 11.32}),
        ]:
            with self.subTest(case=name):
                self.client.get_payment.return_value = {**deepcopy(rounded), **update}
                await self._reconcile()
                self.assertEqual((await self.payment(PAYMENT_ID)).status, 'pending')
                self.assertEqual((await self.user()).subscription_end_date,
                                 payer_before.subscription_end_date)

        self.client.get_payment.return_value = rounded
        await self._reconcile()
        payment = await self.payment(PAYMENT_ID)
        self.assertEqual(payment.status, 'succeeded')
        self.assertEqual(payment.final_amount, 399)
