"""Platega API: https://docs.platega.io/ (разовые платежи)."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import math
import asyncio
from email.utils import parsedate_to_datetime
import hmac
import json
from urllib.parse import urlparse
from uuid import UUID

import httpx


class PlategaError(RuntimeError):
    """Ошибка шлюза без токенов, заголовков и тела ответа в сообщении."""

    def __init__(self, message: str, *, reason: str = 'invalid_response',
                 status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code
        self.retryable = retryable


def _retry_delay(response: httpx.Response) -> float | None:
    """Только короткий повтор; долгий Retry-After оставляем планировщику."""
    value = response.headers.get('Retry-After')
    if value is None:
        return 0.5
    try:
        delay = float(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            delay = (deadline - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    if not math.isfinite(delay) or delay > 2:
        return None
    return max(0, delay)


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


def verified_order_amount(data: dict, expected_amount, currency: str = 'RUB', *,
                          order_id: str | None = None, user_id: int | None = None) -> Decimal:
    """Сумма заказа, независимо подтверждённая ответом GET Platega.

    Наблюдаемый ответ с комиссией поверх цены содержит gross в paymentDetails,
    comission отдельно и comissionType=0. Принимаем только gross - commission, равное цене своего заказа
    либо округлённое вверх до целого рубля, а не произвольную переплату.
    """
    try:
        expected = money(expected_amount)
        details = data['paymentDetails']
        gross = money(details['amount'])
        if details['currency'] != currency:
            raise ValueError
        if gross == expected:
            return expected
        commission = money(data['comission'])
        commission_type = data['comissionType']
        # Platega может округлить сумму плательщика вверх до целого рубля (140 при
        # заказе 129 и комиссии 10.32). Любое другое расхождение отклоняем.
        priced = expected + commission
        if (expected <= 0 or gross <= expected or commission <= 0
                or gross not in (priced, priced.to_integral_value(ROUND_CEILING))
                or type(commission_type) is not int or commission_type != 0):
            raise ValueError
        # На несовпадение цен допускается только надбавка к нашему же заказу.
        # Пользователя и orderId берём из независимого GET, не из callback.
        payload = json.loads(data['payload'])
        if (not isinstance(payload, dict) or order_id is None or user_id is None
                or transaction_id(payload['orderId']) != transaction_id(order_id)
                or str(payload['userId']) != str(user_id)):
            raise ValueError
        return expected
    except (ValueError, KeyError, TypeError):
        raise ValueError('Сумма, валюта или привязка платежа не совпадает с заказом') from None


# Интервалы рекуррентных СБП-подписок Platega: 1 день, 2 неделя, 3 месяц (30 дней), 4 год.
# Лимит intervalCount зависит от интервала.
INTERVAL_DAY, INTERVAL_WEEK, INTERVAL_MONTH, INTERVAL_YEAR = 1, 2, 3, 4
_INTERVAL_LIMITS = {INTERVAL_DAY: 31, INTERVAL_WEEK: 4, INTERVAL_MONTH: 12, INTERVAL_YEAR: 3}
SUBSCRIPTION_STATUSES = ('PendingAgreement', 'Active', 'PastDue', 'Cancelled', 'Failed')


def subscription_interval(duration_days: int) -> tuple[int, int] | None:
    """Период подписки Platega, который соответствует сроку тарифа, либо None.

    Месяц у Platega — ровно 30 дней, год — 365: так срок, который платит клиент,
    совпадает со сроком, который мы выдаём. Если срок не ложится на допустимые
    интервалы, автопродление для такого тарифа не предлагается.
    """
    try:
        days = int(duration_days)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    candidates = []
    if days % 365 == 0:
        candidates.append((INTERVAL_YEAR, days // 365))
    if days % 30 == 0:
        candidates.append((INTERVAL_MONTH, days // 30))
    if days % 7 == 0:
        candidates.append((INTERVAL_WEEK, days // 7))
    candidates.append((INTERVAL_DAY, days))
    for interval, count in candidates:
        if 1 <= count <= _INTERVAL_LIMITS[interval]:
            return interval, count
    return None


def normalize_callback(body: dict) -> dict:
    """Ключи callback приходят то строчными (`id`), то заглавными (`Id`) — приводим к строчным."""
    return {str(key).lower(): value for key, value in body.items()}


def parse_platega_time(value) -> datetime | None:
    """ISO-время Platega (UTC) → наивное локальное время, как хранятся даты в БД."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().replace(tzinfo=None)


SUBSCRIPTION_CALLBACK_STATUSES = (
    'SUBSCRIPTION_ACTIVATED', 'SUBSCRIPTION_PAST_DUE', 'SUBSCRIPTION_CANCELLED', 'SUBSCRIPTION_FAILED',
)


