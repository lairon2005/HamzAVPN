"""Комиссия поверх цены допускается только по независимым данным API заказа."""
from copy import deepcopy
import json

import test_platega_callback as callback_tests


class PayerCommissionCallbackTests(callback_tests.CallbackTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.payment.order_id = callback_tests.ORDER_ID
        self.client.get_payment.return_value.update({
            'paymentDetails': {'amount': 430.92, 'currency': 'RUB'},
            'comission': 31.92,
            'comissionType': 0,
            'payload': json.dumps({'orderId': callback_tests.ORDER_ID, 'userId': '42'}),
        })

    async def test_exact_verified_payer_commission_credits_only_the_order_price(self):
        result = await self.module.platega_webhook_handler(self.request())
        self.assertEqual(result.status, 200)
        self.service.process_successful_payment.assert_awaited_once_with(
            callback_tests.ID, 399, 'RUB')

    async def test_verified_gross_callback_is_accepted_but_credits_only_the_order_price(self):
        result = await self.module.platega_webhook_handler(self.request({
            'id': callback_tests.ID, 'amount': 430.92,
            'currency': 'RUB', 'status': 'CONFIRMED',
        }))
        self.assertEqual(result.status, 200)
        self.service.process_successful_payment.assert_awaited_once_with(
            callback_tests.ID, 399, 'RUB')
        self.service.process_refund.assert_not_awaited()

    async def test_callback_amount_must_be_one_of_the_two_independently_verified_amounts(self):
        for amount in (400, 430.91, 430.93, 500):
            with self.subTest(callback_amount=amount):
                result = await self.module.platega_webhook_handler(self.request({
                    'id': callback_tests.ID, 'amount': amount,
                    'currency': 'RUB', 'status': 'CONFIRMED',
                }))
                self.assertEqual(result.status, 409)
                self.service.process_successful_payment.assert_not_awaited()
                self.service.process_refund.assert_not_awaited()

    async def test_gross_callback_never_bypasses_commission_or_payload_validation(self):
        valid = deepcopy(self.client.get_payment.return_value)
        for name, update in [
            ('wrong fee', {'comission': 31.91}),
            ('wrong payer', {'payload': json.dumps({
                'orderId': callback_tests.ORDER_ID, 'userId': '999'})}),
            ('wrong order', {'payload': json.dumps({
                'orderId': callback_tests.ID, 'userId': '42'})}),
        ]:
            with self.subTest(case=name):
                self.client.get_payment.return_value = {**deepcopy(valid), **update}
                result = await self.module.platega_webhook_handler(self.request({
                    'id': callback_tests.ID, 'amount': 430.92,
                    'currency': 'RUB', 'status': 'CONFIRMED',
                }))
                self.assertEqual(result.status, 409)
                self.service.process_successful_payment.assert_not_awaited()

    async def test_larger_callback_amount_is_rejected_when_api_has_no_payer_fee(self):
        self.client.get_payment.return_value = {
            'id': callback_tests.ID, 'status': 'CONFIRMED',
            'paymentDetails': {'amount': 399, 'currency': 'RUB'},
        }
        result = await self.module.platega_webhook_handler(self.request({
            'id': callback_tests.ID, 'amount': 430.92,
            'currency': 'RUB', 'status': 'CONFIRMED',
        }))
        self.assertEqual(result.status, 409)
        self.service.process_successful_payment.assert_not_awaited()
        self.service.process_refund.assert_not_awaited()

    async def test_gross_confirmed_callback_cannot_override_unconfirmed_api_status(self):
        for status in ('PENDING', 'CANCELED'):
            with self.subTest(api_status=status):
                self.client.get_payment.return_value['status'] = status
                result = await self.module.platega_webhook_handler(self.request({
                    'id': callback_tests.ID, 'amount': 430.92,
                    'currency': 'RUB', 'status': 'CONFIRMED',
                }))
                self.assertEqual(result.status, 409)
                self.service.process_successful_payment.assert_not_awaited()
                self.service.process_refund.assert_not_awaited()

    async def test_commission_cannot_explain_an_incorrect_net_amount(self):
        valid = deepcopy(self.client.get_payment.return_value)
        for name, update in [
            ('wrong commission', {'comission': 31.91}),
            ('zero commission', {'comission': 0}),
            ('negative commission', {'comission': -31.92}),
            ('fractional kopeks', {'comission': 31.921}),
            ('gross below price', {'paymentDetails': {'amount': 398, 'currency': 'RUB'}}),
        ]:
            with self.subTest(case=name):
                self.client.get_payment.return_value = {**deepcopy(valid), **update}
                result = await self.module.platega_webhook_handler(self.request())
                self.assertEqual(result.status, 409)
                self.service.process_successful_payment.assert_not_awaited()
                self.service.process_refund.assert_not_awaited()

    async def test_commission_type_requires_the_exact_integer_payer_mode(self):
        for kind in [None, 1, 2, '0', 0.0, False, True]:
            with self.subTest(comissionType=kind):
                self.client.get_payment.return_value['comissionType'] = kind
                result = await self.module.platega_webhook_handler(self.request())
                self.assertEqual(result.status, 409)
                self.service.process_successful_payment.assert_not_awaited()

    async def test_commission_requires_the_api_payload_to_match_both_order_and_user(self):
        for payload in [
            {'orderId': callback_tests.ORDER_ID, 'userId': '999'},
            {'orderId': callback_tests.ID, 'userId': '42'},
            {'orderId': callback_tests.ORDER_ID},
            {'userId': '42'},
            {},
            [],
        ]:
            with self.subTest(payload=payload):
                self.client.get_payment.return_value['payload'] = json.dumps(payload)
                result = await self.module.platega_webhook_handler(self.request())
                self.assertEqual(result.status, 409)
                self.service.process_successful_payment.assert_not_awaited()
        for payload in [None, '', '{broken']:
            with self.subTest(payload=payload):
                self.client.get_payment.return_value['payload'] = payload
                result = await self.module.platega_webhook_handler(self.request())
                self.assertEqual(result.status, 409)
                self.service.process_successful_payment.assert_not_awaited()

    async def test_fee_on_another_currency_is_rejected(self):
        self.client.get_payment.return_value['paymentDetails']['currency'] = 'USD'
        result = await self.module.platega_webhook_handler(self.request())
        self.assertEqual(result.status, 409)
        self.service.process_successful_payment.assert_not_awaited()

    async def test_fee_recovery_verifies_price_before_binding_reserved_order(self):
        self.repo.get_by_provider_id.side_effect = [None, self.payment]
        result = await self.module.platega_webhook_handler(self.request())
        self.assertEqual(result.status, 200)
        self.repo.bind_transaction.assert_awaited_once_with(
            'order:' + callback_tests.ORDER_ID, callback_tests.ID)
        self.service.process_successful_payment.assert_awaited_once_with(
            callback_tests.ID, 399, 'RUB')

    async def test_fee_recovery_does_not_bind_order_with_unmatched_commission(self):
        self.client.get_payment.return_value['comission'] = 31.91
        self.repo.get_by_provider_id.side_effect = [None, self.payment]
        result = await self.module.platega_webhook_handler(self.request())
        self.assertEqual(result.status, 409)
        self.repo.bind_transaction.assert_not_awaited()
        self.service.process_successful_payment.assert_not_awaited()
