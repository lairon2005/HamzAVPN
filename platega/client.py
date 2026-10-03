"""Platega API: https://docs.platega.io/ (разовые платежи)."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hmac
import json
from urllib.parse import urlparse
from uuid import UUID

import httpx


class PlategaError(RuntimeError):
    """Ошибка шлюза без токенов, заголовков и тела ответа в сообщении."""


def money(value) -> Decimal:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('0.01')):
            raise ValueError
        return amount.quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Некорректная сумма платежа') from None


def transaction_id(value) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Некорректный ID транзакции') from None


@dataclass(frozen=True)
class CreatedPayment:
    id: str
    url: str
    status: str


class PlategaClient:
    def __init__(self, merchant_id: str, secret: str,
                 base_url: str = 'https://app.platega.io', transport=None):
        parsed = urlparse(base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError('PLATEGA_API_URL должен быть HTTPS URL без учетных данных')
        self.merchant_id = merchant_id
        self.secret = secret
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip('/'), timeout=httpx.Timeout(20, connect=10),
            headers={'X-MerchantId': merchant_id, 'X-Secret': secret},
            transport=transport, follow_redirects=False,
        )

    @property
    def configured(self) -> bool:
        return bool(self.merchant_id and self.secret)

    def authenticate_callback(self, headers) -> bool:
        if not self.configured:
            return False
        # Сравниваем оба поля независимо; пустые настройки не принимают callback.
        merchant_ok = hmac.compare_digest(str(headers.get('X-MerchantId', '')).encode(),
                                          self.merchant_id.encode())
        secret_ok = hmac.compare_digest(str(headers.get('X-Secret', '')).encode(),
                                        self.secret.encode())
        return merchant_ok & secret_ok

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        if not self.configured:
            raise PlategaError('Platega не настроена')
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (httpx.HTTPError, ValueError, TypeError):
            raise PlategaError('Platega временно недоступна или вернула некорректный ответ') from None

    async def create_payment(self, amount, description: str, return_url: str,
                             user_id: int, metadata: dict | None = None,
                             failed_url: str | None = None) -> CreatedPayment:
        amount = money(amount)
        if amount <= 0:
            raise ValueError('Сумма платежа должна быть больше нуля')
        details = dict(metadata or {})
        details['userId'] = str(user_id)
        payload = json.dumps(details, ensure_ascii=False, separators=(',', ':'))
        body = {
            'paymentDetails': {'amount': float(amount), 'currency': 'RUB'},
            'description': description[:255], 'return': return_url,
            'failedUrl': failed_url or return_url, 'payload': payload,
            'metadata': {'userId': str(user_id)},
        }
        # Создание не повторяется автоматически: API не документирует ключ идемпотентности.
        data = await self._request('POST', '/v2/transaction/process', json=body)
        try:
            identifier = transaction_id(data['transactionId'])
            url = data['url']
            parsed = urlparse(url)
            if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError
            status = data['status']
            if status != 'PENDING':
                raise ValueError
            return CreatedPayment(identifier, url, status)
        except (KeyError, ValueError, TypeError):
            raise PlategaError('Platega вернула некорректную платежную ссылку') from None

    async def get_payment(self, identifier: str) -> dict:
        identifier = transaction_id(identifier)
        data = await self._request('GET', f'/transaction/{identifier}')
        try:
            if transaction_id(data['id']) != identifier:
                raise ValueError
            money(data['paymentDetails']['amount'])
            if data['paymentDetails']['currency'] != 'RUB':
                raise ValueError
            if data['status'] not in ('PENDING', 'CONFIRMED', 'CANCELED', 'CHARGEBACKED'):
                raise ValueError
            return data
        except (KeyError, ValueError, TypeError):
            raise PlategaError('Platega вернула некорректные детали транзакции') from None

    async def aclose(self):
        await self._client.aclose()
