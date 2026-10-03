"""
HTTP-клиент к Remnawave Panel REST API.

Документация: https://docs.rw/
OpenAPI spec:  <REMNAWAVE_API_URL>/api (Swagger UI)

Аутентификация: Bearer API token, генерируется в Dashboard → API Keys.
Для панелей за eGames reverse proxy опционально передаётся access cookie.
Env vars: REMNAWAVE_API_URL, REMNAWAVE_API_TOKEN,
REMNAWAVE_DEFAULT_SQUAD_UUID, REMNAWAVE_ACCESS_COOKIE,
REMNAWAVE_PROXY_URL.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_HTTP_CONNECT_TIMEOUT = 3.0
_HTTP_READ_TIMEOUT = 6.0
_HTTP_WRITE_TIMEOUT = 5.0
_HTTP_POOL_TIMEOUT = 1.0
_HTTP_MAX_CONNECTIONS = 20
_HTTP_MAX_KEEPALIVE_CONNECTIONS = 10
_HTTP_KEEPALIVE_EXPIRY = 30.0
# GET-запросы идемпотентны. Несколько попыток сглаживают кратковременные
# провалы маршрута до панели, которые в production иногда длятся 2–3 подключения.
# Изменяющие запросы по-прежнему выполняются ровно один раз ниже в _request.
_HTTP_GET_ATTEMPTS = 4
_HTTP_RETRY_BASE_DELAY = 0.25
_HTTP_RETRY_AFTER_CAP = 2.0
# Жёсткий потолок суммарного времени всех ретраев одного GET. Вызовы идут из
# интерактивных хендлеров бота, которые обязаны ответить в пределах окна
# Telegram (~15 c на callback), иначе пользователь видит подвисание, а повторный
# `.answer()` падает с "query is too old". Новую попытку не начинаем и не спим
# дольше, чем осталось до дедлайна; при connect-таймаутах (3 c) успеваем ~3
# попытки, при read-таймаутах (6 c) — ~2, вместо прежних гарантированных 4×8 c.
_HTTP_GET_RETRY_BUDGET = 8.0
_HTTP_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_HTTP_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)
# Переключение на запасной маршрут (прокси) для ИЗМЕНЯЮЩИХ запросов допустимо
# только когда соединение вообще не установилось — тогда сервер точно ничего не
# применил и повтор через прокси безопасен. ReadTimeout/ReadError/RemoteProtocol
# неоднозначны: панель могла уже продлить подписку или списать трафик, а ответ
# потеряться, — повтор создал бы дубликат. Такие мутации на прокси не уводим.
_FAILOVER_SAFE_MUTATION_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)
_MAX_ERROR_BODY_LENGTH = 2048
_USERS_PAGE_SIZE = 1000


class RemnawaveError(Exception):
    """Базовая ошибка клиента Remnawave."""


class RemnawaveAPIError(RemnawaveError):
    """Remnawave вернул HTTP-статус вне диапазона 2xx."""

    def __init__(
        self,
        status: int,
        body: str,
        *,
        request_id: str | None = None,
    ) -> None:
        self.status = status
        self.body = body[:_MAX_ERROR_BODY_LENGTH]
        self.request_id = request_id
        request_suffix = f" (request_id={request_id})" if request_id else ""
        # Тело намеренно не включаем в str(exception): оно может содержать
        # служебные данные или ссылку подписки и попасть в error-уведомление.
        super().__init__(f"Remnawave API returned HTTP {status}{request_suffix}")


class RemnawaveTransportError(RemnawaveError):
    """Запрос не завершился из-за сетевой ошибки или исчерпания пула."""

    def __init__(self, method: str, category: str) -> None:
        self.method = method
        self.category = category
        super().__init__(
            f"Remnawave transport failure during {method}: {category}"
        )


class RemnawaveInvalidResponseError(RemnawaveError):
    """Remnawave вернул успешный HTTP-ответ с неверным форматом."""

    def __init__(
        self,
        status: int,
        reason: str,
        *,
        content_type: str = "",
        request_id: str | None = None,
    ) -> None:
        self.status = status
        self.reason = reason
        self.content_type = content_type
        self.request_id = request_id
        request_suffix = f" (request_id={request_id})" if request_id else ""
        super().__init__(
            f"Invalid Remnawave response: {reason}, HTTP {status}{request_suffix}"
        )


def _request_id(resp: httpx.Response) -> str | None:
    raw_request_id = (
        resp.headers.get("x-request-id")
        or resp.headers.get("x-correlation-id")
    )
    if not raw_request_id:
        return None
    normalized = "".join(
        character for character in raw_request_id.strip() if character.isprintable()
    )
    return normalized[:128] or None


def _decode_response(
    resp: httpx.Response,
    *,
    allow_empty: bool = False,
) -> dict | None:
    """Проверяет статус и JSON-контракт, не раскрывая тело в исключениях."""
    request_id = _request_id(resp)
    if not 200 <= resp.status_code < 300:
        raise RemnawaveAPIError(
            resp.status_code,
            resp.text,
            request_id=request_id,
        )

    if resp.status_code == 204 or not resp.content:
        if allow_empty:
            return None
        raise RemnawaveInvalidResponseError(
            resp.status_code,
            "empty response",
            content_type=resp.headers.get("content-type", ""),
            request_id=request_id,
        )

    content_type = resp.headers.get("content-type", "")
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type != "application/json" and not media_type.endswith("+json"):
        raise RemnawaveInvalidResponseError(
            resp.status_code,
            "unexpected content type",
            content_type=content_type,
            request_id=request_id,
        )

    try:
        payload = resp.json()
    except ValueError:
        raise RemnawaveInvalidResponseError(
            resp.status_code,
            "malformed JSON",
            content_type=content_type,
            request_id=request_id,
        ) from None

    if not isinstance(payload, dict):
        raise RemnawaveInvalidResponseError(
            resp.status_code,
            "unexpected JSON root",
            content_type=content_type,
            request_id=request_id,
        )
    return payload


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    retry_after_seconds = _parse_retry_after(retry_after)
    if retry_after_seconds is not None:
        return min(max(0.0, retry_after_seconds), _HTTP_RETRY_AFTER_CAP)

    upper_bound = _HTTP_RETRY_BASE_DELAY * (2 ** max(0, attempt - 1))
    return random.uniform(0.0, upper_bound)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None

    try:
        return float(value)
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return (retry_at - datetime.now(timezone.utc)).total_seconds()


def _safe_int(value: Any, default: int = 0) -> int:
    """Безопасно приводит значение к int (панель может прислать строку/None)."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _safe_int_or_none(value: Any) -> int | None:
    """Как _safe_int, но при отсутствии/некорректности значения возвращает
    None, а не 0 — для полей, где 0 является отличным от "неизвестно" значением
    (cpuCount, totalRam)."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _find_metric_value(entry: dict, substrings: tuple[str, ...]) -> float | None:
    """Ищет в entry первое поле, чей ключ содержит одну из подстрок
    (регистронезависимо), и безопасно приводит его значение к float."""
    for key, value in entry.items():
        if not isinstance(key, str):
            continue
        lowered = key.lower()
        if not any(substring in lowered for substring in substrings):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _node_load(raw_node: dict) -> tuple[float | None, float | None]:
    """Извлекает загрузку CPU и RAM (в процентах) из поля node.system.

    Remnawave отдаёт у ноды в /api/nodes объект system:
      system.info.cpus, system.info.memoryTotal — характеристики железа;
      system.stats.loadAvg[0], system.stats.memoryUsed — живые метрики.
    CPU% ≈ loadAvg(1 мин) / cpus * 100; RAM% = memoryUsed / memoryTotal * 100.
    Любое отсутствие/некорректность данных → None (тогда метрику не показываем).
    """
    system = raw_node.get("system")
    if not isinstance(system, dict):
        return None, None
    info = system.get("info") if isinstance(system.get("info"), dict) else {}
    stats = system.get("stats") if isinstance(system.get("stats"), dict) else {}

    cpu_usage: float | None = None
    load_avg = stats.get("loadAvg")
    cpus = info.get("cpus")
    if isinstance(load_avg, (list, tuple)) and load_avg and cpus:
        try:
            cpu_usage = float(load_avg[0]) / float(cpus) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            cpu_usage = None

    ram_usage: float | None = None
    mem_used = stats.get("memoryUsed")
    mem_total = info.get("memoryTotal")
    if mem_used is not None and mem_total:
        try:
            ram_usage = float(mem_used) / float(mem_total) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            ram_usage = None

    return cpu_usage, ram_usage


@dataclass
class _Route:
    """Один сетевой путь до панели: подпись для логов + свой httpx-клиент.

    Клиент держит собственный transport (прямой или через прокси), поэтому у
    маршрутов независимые пулы соединений — падение прямого пути не отравляет
    keepalive-соединения прокси-пути.
    """

    label: str
    client: httpx.AsyncClient


class RemnawaveClient:
    """
    Тонкая обёртка над Remnawave REST API.

    Все методы async. Создаётся один раз (синглтон через loader.py).

    Основные операции:
      - create_user / delete_user / get_user_by_username / update_user
      - disable_user / enable_user / reset_user_traffic
      - get_all_users

    Отказоустойчивость маршрута: сначала запрос идёт напрямую, и только если
    прямой путь до панели не отвечает — повторяется через прокси из
    REMNAWAVE_PROXY_URL (если он задан). См. _request / _may_failover.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        squad_uuid: str = "",
        access_cookie: str = "",
        proxy_url: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._squad_uuid = squad_uuid
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if access_cookie:
            self._headers["Cookie"] = access_cookie

        # Один клиент на весь процесс: повторно используем уже установленное
        # TCP/TLS-соединение вместо нового handshake на каждый клик пользователя.
        timeout = httpx.Timeout(
            connect=_HTTP_CONNECT_TIMEOUT,
            read=_HTTP_READ_TIMEOUT,
            write=_HTTP_WRITE_TIMEOUT,
            pool=_HTTP_POOL_TIMEOUT,
        )

        # Список маршрутов в порядке приоритета. Прямой всегда первый; прокси
        # добавляется вторым только если задан REMNAWAVE_PROXY_URL. Запрос идёт
        # по следующему маршруту лишь когда предыдущий не смог достучаться до
        # панели (см. _request).
        self._routes: list[_Route] = []
        if transport is not None:
            # Инъекция транспорта (тесты) — единственный прямой маршрут.
            self._routes.append(
                _Route("direct", self._build_client(timeout, transport))
            )
        else:
            limits = httpx.Limits(
                max_connections=_HTTP_MAX_CONNECTIONS,
                max_keepalive_connections=_HTTP_MAX_KEEPALIVE_CONNECTIONS,
                keepalive_expiry=_HTTP_KEEPALIVE_EXPIRY,
            )
            direct_transport = httpx.AsyncHTTPTransport(
                retries=0,
                limits=limits,
                local_address="0.0.0.0",  # на сервере стабильный маршрут только по IPv4
            )
            self._routes.append(
                _Route("direct", self._build_client(timeout, direct_transport))
            )
            if proxy_url:
                proxy_transport = httpx.AsyncHTTPTransport(
                    retries=0,
                    limits=limits,
                    proxy=proxy_url,
                    local_address="0.0.0.0",
                )
                self._routes.append(
                    _Route("proxy", self._build_client(timeout, proxy_transport))
                )

    def _build_client(
        self,
        timeout: httpx.Timeout,
        transport: httpx.AsyncBaseTransport,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers=self._headers,
            follow_redirects=False,
        )

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        allow_empty: bool = False,
    ) -> dict | None:
        """Выполняет запрос по маршрутам и нормализует ошибки.

        Сначала пробуется прямой маршрут (с ретраями GET, как раньше). Если он
        не смог достучаться до панели — запрос повторяется через прокси-маршрут
        (когда тот настроен и переключение безопасно, см. _may_failover).
        """
        method = method.upper()
        url = f"{self._base_url}{path}"

        for route_index, route in enumerate(self._routes):
            is_last_route = route_index == len(self._routes) - 1
            try:
                resp = await self._request_via_route(
                    route, method, url, json=json, params=params
                )
            except _HTTP_RETRYABLE_EXCEPTIONS as exc:
                if not is_last_route and self._may_failover(method, exc):
                    logger.warning(
                        "[remnawave] route '%s' unreachable (%s); failing over: "
                        "method=%s path=%s",
                        route.label,
                        type(exc).__name__,
                        method,
                        path,
                    )
                    continue
                raise RemnawaveTransportError(method, type(exc).__name__) from exc
            except httpx.HTTPError as exc:
                # Неретраябельная сетевая/протокольная ошибка — другой маршрут
                # её не исправит, поднимаем сразу.
                raise RemnawaveTransportError(method, type(exc).__name__) from exc

            # Ответ получен. Для GET, если после исчерпания ретраев это всё ещё
            # временный статус (429/5xx) и есть запасной маршрут — пробуем его:
            # проблема может быть во фронт-слое панели на прямом пути.
            if (
                not is_last_route
                and method == "GET"
                and resp.status_code in _HTTP_RETRYABLE_STATUSES
            ):
                logger.warning(
                    "[remnawave] route '%s' exhausted temporary status %s; "
                    "failing over: path=%s",
                    route.label,
                    resp.status_code,
                    path,
                )
                continue

            return _decode_response(resp, allow_empty=allow_empty)

        raise AssertionError("Remnawave request routes exhausted unexpectedly")

    async def _request_via_route(
        self,
        route: _Route,
        method: str,
        url: str,
        *,
        json: Any,
        params: dict | None,
    ) -> httpx.Response:
        """Выполняет запрос по одному маршруту с интра-маршрутными ретраями GET.

        Возвращает httpx.Response (в т.ч. с временным статусом, если бюджет
        ретраев исчерпан). Пробрасывает исходное httpx-исключение, если все
        попытки упали транспортно, — решение о переключении маршрута и о
        нормализации ошибки принимает вызывающий _request.
        """
        attempts = _HTTP_GET_ATTEMPTS if method == "GET" else 1
        deadline = time.monotonic() + _HTTP_GET_RETRY_BUDGET

        for attempt in range(1, attempts + 1):
            try:
                resp = await route.client.request(
                    method,
                    url,
                    json=json,
                    params=params,
                )
            except _HTTP_RETRYABLE_EXCEPTIONS:
                now = time.monotonic()
                if attempt < attempts and now < deadline:
                    delay = min(_retry_delay(attempt), max(0.0, deadline - now))
                    logger.warning(
                        "[remnawave] retrying read after transport error: "
                        "route=%s method=%s attempt=%s/%s",
                        route.label,
                        method,
                        attempt,
                        attempts,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

            if method == "GET" and resp.status_code in _HTTP_RETRYABLE_STATUSES:
                now = time.monotonic()
                if attempt < attempts and now < deadline:
                    delay = min(
                        _retry_delay(attempt, resp.headers.get("Retry-After")),
                        max(0.0, deadline - now),
                    )
                    logger.warning(
                        "[remnawave] retrying read after temporary API response: "
                        "route=%s method=%s attempt=%s/%s status=%s",
                        route.label,
                        method,
                        attempt,
                        attempts,
                        resp.status_code,
                    )
                    await asyncio.sleep(delay)
                    continue

            return resp

        raise AssertionError("Remnawave route attempts exhausted unexpectedly")

    @staticmethod
    def _may_failover(method: str, exc: Exception) -> bool:
        """Можно ли повторить запрос по следующему маршруту после ошибки exc.

        GET идемпотентен — уводим всегда. Изменяющие запросы — только если
        соединение не установилось (гарантированно ничего не применилось).
        """
        if method == "GET":
            return True
        return isinstance(exc, _FAILOVER_SAFE_MUTATION_EXCEPTIONS)

    async def aclose(self) -> None:
        """Закрывает HTTP-клиенты всех маршрутов при остановке процесса."""
        for route in self._routes:
            await route.client.aclose()

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def create_user(
        self,
        *,
        username: str,
        user_uuid: str | None = None,
        expire_at: str | None = None,
        traffic_limit_bytes: int | None = None,
        traffic_limit_strategy: str | None = None,
        description: str | None = None,
    ) -> dict:
        """
        Создаёт пользователя в Remnawave.

        username               — уникальный идентификатор (a-zA-Z0-9_-, 3..36 символов)
        user_uuid              — VLESS UUID (опционально, панель генерирует автоматически)
        expire_at              — дата истечения в формате ISO 8601
        traffic_limit_bytes    — лимит трафика в байтах (0 / None = безлимит)
        traffic_limit_strategy — стратегия сброса: NO_RESET/DAY/WEEK/MONTH/MONTH_ROLLING
                                 (None → дефолт панели NO_RESET)

        Пользователь сразу зачисляется в Internal Squad (self._squad_uuid) через поле
        activeInternalSquads — без сквада у него не будет ни одного inbound и VPN не заработает.
        """
        if expire_at is None:
            expire_at = (
                datetime.now(timezone.utc) + timedelta(days=365)
            ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        payload: dict[str, Any] = {
            "username": username,
            "expireAt": expire_at,
            "status": "ACTIVE",
        }
        if user_uuid:
            payload["vlessUuid"] = user_uuid
        if traffic_limit_bytes is not None:
            payload["trafficLimitBytes"] = traffic_limit_bytes
        if traffic_limit_strategy:
            payload["trafficLimitStrategy"] = traffic_limit_strategy
        if self._squad_uuid:
            payload["activeInternalSquads"] = [self._squad_uuid]
        if description:
            payload["description"] = description

        resp = await self._request("POST", "/api/users", json=payload)
        user = resp.get("response", resp)
        logger.info(
            "[remnawave] user created: username=%s uuid=%s squad=%s",
            username, user.get("uuid"), self._squad_uuid or "—",
        )
        return user

    async def delete_user(self, user_uuid: str) -> None:
        """Удаляет пользователя по его UUID (не username)."""
        await self._request(
            "DELETE",
            f"/api/users/{user_uuid}",
            allow_empty=True,
        )
        logger.info("[remnawave] user deleted: uuid=%s", user_uuid)

    async def get_user_by_username(self, username: str) -> dict | None:
        """Возвращает пользователя по username или None если не найден."""
        try:
            resp = await self._request("GET", f"/api/users/by-username/{username}")
            return resp.get("response", resp)
        except RemnawaveAPIError as e:
            if e.status == 404:
                return None
            raise

    async def update_user(
        self,
        user_uuid: str,
        *,
        expire_at: str | None = None,
        traffic_limit_bytes: int | None = None,
        traffic_limit_strategy: str | None = None,
        hwid_device_limit: int | None = None,
    ) -> dict:
        """
        Обновляет пользователя Remnawave (дату истечения, лимит трафика, лимит устройств).

        user_uuid              — UUID пользователя в Remnawave (uuid имеет приоритет над username)
        expire_at              — ISO 8601 строка, напр. "2027-01-01T00:00:00.000Z"
        traffic_limit_bytes    — новый лимит трафика в байтах (0 = безлимит)
        traffic_limit_strategy — стратегия сброса: NO_RESET/DAY/WEEK/MONTH/MONTH_ROLLING
        hwid_device_limit      — персональный лимит устройств, АБСОЛЮТНОЕ значение
                                 (базовый лимит + докупленные слоты)

        Все параметры опциональны — в PATCH-payload попадают только заданные поля.

        Важно про hwid_device_limit: пока поле NULL, на пользователя действует
        глобальный `fallbackDeviceLimit` из subscription-settings. Как только мы
        записали персональное число, глобальная настройка для него перестаёт
        что-либо значить — поэтому писать сюда нужно всегда абсолютное
        `база + слоты`, а при смене базы прогонять всех, у кого поле заполнено
        (это делает джоб sync_device_limits).
        """
        payload: dict[str, Any] = {"uuid": user_uuid}
        if expire_at is not None:
            payload["expireAt"] = expire_at
        if traffic_limit_bytes is not None:
            payload["trafficLimitBytes"] = traffic_limit_bytes
        if traffic_limit_strategy:
            payload["trafficLimitStrategy"] = traffic_limit_strategy
        if hwid_device_limit is not None:
            payload["hwidDeviceLimit"] = hwid_device_limit

        resp = await self._request("PATCH", "/api/users", json=payload)
        user = resp.get("response", resp)
        logger.info(
            "[remnawave] user updated: uuid=%s expire_at=%s traffic_limit_bytes=%s hwid_limit=%s",
            user_uuid, expire_at, traffic_limit_bytes, hwid_device_limit,
        )
        return user

    async def update_user_expiry(self, user_uuid: str, expire_at: str) -> dict:
        """
        Обратная совместимость: обновляет только дату истечения пользователя.
        Тонкая обёртка над update_user().
        """
        return await self.update_user(user_uuid, expire_at=expire_at)

    async def get_all_users(self) -> list[dict]:
        """Возвращает всех пользователей с учётом пагинации size/start."""
        users: list[dict] = []
        start = 0

        while True:
            resp = await self._request(
                "GET",
                "/api/users",
                params={"size": _USERS_PAGE_SIZE, "start": start},
            )
            if not resp:
                break

            response = resp.get("response", {})
            page = response.get("users", [])
            total = int(response.get("total") or 0)
            if not page:
                break

            users.extend(page)
            start += len(page)

            if total and start >= total:
                break

        return users

    async def disable_user(self, user_uuid: str) -> dict:
        """Деактивирует пользователя (без удаления)."""
        resp = await self._request("POST", f"/api/users/{user_uuid}/actions/disable")
        return resp.get("response", resp)

    async def enable_user(self, user_uuid: str) -> dict:
        """Активирует пользователя."""
        resp = await self._request("POST", f"/api/users/{user_uuid}/actions/enable")
        return resp.get("response", resp)

    async def reset_user_traffic(self, user_uuid: str) -> dict:
        """
        Обнуляет накопленный трафик пользователя (POST /api/users/{uuid}/actions/reset-traffic).
        Используется при покупке/продлении платного тарифа, чтобы дать свежую квоту
        (а не наследовать израсходованный трафик прошлого периода).
        """
        resp = await self._request(
            "POST",
            f"/api/users/{user_uuid}/actions/reset-traffic",
            allow_empty=True,
        )
        logger.info("[remnawave] user traffic reset: uuid=%s", user_uuid)
        return resp.get("response", resp) if resp else {}

    async def revoke_user_subscription(self, user_uuid: str) -> dict:
        """
        Перевыпускает подписку пользователя (POST /api/users/{uuid}/actions/revoke).

        Панель генерирует новый shortUuid (а значит и subscriptionUrl) и новые
        секреты протоколов — vlessUuid, trojanPassword, ssPassword. Старая
        ссылка после этого мертва, то есть это единственный способ отобрать
        доступ у того, кому ключ утёк.

        Тело запроса не отправляем намеренно: без него панель сама генерирует
        shortUuid (в контракте так и написано — свой задавать не рекомендуется),
        а с `revokeOnlyPasswords` ссылка осталась бы прежней, что задачу утечки
        не решает.

        HWID-устройства при перевыпуске НЕ трогаются: записи утёкших устройств
        продолжат занимать слоты hwidDeviceLimit, хотя подключиться уже не
        смогут. Освобождать слоты нужно отдельно — delete_all_user_devices().
        """
        resp = await self._request("POST", f"/api/users/{user_uuid}/actions/revoke")
        user = resp.get("response", resp)
        logger.info("[remnawave] subscription revoked: uuid=%s", user_uuid)
        return user

    # ------------------------------------------------------------------
    # HWID-устройства
    # ------------------------------------------------------------------

    async def get_user_devices(self, user_uuid: str) -> list[dict]:
        """
        Возвращает устройства пользователя (GET /api/hwid/devices/{uuid}).

        Панель отдаёт весь список разом, без пагинации, поэтому нарезкой на
        страницы занимается вызывающий код (см. tgbot/services/device_service.py).

        Поля устройства: hwid, platform, osVersion, deviceModel, userAgent,
        requestIp, createdAt, updatedAt.
        """
        resp = await self._request("GET", f"/api/hwid/devices/{user_uuid}")
        return (resp.get("response") or {}).get("devices", [])

    async def delete_user_device(self, user_uuid: str, hwid: str) -> list[dict]:
        """
        Удаляет одно устройство пользователя и возвращает оставшиеся.

        Панель принимает это POST-ом на devices/delete, а не DELETE-ом
        (см. HWID_ROUTES.DELETE_USER_HWID_DEVICE в контракте панели).
        """
        resp = await self._request(
            "POST",
            "/api/hwid/devices/delete",
            json={"userUuid": user_uuid, "hwid": hwid},
        )
        logger.info("[remnawave] hwid device deleted: uuid=%s hwid=%s", user_uuid, hwid)
        return (resp.get("response") or {}).get("devices", [])

    async def delete_all_user_devices(self, user_uuid: str) -> None:
        """Сбрасывает все устройства пользователя (POST /api/hwid/devices/delete-all)."""
        await self._request(
            "POST",
            "/api/hwid/devices/delete-all",
            json={"userUuid": user_uuid},
            allow_empty=True,
        )
        logger.info("[remnawave] all hwid devices deleted: uuid=%s", user_uuid)

    # ------------------------------------------------------------------
    # Squads
    # ------------------------------------------------------------------

    async def get_all_squads(self) -> list[dict]:
        """Возвращает список всех Internal Squads."""
        resp = await self._request("GET", "/api/internal-squads")
        if not resp:
            return []
        return resp.get("response", {}).get("internalSquads", [])

    # ------------------------------------------------------------------
    # Config Profiles
    # ------------------------------------------------------------------

    async def get_all_config_profiles(self) -> list[dict]:
        """Возвращает список всех Config Profiles."""
        resp = await self._request("GET", "/api/config-profiles")
        if not resp:
            return []
        return resp.get("response", {}).get("configProfiles", [])

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def get_system_stats(self) -> dict:
        """
        Возвращает сводную статистику панели (GET /api/system/stats).

        Ответ панели нормализуется в плоский dict с безопасными дефолтами —
        на случай, если какие-то вложенные поля отсутствуют в конкретной
        версии Remnawave:
          online_now    — response.onlineStats.onlineNow, int, default 0
          users_total   — response.users.totalUsers, int, default 0
          status_counts — response.users.statusCounts, dict, default {}
          nodes_online  — response.nodes.totalOnline, int, default 0
        """
        empty: dict[str, Any] = {
            "online_now": 0,
            "users_total": 0,
            "status_counts": {},
            "nodes_online": 0,
        }

        resp = await self._request("GET", "/api/system/stats")
        if not resp:
            return empty

        response = resp.get("response", resp)
        if not isinstance(response, dict):
            return empty

        online_stats = response.get("onlineStats")
        if not isinstance(online_stats, dict):
            online_stats = {}
        users = response.get("users")
        if not isinstance(users, dict):
            users = {}
        nodes = response.get("nodes")
        if not isinstance(nodes, dict):
            nodes = {}

        status_counts = users.get("statusCounts")
        if not isinstance(status_counts, dict):
            status_counts = {}

        return {
            "online_now": _safe_int(online_stats.get("onlineNow")),
            "users_total": _safe_int(users.get("totalUsers")),
            "status_counts": status_counts,
            "nodes_online": _safe_int(nodes.get("totalOnline")),
        }

    async def get_nodes(self) -> list[dict]:
        """
        Возвращает список нод панели (GET /api/nodes) с нормализованными полями.

        Каждая нода приводится к виду:
          uuid             — str, default ""
          name             — str, default "Без имени"
          country_code     — str, default ""
          is_connected     — bool, default False
          is_online        — bool, isNodeOnline (fallback isConnected), default False
          xray_running     — bool, default False
          users_online     — int, default 0
          cpu_count        — int | None, default None
          total_ram_bytes  — int | None, default None
          raw              — исходный dict ноды целиком

        Возвращает [] если ответ пустой/None или не удалось распознать формат.
        """
        resp = await self._request("GET", "/api/nodes")
        if not resp:
            return []

        response = resp.get("response", resp)
        if isinstance(response, list):
            raw_nodes: list[Any] = response
        elif isinstance(response, dict):
            # У разных версий панели список нод лежит либо под "nodes", либо
            # (реже) снова под вложенным "response".
            nested = response.get("nodes")
            if not isinstance(nested, list):
                nested = response.get("response")
            raw_nodes = nested if isinstance(nested, list) else []
        else:
            raw_nodes = []

        nodes: list[dict] = []
        for raw_node in raw_nodes:
            if not isinstance(raw_node, dict):
                continue

            is_connected = bool(raw_node.get("isConnected") or False)
            is_node_online = raw_node.get("isNodeOnline")
            is_online = bool(is_node_online) if is_node_online is not None else is_connected

            # CPU/RAM живут прямо в узле, в поле system (см. _node_load), а не в
            # отдельном /api/system/nodes/metrics (тот отдаёт только трафик).
            cpu_usage, ram_usage = _node_load(raw_node)

            nodes.append({
                "uuid": raw_node.get("uuid") or "",
                "name": raw_node.get("name") or "Без имени",
                "country_code": raw_node.get("countryCode") or "",
                "is_connected": is_connected,
                "is_online": is_online,
                "xray_running": bool(raw_node.get("isXrayRunning") or False),
                "users_online": _safe_int(raw_node.get("usersOnline")),
                "cpu_count": _safe_int_or_none(raw_node.get("cpuCount")),
                "total_ram_bytes": _safe_int_or_none(raw_node.get("totalRam")),
                "cpu_usage": cpu_usage,
                "ram_usage": ram_usage,
                "raw": raw_node,
            })

        return nodes

    async def get_nodes_metrics(self) -> dict:
        """
        Возвращает метрики CPU/RAM по нодам (GET /api/system/nodes-metrics).

        # TODO: точную структуру ответа этого эндпоинта нужно сверить с
        # реальной панелью — она заметно отличается между версиями Remnawave,
        # а в некоторых сборках эндпоинт может отсутствовать вовсе. Разбор
        # ниже — эвристический (по подстрокам 'cpu'/'mem'/'ram' в ключах);
        # любое несовпадение формата даёт {}, а не исключение.

        Возвращает {node_key -> {"cpu_usage": float | None, "ram_usage": float | None}},
        где node_key — uuid ноды если есть, иначе её name. Пустой dict, если
        ответ пуст или формат не распознан. Сетевые/транспортные исключения
        из self._request не перехватываются — их обрабатывает вызывающий код.
        """
        resp = await self._request("GET", "/api/system/nodes-metrics")
        if not resp:
            return {}

        response = resp.get("response", resp)

        entries: list[dict]
        if isinstance(response, list):
            entries = [item for item in response if isinstance(item, dict)]
        elif isinstance(response, dict):
            entries = []
            for key in ("nodes", "metrics", "items", "data"):
                candidate = response.get(key)
                if isinstance(candidate, list):
                    entries = [item for item in candidate if isinstance(item, dict)]
                    break
            else:
                # Ни один из ожидаемых ключей-списков не найден — пробуем
                # трактовать сам response как {node_key: metrics}.
                for key, value in response.items():
                    if isinstance(value, dict):
                        entries.append({**value, "_node_key": key})
        else:
            entries = []

        result: dict[str, dict] = {}
        for entry in entries:
            node_key = (
                entry.get("_node_key")
                or entry.get("uuid")
                or entry.get("nodeUuid")
                or entry.get("name")
            )
            if not node_key:
                continue

            result[str(node_key)] = {
                "cpu_usage": _find_metric_value(entry, ("cpu",)),
                "ram_usage": _find_metric_value(entry, ("mem", "ram")),
            }

        return result
