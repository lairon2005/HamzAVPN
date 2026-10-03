"""Проверка официального формата запросов без обращения к платежной системе."""
import json
import unittest
from decimal import Decimal
import httpx
from platega.client import PlategaClient, PlategaError, money

ID = '00000000-0000-0000-0000-000000000001'


class PlategaClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_uses_v2_headers_rub_and_customer_metadata(self):
        captured = []
        def respond(request):
            captured.append(request)
            return httpx.Response(200, json={'transactionId': ID, 'status': 'PENDING',
                'url': 'https://pay.platega.io/?id=' + ID, 'expiresIn': '00:15:00'})
        client = PlategaClient('merchant', 'secret', transport=httpx.MockTransport(respond))
        try:
            result = await client.create_payment(339.15, 'Подписка HamzAVPN',
                'https://example.test/profile/', 42, metadata={'orderId': ID, 'tariffId': '5'})
        finally:
            await client.aclose()
        self.assertEqual(result.id, ID)
        req = captured[0]
        self.assertEqual(str(req.url), 'https://app.platega.io/v2/transaction/process')
        self.assertEqual(req.headers['X-MerchantId'], 'merchant')
        self.assertEqual(req.headers['X-Secret'], 'secret')
        body = json.loads(req.content)
        self.assertEqual(body['paymentDetails'], {'amount': 339.15, 'currency': 'RUB'})
        self.assertEqual(body['metadata'], {'userId': '42'})
        self.assertEqual(json.loads(body['payload'])['orderId'], ID)
        self.assertEqual(body['return'], body['failedUrl'])
        self.assertNotIn('paymentMethod', body)
        self.assertNotIn('receipt', body)

    async def test_status_uses_documented_endpoint_and_rejects_wrong_id(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'id': '00000000-0000-0000-0000-000000000002',
                'status': 'CONFIRMED', 'paymentDetails': {'amount': 399, 'currency': 'RUB'}})
        client = PlategaClient('merchant', 'secret', transport=httpx.MockTransport(respond))
        try:
            with self.assertRaises(PlategaError):
                await client.get_payment(ID)
        finally:
            await client.aclose()
        self.assertEqual(requests[0].url.path, '/transaction/' + ID)

    async def test_empty_credentials_never_accept_callback_or_make_api_request(self):
        client = PlategaClient('', '')
        try:
            self.assertFalse(client.authenticate_callback({'X-MerchantId': '', 'X-Secret': ''}))
            with self.assertRaises(PlategaError):
                await client.get_payment(ID)
        finally:
            await client.aclose()

    async def test_callback_requires_both_exact_credentials(self):
        client = PlategaClient('merchant', 'secret')
        try:
            self.assertTrue(client.authenticate_callback({'X-MerchantId': 'merchant', 'X-Secret': 'secret'}))
            for headers in [{}, {'X-MerchantId': 'merchant'},
                            {'X-MerchantId': 'other', 'X-Secret': 'secret'},
                            {'X-MerchantId': 'merchant', 'X-Secret': 'wrong'}]:
                self.assertFalse(client.authenticate_callback(headers))
        finally:
            await client.aclose()

    async def test_creation_timeout_is_not_automatically_retried(self):
        calls = []
        def respond(request):
            calls.append(request)
            raise httpx.ReadTimeout('sensitive server response', request=request)
        client = PlategaClient('merchant', 'VERY_SECRET', transport=httpx.MockTransport(respond))
        try:
            with self.assertRaises(PlategaError) as raised:
                await client.create_payment(100, 'Оплата', 'https://example.test/', 42)
            self.assertNotIn('VERY_SECRET', str(raised.exception))
        finally:
            await client.aclose()
        self.assertEqual(len(calls), 1)

    def test_money_rejects_nonfinite_negative_and_fractional_kopeks(self):
        self.assertEqual(money('339.15'), Decimal('339.15'))
        for amount in ['NaN', 'Infinity', -1, 100.001, 'oops']:
            with self.assertRaises(ValueError):
                money(amount)
