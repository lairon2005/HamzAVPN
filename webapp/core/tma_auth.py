# webapp/core/tma_auth.py
"""
Валидация Telegram WebApp initData (Mini App авторизация, README.md фаза 1).

Алгоритм из официальной доки Telegram
(https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):
  1. secret_key = HMAC_SHA256(key=b"WebAppData", msg=bot_token)
  2. data_check_string — все пары `key=value` initData (кроме `hash`), значения
     URL-декодированы, пары отсортированы по ключу и соединены через `\n`
  3. ожидаемый hash = HMAC_SHA256(key=secret_key, msg=data_check_string).hexdigest()
  4. сравнить с присланным `hash` через hmac.compare_digest (защита от timing-атак)

Только stdlib — без новых pip-зависимостей.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InvalidInitDataError(Exception):
    """initData не прошла проверку подлинности, свежести или структуры."""


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 3600) -> dict:
    """
    Проверяет подпись `init_data` и возвращает распарсенные данные.

    Возвращает dict:
      - user: dict — распарсенный JSON из поля `user` (id, first_name, username, ...)
      - start_param: str | None — параметр `?startapp=` (реферальный ID и т.п.)
      - auth_date: int — unix-timestamp, когда Telegram подписал initData

    Кидает InvalidInitDataError, если: initData пустая, hash отсутствует/не совпадает,
    auth_date отсутствует/просрочен (> max_age_seconds), поле user отсутствует
    или не парсится как JSON.
    """
    if not init_data:
        raise InvalidInitDataError("initData is empty")

    # parse_qsl сам URL-декодирует значения; keep_blank_values — на случай
    # пустых полей вида start_param= (Telegram присылает их без значения).
    pairs = parse_qsl(init_data, keep_blank_values=True)

    received_hash = None
    data: dict[str, str] = {}
    for key, value in pairs:
        if key == "hash":
            received_hash = value
        else:
            data[key] = value

    if not received_hash:
        raise InvalidInitDataError("hash is missing")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))

    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise InvalidInitDataError("hash mismatch")

    auth_date_raw = data.get("auth_date")
    if not auth_date_raw:
        raise InvalidInitDataError("auth_date is missing")
    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        raise InvalidInitDataError("auth_date is not a valid timestamp")

    # Защита от replay-атак: initData старше max_age_seconds считаем недействительной.
    if time.time() - auth_date > max_age_seconds:
        raise InvalidInitDataError("initData is expired")

    user_raw = data.get("user")
    if not user_raw:
        raise InvalidInitDataError("user is missing")
    try:
        user = json.loads(user_raw)
    except (ValueError, TypeError) as e:
        raise InvalidInitDataError("user is not valid JSON") from e

    return {
        "user": user,
        "start_param": data.get("start_param"),
        "auth_date": auth_date,
    }
