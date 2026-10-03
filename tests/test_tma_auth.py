# tests/test_tma_auth.py
"""
Валидация Telegram WebApp initData (README.md фаза 1). Тесты сами собирают
initData тем же HMAC-алгоритмом, что и validate_init_data, поэтому работают без
сети и без БД — модуль webapp/core/tma_auth.py использует только stdlib.
"""
import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from webapp.core.tma_auth import InvalidInitDataError, validate_init_data

BOT_TOKEN = "123456:TEST-BOT-TOKEN-FOR-UNIT-TESTS"


def _sign(params: dict, bot_token: str = BOT_TOKEN) -> str:
    """Считает hash для набора полей (без самого hash) — тот же алгоритм, что и в проде."""
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()


def _build_init_data(params: dict, bot_token: str = BOT_TOKEN, include_hash: bool = True) -> str:
    query = dict(params)
    if include_hash:
        query["hash"] = _sign(params, bot_token)
    return urlencode(query)


def _base_params(auth_date: int | None = None, start_param: str | None = None) -> dict:
    user = {"id": 555, "first_name": "Иван", "last_name": "Петров", "username": "ivan"}
    params = {
        "user": json.dumps(user, ensure_ascii=False),
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
    }
    if start_param is not None:
        params["start_param"] = start_param
    return params


class ValidateInitDataTests(unittest.TestCase):
    def test_valid_init_data_is_parsed(self):
        params = _base_params()
        init_data = _build_init_data(params)

        result = validate_init_data(init_data, BOT_TOKEN)

        self.assertEqual(result["user"]["id"], 555)
        self.assertEqual(result["user"]["first_name"], "Иван")
        self.assertEqual(result["user"]["username"], "ivan")
        self.assertEqual(result["auth_date"], int(params["auth_date"]))

    def test_tampered_field_is_rejected(self):
        # Подпись остаётся от исходных данных, но user.id меняем уже после подписи —
        # как будто атакующий подменил initData, не имея bot_token для пересчёта hash.
        params = _base_params()
        original_hash = _sign(params)

        tampered_user = json.loads(params["user"])
        tampered_user["id"] = 999
        tampered_params = dict(params)
        tampered_params["user"] = json.dumps(tampered_user, ensure_ascii=False)
        tampered_params["hash"] = original_hash

        init_data = urlencode(tampered_params)

        with self.assertRaises(InvalidInitDataError):
            validate_init_data(init_data, BOT_TOKEN)

    def test_forged_hash_value_is_rejected(self):
        params = _base_params()
        init_data = _build_init_data(params, include_hash=False)
        init_data += "&hash=" + "0" * 64

        with self.assertRaises(InvalidInitDataError):
            validate_init_data(init_data, BOT_TOKEN)

    def test_expired_auth_date_is_rejected(self):
        old_auth_date = int(time.time()) - 7200  # 2 часа назад — за пределами часового окна
        params = _base_params(auth_date=old_auth_date)
        init_data = _build_init_data(params)

        with self.assertRaises(InvalidInitDataError):
            validate_init_data(init_data, BOT_TOKEN, max_age_seconds=3600)

    def test_start_param_is_extracted(self):
        params = _base_params(start_param="42")
        init_data = _build_init_data(params)

        result = validate_init_data(init_data, BOT_TOKEN)

        self.assertEqual(result["start_param"], "42")

    def test_missing_start_param_is_none(self):
        params = _base_params()
        init_data = _build_init_data(params)

        result = validate_init_data(init_data, BOT_TOKEN)

        self.assertIsNone(result["start_param"])

    def test_missing_hash_is_rejected(self):
        params = _base_params()
        init_data = urlencode(params)  # без поля hash вообще

        with self.assertRaises(InvalidInitDataError):
            validate_init_data(init_data, BOT_TOKEN)

    def test_empty_init_data_is_rejected(self):
        with self.assertRaises(InvalidInitDataError):
            validate_init_data("", BOT_TOKEN)


if __name__ == "__main__":
    unittest.main()
