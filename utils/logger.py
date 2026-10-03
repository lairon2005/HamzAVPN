import logging
import queue
import threading
import time
from collections import deque
from html import escape
from collections.abc import Iterable
from typing import Final

import requests


_STOP: Final = object()
_TELEGRAM_TEXT_LIMIT = 3500


def _escape_telegram_code(text: str) -> str:
    """Экранирует и обрезает текст без разрыва HTML entity/UTF-16 лимита."""
    parts: list[str] = []
    wire_length = 0
    utf16_units = 0

    for character in text:
        escaped_character = escape(character)
        character_units = 2 if ord(character) > 0xFFFF else 1
        if (
            wire_length + len(escaped_character) > _TELEGRAM_TEXT_LIMIT
            or utf16_units + character_units > _TELEGRAM_TEXT_LIMIT
        ):
            if wire_length + 1 <= _TELEGRAM_TEXT_LIMIT:
                parts.append("…")
            break
        parts.append(escaped_character)
        wire_length += len(escaped_character)
        utf16_units += character_units

    return "".join(parts)


try:
    from aiohttp.http_exceptions import HttpProcessingError as _AiohttpHttpError
except Exception:  # pragma: no cover — aiohttp всегда установлен в проде
    _AiohttpHttpError = ()


class AiohttpNoiseFilter(logging.Filter):
    """Гасит шум от интернет-сканеров, стучащихся мусором в открытый порт.

    aiohttp логирует каждый некорректный/оборванный HTTP-запрос как
    ``aiohttp.server`` ERROR «Error handling request» с трейсбеком BadStatusLine.
    Это не ошибка приложения, а фоновый интернет-скан: он засоряет логи и
    (через APINotificationHandler на уровне ERROR) улетает админу в Telegram.

    Фильтр отбрасывает ТОЛЬКО записи, чьё исключение — протокольная ошибка HTTP
    (BadStatusLine и родня, все наследники HttpProcessingError) или разрыв
    соединения. Реальные ошибки обработчиков (любой другой тип исключения)
    проходят как обычно.
    """

    _DROP: tuple[type[BaseException], ...] = (_AiohttpHttpError, ConnectionError)

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if exc is not None and isinstance(exc, self._DROP):
            return False
        return True


class CustomFormatter(logging.Formatter):
    def __init__(self):
        super().__init__(
            fmt=(
                "%(filename)s:%(lineno)d #%(levelname)-8s "
                "[%(asctime)s] - %(name)s - %(message)s"
            )
        )


