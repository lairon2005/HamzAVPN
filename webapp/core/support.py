# webapp/core/support.py
"""
Валидация и rate-limit для формы обращения в поддержку Telegram Mini App
(README.md фаза 4, webapp/routers/tma.py:: POST /tma/support).

Модуль без внешних зависимостей (stdlib only) — по тому же принципу, что и
webapp/core/tma_auth.py: чистая логика отдельно от FastAPI-роутера, чтобы её
можно было unit-тестировать без поднятия всего приложения (config.load_config(),
loader.bot и остальных сервисных синглтонов).
"""
import html
from datetime import datetime

SUPPORT_MESSAGE_MAX_LEN = 2000
SUPPORT_RATE_LIMIT_SECONDS = 30


class SupportValidationError(Exception):
    """Ошибка валидации/rate-limit обращения в поддержку.

    `status_code` соответствует тому, что роутер отдаёт через HTTPException
    (400 — невалидный текст, 429 — rate-limit).
    """

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def validate_support_text(raw: str) -> str:
    """Непустой текст обращения, не длиннее SUPPORT_MESSAGE_MAX_LEN символов."""
    text = (raw or "").strip()
    if not text:
        raise SupportValidationError(400, "Сообщение не может быть пустым")
    if len(text) > SUPPORT_MESSAGE_MAX_LEN:
        raise SupportValidationError(
            400, f"Слишком длинное сообщение (максимум {SUPPORT_MESSAGE_MAX_LEN} символов)"
        )
    return text


def check_rate_limit(last_sent_at: dict, user_id: int, now: datetime | None = None) -> None:
    """Не чаще одного сообщения в SUPPORT_RATE_LIMIT_SECONDS на пользователя.

    `last_sent_at` — общий in-memory словарь {user_id: datetime}, который хранит
    вызывающая сторона (простейший rate-limit, достаточный для одного процесса
    vpn_site — не переживает рестарт, но это ОК для защиты от повторных
    сабмитов формы). Отметку ставим сразу при успешном проходе проверки —
    иначе пользователь мог бы засыпать бота повторными запросами, пока каждый
    предыдущий ещё обрабатывается.
    """
    now = now or datetime.now()
    last_sent = last_sent_at.get(user_id)
    if last_sent and (now - last_sent).total_seconds() < SUPPORT_RATE_LIMIT_SECONDS:
        raise SupportValidationError(
            429, "Пожалуйста, подождите немного перед отправкой следующего сообщения"
        )
    last_sent_at[user_id] = now


def format_support_message(full_name: str, user_id: int, text: str) -> str:
    """Сообщение для топика поддержки с экранированием пользовательских данных.

    Бот работает с parse_mode=HTML по умолчанию (loader.py, DefaultBotProperties),
    поэтому и текст обращения, и отображаемое имя (оба задаются пользователем)
    ОБЯЗАТЕЛЬНО экранируются. Иначе:
      - любой символ `<` в обращении ломает разбор entities — Telegram отвечает
        400 "can't parse entities", и обращение теряется;
      - пользователь может подделать разметку/ссылки в топике администратора.

    В боте этой проблемы нет: tgbot/handlers/support.py пересылает сообщение
    через message.forward(), а не подставляет его в HTML-шаблон.

    quote=False — экранируем только `&`, `<`, `>`; кавычки в тексте безопасны и
    не должны превращаться в &quot; при чтении админом.
    """
    safe_name = html.escape(full_name or "", quote=False)
    safe_text = html.escape(text, quote=False)
    return (
        f"👤 <b>{safe_name}</b> (ID: <code>{user_id}</code>) [из Mini App]:\n\n{safe_text}"
    )


def format_new_ticket_message(full_name: str, user_id: int) -> str:
    """Первое сообщение при создании топика (имя тоже экранируется, см. выше)."""
    safe_name = html.escape(full_name or "", quote=False)
    return (
        f"👤 Пользователь <b>{safe_name}</b> (ID: <code>{user_id}</code>) "
        "открыл новый тикет (из Mini App)."
    )
