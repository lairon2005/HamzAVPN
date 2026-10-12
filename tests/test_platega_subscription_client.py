"""Рекуррентные подписки Platega: формат запросов, интервалы, разбор callback."""
import json
import unittest

import httpx

from platega.client import (
    PlategaClient, PlategaError, callback_kind, normalize_callback, parse_charge,
    parse_platega_time, subscription_interval, whole_rubles,
)

SUB = '00000000-0000-0000-0000-0000000000a1'


class IntervalTests(unittest.TestCase):
    def test_tariff_periods_map_to_platega_intervals(self):
        # 1 — день, 2 — неделя, 3 — месяц (30 дней), 4 — год
        expected = {7: (2, 1), 30: (3, 1), 90: (3, 3), 180: (3, 6), 365: (4, 1), 3: (1, 3)}
        for days, interval in expected.items():
            self.assertEqual(subscription_interval(days), interval, days)

    def test_period_that_platega_cannot_express_is_refused(self):
        for days in (0, -30, 45, 1460, None, 'abc'):
            self.assertIsNone(subscription_interval(days), days)

    def test_amount_must_be_whole_rubles(self):
        self.assertEqual(whole_rubles(399), 399)
        self.assertEqual(whole_rubles('399.00'), 399)
        for bad in (199.5, 0, -5, 'x'):
            with self.assertRaises(ValueError):
                whole_rubles(bad)


class CallbackParsingTests(unittest.TestCase):
    def test_keys_are_case_insensitive(self):
        self.assertEqual(normalize_callback({'Id': 1, 'SubscriptionId': 2, 'status': 3}),
                         {'id': 1, 'subscriptionid': 2, 'status': 3})

    def test_callback_kinds(self):
        self.assertEqual(callback_kind(normalize_callback(
            {'id': SUB, 'amount': 399, 'currency': 'RUB', 'status': 'CONFIRMED'})), 'payment')
        self.assertEqual(callback_kind(normalize_callback(
            {'Id': 'c', 'Status': 'CONFIRMED', 'SubscriptionId': SUB})), 'subscription_charge')
        self.assertEqual(callback_kind(normalize_callback(
            {'Id': SUB, 'Status': 'SUBSCRIPTION_ACTIVATED'})), 'subscription_status')
        # Статус подписки определяется по значению, даже если SubscriptionId тоже есть.
        self.assertEqual(callback_kind(normalize_callback(
            {'Id': SUB, 'Status': 'SUBSCRIPTION_CANCELLED', 'SubscriptionId': SUB})), 'subscription_status')

    def test_charge_fields_and_time(self):
        charge = parse_charge({'Id': 'c', 'Amount': 100, 'Currency': 'RUB', 'Status': 'CONFIRMED',
                               'SubscriptionId': SUB, 'NextChargeAt': '2026-08-09T09:10:00Z'})
        self.assertEqual((charge['id'], charge['subscription_id'], charge['status']), ('c', SUB, 'CONFIRMED'))
        self.assertIsNotNone(charge['next_charge_at'])
        self.assertIsNone(parse_charge({'Id': 'c', 'NextChargeAt': None})['next_charge_at'])
        self.assertIsNone(parse_platega_time('не дата'))


class SubscriptionApiTests(unittest.IsolatedAsyncioTestCase):
    def make(self, respond):
        return PlategaClient('merchant', 'secret', transport=httpx.MockTransport(respond))

    async def test_create_subscription_matches_documented_request(self):
        captured = []

        def respond(request):
            captured.append(request)
            return httpx.Response(200, json={
                'paymentMethod': 'Subscription', 'transactionId': SUB, 'status': 'PENDING',
                'redirect': 'https://pay.platega.io/subscription/' + SUB, 'merchantId': 'm'})
        client = self.make(respond)
        try:
            created = await client.create_subscription(399, 'Автопродление', 3, 3)
        finally:
            await client.aclose()
        self.assertEqual((created.id, created.url), (SUB, 'https://pay.platega.io/subscription/' + SUB))
        request = captured[0]
        self.assertEqual((request.method, request.url.path), ('POST', '/transaction/process'))
        self.assertEqual(request.headers['X-MerchantId'], 'merchant')
        body = json.loads(request.content)
        self.assertEqual(body['paymentMethod'], 6)
        self.assertEqual(body['paymentDetails'],
                         {'amount': 399, 'currency': 'RUB', 'interval': 3, 'intervalCount': 3})
        self.assertIsInstance(body['paymentDetails']['amount'], int)

    async def test_create_subscription_validates_before_request_and_response_after(self):
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={'transactionId': SUB, 'status': 'PENDING',
                                             'redirect': 'http://insecure.example/pay'})
        client = self.make(respond)
        try:
            with self.assertRaises(ValueError):
                await client.create_subscription(199.5, 'x', 3, 1)
            with self.assertRaises(ValueError):
                await client.create_subscription(399, 'x', 3, 13)  # месяцев не больше 12
            self.assertEqual(calls, [])
            with self.assertRaises(PlategaError):  # не HTTPS-ссылка
                await client.create_subscription(399, 'x', 3, 1)
        finally:
            await client.aclose()

    async def test_gateway_rejection_surfaces_as_platega_error_without_body(self):
        client = self.make(lambda request: httpx.Response(400, json={
            'code': 'Common:VAL_0001', 'data': [{'key': 'paymentMethod', 'message': 'Subscription'}]}))
        try:
            with self.assertRaises(PlategaError) as ctx:
                await client.create_subscription(399, 'x', 3, 1)
        finally:
            await client.aclose()
        self.assertNotIn('VAL_0001', str(ctx.exception))

    async def test_get_and_cancel_use_documented_endpoints(self):
        seen = []

        def respond(request):
            seen.append((request.method, request.url.path))
            if request.method == 'GET':
                return httpx.Response(200, json={'id': SUB, 'status': 'Active', 'amount': 399})
            return httpx.Response(200, json={'subscriptionId': SUB, 'status': 'cancelled'})
        client = self.make(respond)
        try:
            remote = await client.get_subscription(SUB)
            await client.cancel_subscription(SUB)
        finally:
            await client.aclose()
        self.assertEqual(remote['status'], 'Active')
        self.assertEqual(seen, [('GET', f'/subscription/{SUB}'), ('POST', f'/subscription/{SUB}/cancel')])

    async def test_foreign_or_malformed_subscription_is_rejected(self):
        for payload in ({'id': '00000000-0000-0000-0000-0000000000ff', 'status': 'Active', 'amount': 1},
                        {'id': SUB, 'status': 'Weird', 'amount': 1},
                        {'id': SUB, 'status': 'Active', 'amount': 1.5}):
            client = self.make(lambda request, p=payload: httpx.Response(200, json=p))
            try:
                with self.assertRaises(PlategaError):
                    await client.get_subscription(SUB)
            finally:
                await client.aclose()


if __name__ == '__main__':
    unittest.main()
