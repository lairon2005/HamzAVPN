"""Отказы шлюза: повторять чтение безопасно, создание платежа — нет."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import traceback
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from platega.client import PlategaClient, PlategaError

ID = '00000000-0000-0000-0000-000000000001'
PAYMENT = {'id': ID, 'status': 'CANCELED',
           'paymentDetails': {'amount': 67, 'currency': 'RUB'}}


class PlategaFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_recovers_after_timeout_or_http_503(self):
        for failure in ('timeout', 'http'):
            with self.subTest(failure=failure):
                calls = []
                def respond(request):
                    calls.append(request)
                    if len(calls) == 1:
                        if failure == 'timeout':
                            raise httpx.ReadTimeout('private gateway body', request=request)
                        return httpx.Response(503, text='private gateway body')
                    return httpx.Response(200, json=PAYMENT)
                client = PlategaClient('merchant', 'secret', transport=httpx.MockTransport(respond))
                try:
                    with patch('platega.client.asyncio.sleep', new_callable=AsyncMock) as sleep:
                        self.assertEqual(await client.get_payment(ID), PAYMENT)
                        sleep.assert_awaited_once_with(0.5)
                finally:
                    await client.aclose()
                self.assertEqual(len(calls), 2)

    async def test_http_refusal_retains_only_safe_status(self):
        for status in (302, 400, 401, 403, 404, 429, 503):
            with self.subTest(status=status):
                calls = []
                def respond(request):
                    calls.append(request)
                    return httpx.Response(status, text='PRIVATE_BODY PRIVATE_SECRET',
                                          headers={'Location': 'https://example.test/PRIVATE_SECRET'})
                client = PlategaClient('PRIVATE_MERCHANT', 'PRIVATE_SECRET',
                                       transport=httpx.MockTransport(respond))
                try:
                    with patch('platega.client.asyncio.sleep', new_callable=AsyncMock):
                        with self.assertRaises(PlategaError) as raised:
                            await client.get_payment(ID)
                    error = raised.exception
                    self.assertEqual(error.status_code, status)
                    self.assertEqual(error.reason, 'http')
                    self.assertEqual(error.retryable, status in (429, 503))
                    rendered = ''.join(traceback.format_exception(error))
                    for private in ('PRIVATE_BODY', 'PRIVATE_SECRET', 'PRIVATE_MERCHANT', ID):
                        self.assertNotIn(private, rendered)
                finally:
                    await client.aclose()
                self.assertEqual(len(calls), 2 if status in (429, 503) else 1)

    async def test_network_failure_has_safe_reason_without_exception_details(self):
        for exception_type, reason in ((httpx.ConnectError, 'network'), (httpx.ReadTimeout, 'timeout')):
            with self.subTest(reason=reason):
                calls = []
                def respond(request):
                    calls.append(request)
                    raise exception_type('PRIVATE_SECRET https://private.test', request=request)
                client = PlategaClient('merchant', 'PRIVATE_SECRET', transport=httpx.MockTransport(respond))
                try:
                    with patch('platega.client.asyncio.sleep', new_callable=AsyncMock):
                        with self.assertRaises(PlategaError) as raised:
                            await client.get_payment(ID)
                    self.assertEqual(raised.exception.reason, reason)
                    self.assertTrue(raised.exception.retryable)
                    self.assertIsNone(raised.exception.status_code)
                    rendered = ''.join(traceback.format_exception(raised.exception))
                    self.assertNotIn('PRIVATE_SECRET', rendered)
                    self.assertNotIn('private.test', rendered)
                finally:
                    await client.aclose()
                self.assertEqual(len(calls), 2)

    async def test_invalid_json_has_separate_diagnostic_and_is_not_retried(self):
        for body in ('PRIVATE_SECRET HTML', '[]', 'null'):
            with self.subTest(body=body):
                calls = []
                def respond(request):
                    calls.append(request)
                    return httpx.Response(200, text=body)
                client = PlategaClient('merchant', 'PRIVATE_SECRET', transport=httpx.MockTransport(respond))
                try:
                    with self.assertRaises(PlategaError) as raised:
                        await client.get_payment(ID)
                    self.assertEqual(raised.exception.reason, 'invalid_response')
                    self.assertEqual(raised.exception.status_code, 200)
                    self.assertFalse(raised.exception.retryable)
                    self.assertNotIn('PRIVATE_SECRET', str(raised.exception))
                finally:
                    await client.aclose()
                self.assertEqual(len(calls), 1)

    async def test_short_retry_after_is_respected_and_long_or_invalid_is_deferred(self):
        future = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=10), usegmt=True)
        for header, expected_delay in (('1', 1.0), ('600', None), (future, None),
                                       ('invalid', None), ('NaN', None)):
            with self.subTest(header=header):
                calls = []
                def respond(request):
                    calls.append(request)
                    return httpx.Response(429, headers={'Retry-After': header})
                client = PlategaClient('merchant', 'secret', transport=httpx.MockTransport(respond))
                try:
                    with patch('platega.client.asyncio.sleep', new_callable=AsyncMock) as sleep:
                        with self.assertRaises(PlategaError):
                            await client.get_payment(ID)
                        if expected_delay is None:
                            sleep.assert_not_awaited()
                        else:
                            sleep.assert_awaited_once_with(expected_delay)
                finally:
                    await client.aclose()
                self.assertEqual(len(calls), 1 if expected_delay is None else 2)

    async def test_creation_is_not_retried_on_http_error(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(503, text='PRIVATE_SECRET')
        client = PlategaClient('merchant', 'PRIVATE_SECRET', transport=httpx.MockTransport(respond))
        try:
            with patch('platega.client.asyncio.sleep', new_callable=AsyncMock) as sleep:
                with self.assertRaises(PlategaError) as raised:
                    await client.create_payment(67, 'Оплата', 'https://example.test/', 42)
                sleep.assert_not_awaited()
                self.assertTrue(raised.exception.retryable)
        finally:
            await client.aclose()
        self.assertEqual(len(calls), 1)