def callback_kind(data: dict) -> str:
    """Тип callback по нормализованному телу: 'payment', 'subscription_charge' или 'subscription_status'.

    Статус подписки — отдельные значения SUBSCRIPTION_*; списание по подписке отличается от
    обычного платежа только полями SubscriptionId и NextChargeAt.
    """
    if str(data.get('status', '')).upper() in SUBSCRIPTION_CALLBACK_STATUSES:
        return 'subscription_status'
    if data.get('subscriptionid'):
        return 'subscription_charge'
    return 'payment'


def parse_charge(body: dict) -> dict:
    """Поля callback списания в единой форме (ключи Platega приходят в разном регистре)."""
    data = normalize_callback(body)
    return {
        'id': data.get('id'), 'amount': data.get('amount'), 'currency': data.get('currency'),
        'status': data.get('status'), 'subscription_id': data.get('subscriptionid'),
        'next_charge_at': parse_platega_time(data.get('nextchargeat')),
    }


def whole_rubles(value) -> int:
    """Сумма регулярного списания — целое число рублей (API подписок не принимает копейки)."""
    amount = money(value)
    if amount != amount.to_integral_value() or amount <= 0:
        raise ValueError('Сумма автопродления должна быть целым числом рублей')
    return int(amount)


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
            raise PlategaError('Platega не настроена', reason='configuration')
        # Повторяем только чтение: повтор POST может создать второй платеж.
        attempts = 2 if method.upper() == 'GET' else 1
        for attempt in range(attempts):
            delay = 0.5
            try:
                response = await self._client.request(method, path, **kwargs)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                retryable = status in (408, 429) or status >= 500
                messages = {
                    401: 'Platega отклонила авторизацию',
                    403: 'Platega запретила доступ к операции',
                    404: 'Platega не нашла запрошенную транзакцию',
                    429: 'Platega ограничила частоту запросов',
                }
                message = messages.get(status, 'Platega вернула ошибку HTTP')
                error = PlategaError(f'{message} (HTTP {status})', reason='http',
                                     status_code=status, retryable=retryable)
                delay = _retry_delay(exc.response)
            except httpx.TimeoutException:
                error = PlategaError('Истекло время ожидания ответа Platega',
                                     reason='timeout', retryable=True)
            except httpx.RequestError:
                error = PlategaError('Не удалось соединиться с Platega',
                                     reason='network', retryable=True)
            else:
                try:
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError
                    return data
                except (ValueError, TypeError):
                    raise PlategaError('Platega вернула некорректный JSON-ответ',
                                       status_code=response.status_code) from None
            if error.retryable and attempt + 1 < attempts and delay is not None:
                await asyncio.sleep(delay)
                continue
            raise error from None

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

    async def create_subscription(self, amount, description: str,
                                  interval: int, interval_count: int) -> CreatedPayment:
        """Рекуррентная СБП-подписка: платёж при создании не проходит, списания делает Platega.

        `transactionId` в ответе — ID подписки; по нему приходят callback'и.
        Плательщика нужно сразу отправить на `redirect`: на привязку счёта даётся 30 минут.
        """
        amount = whole_rubles(amount)
        if interval not in _INTERVAL_LIMITS or not 1 <= interval_count <= _INTERVAL_LIMITS[interval]:
            raise ValueError('Недопустимый период подписки')
        body = {
            'paymentMethod': 6,
            'paymentDetails': {'amount': amount, 'currency': 'RUB',
                               'interval': interval, 'intervalCount': interval_count},
            'description': description[:255],
        }
        # Как и разовый платёж, создание не повторяется автоматически.
        data = await self._request('POST', '/transaction/process', json=body)
        try:
            identifier = transaction_id(data['transactionId'])
            url = data.get('redirect') or data['url']
            parsed = urlparse(url)
            if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError
            status = data['status']
            if status != 'PENDING':
                raise ValueError
            return CreatedPayment(identifier, url, status)
        except (KeyError, ValueError, TypeError):
            raise PlategaError('Platega вернула некорректную ссылку подписки') from None

    async def get_subscription(self, identifier: str) -> dict:
        identifier = transaction_id(identifier)
        data = await self._request('GET', f'/subscription/{identifier}')
        try:
            if transaction_id(data['id']) != identifier:
                raise ValueError
            if data['status'] not in SUBSCRIPTION_STATUSES:
                raise ValueError
            whole_rubles(data['amount'])
            return data
        except (KeyError, ValueError, TypeError):
            raise PlategaError('Platega вернула некорректные детали подписки') from None

    async def cancel_subscription(self, identifier: str) -> None:
        """Останавливает будущие списания. Метод идемпотентен на стороне Platega."""
        identifier = transaction_id(identifier)
        await self._request('POST', f'/subscription/{identifier}/cancel')

    async def aclose(self):
        await self._client.aclose()
