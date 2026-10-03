# tests/test_payment_repository.py
"""
Юнит-тесты чистой функции _msk_period_start (без обращения к БД).

Расчёт дохода по календарным периодам (день/неделя/месяц/год) должен опираться
на границы московских суток, а не на серверное локальное время или скользящее
окно. Payment.completed_at хранится как наивный datetime в UTC (дефолт Docker-
контейнера — TZ не задан), поэтому helper переводит МСК-границу обратно в
наивный UTC перед сравнением с БД.
"""
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path


def load_payment_repository_module():
    """Загружает database/repositories/payment.py без запуска db.py
    (который требует REMNAWAVE_API_URL и прочие env-переменные)."""
    db_module = types.ModuleType("db")
    db_module.Payment = type("Payment", (), {})
    db_module.User = type("User", (), {})
    db_module.Tariff = type("Tariff", (), {})

    module_name = "payment_repository_under_test"
    module_path = (
        Path(__file__).resolve().parents[1]
        / "database"
        / "repositories"
        / "payment.py"
    )

    stubs = {"db": db_module}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
    return module


payment_repository = load_payment_repository_module()
_msk_period_start = payment_repository._msk_period_start


# Пятница, 2026-07-24 09:00 UTC == 2026-07-24 12:00 МСК (MSK = UTC+3)
NOW_UTC = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)


class TestMskPeriodStart(unittest.TestCase):
    def test_day_start(self):
        result = _msk_period_start("day", NOW_UTC)
        self.assertEqual(result, datetime(2026, 7, 23, 21, 0))
        self.assertIsNone(result.tzinfo)

    def test_week_start(self):
        # Понедельник этой недели — 2026-07-20, 00:00 МСК = 21:00 UTC 19 июля.
        result = _msk_period_start("week", NOW_UTC)
        self.assertEqual(result, datetime(2026, 7, 19, 21, 0))
        self.assertIsNone(result.tzinfo)

    def test_month_start(self):
        # Граничный кейс: МСК-дата (1 июля) отличается от UTC-даты (30 июня).
        result = _msk_period_start("month", NOW_UTC)
        self.assertEqual(result, datetime(2026, 6, 30, 21, 0))
        self.assertIsNone(result.tzinfo)

    def test_year_start(self):
        result = _msk_period_start("year", NOW_UTC)
        self.assertEqual(result, datetime(2025, 12, 31, 21, 0))
        self.assertIsNone(result.tzinfo)

    def test_invalid_kind_raises(self):
        with self.assertRaises(ValueError):
            _msk_period_start("century", NOW_UTC)

    def test_default_now_utc_uses_current_time(self):
        # Без явного now_utc функция не должна падать и должна вернуть наивный datetime.
        result = _msk_period_start("day")
        self.assertIsNone(result.tzinfo)


if __name__ == "__main__":
    unittest.main()