class APINotificationHandler(logging.Handler):
    """Отправляет error-логи админу, не выполняя сеть в event loop.

    ``logging.Handler.emit`` вызывается синхронно даже из async-кода. Поэтому
    здесь он только кладёт готовое сообщение в ограниченную очередь. Сетевой
    запрос выполняет отдельный daemon-поток с обязательным timeout.

    Точная дедупликация и общий rate limit защищают Telegram и приложение от
    шторма уведомлений при массовой недоступности внешнего сервиса.
    """

    def __init__(
        self,
        token: str,
        admin: int,
        *,
        queue_size: int = 100,
        dedupe_window: float = 60.0,
        rate_limit: int = 10,
        rate_window: float = 60.0,
        request_timeout: tuple[float, float] = (2.0, 5.0),
        redactions: Iterable[str] = (),
        proxy_url: str | None = None,
        shutdown_timeout: float = 1.0,
    ) -> None:
        super().__init__()
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        if rate_limit < 1:
            raise ValueError("rate_limit must be positive")

        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.admin = admin
        self.setFormatter(CustomFormatter())

        self._queue: queue.Queue[dict | object] = queue.Queue(maxsize=queue_size)
        self._dedupe_window = max(0.0, dedupe_window)
        self._rate_limit = rate_limit
        self._rate_window = max(0.0, rate_window)
        self._request_timeout = request_timeout
        self._proxy_url = proxy_url or None
        self._proxies = (
            {"http": self._proxy_url, "https": self._proxy_url}
            if self._proxy_url
            else None
        )
        # Длинные секреты заменяем первыми: иначе короткий секрет-префикс
        # может оставить хвост более длинного значения в уведомлении.
        self._redactions = tuple(sorted(
            {value for value in (token, self._proxy_url, *redactions) if value},
            key=len,
            reverse=True,
        ))
        self._shutdown_timeout = max(0.0, shutdown_timeout)
        self._recent: dict[str, float] = {}
        self._sent_timestamps: deque[float] = deque()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._closed = False
        self._dropped_notifications = 0
        self._failed_notifications = 0

        self._worker = threading.Thread(
            target=self._worker_loop,
            name="telegram-log-notifier",
            daemon=True,
        )
        self._worker.start()

    @property
    def dropped_notifications(self) -> int:
        with self._state_lock:
            return self._dropped_notifications

    @property
    def failed_notifications(self) -> int:
        with self._state_lock:
            return self._failed_notifications

    def emit(self, record: logging.LogRecord) -> None:
        """Быстро ставит уведомление в очередь и никогда не бросает ошибку."""
        try:
            now = time.monotonic()
            fingerprint = self._fingerprint(record)
            if not self._admit(fingerprint, now):
                return

            log_entry = self.format(record)
            log_entry = log_entry.replace("[", "\n[")
            log_entry = log_entry.replace("]", "]\n")
            log_entry = log_entry.replace("__ -", "__ -\n")
            for secret in self._redactions:
                log_entry = log_entry.replace(secret, "[REDACTED]")
            # Telegram ограничивает сообщение 4096 UTF-16 символами после
            # разбора entities. Дополнительно ограничиваем длину wire-текста,
            # чтобы большое число символов <, > и & не раздуло HTML.
            safe_entry = _escape_telegram_code(log_entry)
            payload = {
                "chat_id": self.admin,
                "text": f"<code>{safe_entry}</code>",
                "parse_mode": "HTML",
            }
            self._queue.put_nowait(payload)
        except queue.Full:
            self._mark_dropped()
        except Exception:
            # Ошибка канала уведомлений не должна менять выполнение приложения
            # и тем более маскировать исходную ошибку.
            self._mark_dropped()

    def close(self) -> None:
        self.shutdown(drain=False)
        super().close()

    def shutdown(
        self,
        *,
        drain: bool = False,
        timeout: float | None = None,
    ) -> bool:
        """Останавливает worker за ограниченное время.

        По умолчанию хвост очереди отбрасывается: shutdown приложения не
        должен ждать пачку устаревших алертов. Уже начатый HTTP-запрос имеет
        собственный timeout, а daemon-thread не удерживает процесс.
        """
        with self._state_lock:
            if self._closed:
                return not self._worker.is_alive()
            self._closed = True

        if not drain:
            self._discard_pending()

        self._stop_event.set()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            pass

        if self._worker.is_alive() and self._worker is not threading.current_thread():
            self._worker.join(
                timeout=self._shutdown_timeout if timeout is None else max(0.0, timeout)
            )
        return not self._worker.is_alive()

    def _fingerprint(self, record: logging.LogRecord) -> str:
        exc_type = record.exc_info[0].__name__ if record.exc_info else ""
        return f"{record.levelno}:{record.name}:{exc_type}:{record.getMessage()}"

    def _admit(self, fingerprint: str, now: float) -> bool:
        with self._state_lock:
            if self._closed:
                self._dropped_notifications += 1
                return False

            dedupe_cutoff = now - self._dedupe_window
            self._recent = {
                key: timestamp
                for key, timestamp in self._recent.items()
                if timestamp > dedupe_cutoff
            }
            if fingerprint in self._recent:
                self._dropped_notifications += 1
                return False

            rate_cutoff = now - self._rate_window
            while self._sent_timestamps and self._sent_timestamps[0] <= rate_cutoff:
                self._sent_timestamps.popleft()
            if len(self._sent_timestamps) >= self._rate_limit:
                self._dropped_notifications += 1
                return False

            self._recent[fingerprint] = now
            self._sent_timestamps.append(now)
            return True

    def _mark_dropped(self) -> None:
        with self._state_lock:
            self._dropped_notifications += 1

    def _discard_pending(self) -> None:
        discarded = 0
        while True:
            try:
                payload = self._queue.get_nowait()
            except queue.Empty:
                break
            else:
                self._queue.task_done()
                if payload is not _STOP:
                    discarded += 1
        if discarded:
            with self._state_lock:
                self._dropped_notifications += discarded

    def _worker_loop(self) -> None:
        while True:
            try:
                payload = self._queue.get(timeout=0.25)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue

            try:
                if payload is _STOP:
                    return
                try:
                    response = requests.post(
                        self.url,
                        json=payload,
                        timeout=self._request_timeout,
                        proxies=self._proxies,
                    )
                    response.raise_for_status()
                except Exception:
                    # Нельзя логировать через logging отсюда: это создаст
                    # рекурсию этого же handler при недоступности Telegram.
                    with self._state_lock:
                        self._failed_notifications += 1
            finally:
                self._queue.task_done()
