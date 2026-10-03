# tests/test_promo_code_service.py
"""
PromoCodeService: захват промокода (try_claim) и порядок операций в
apply()/apply_bonus_days().

Исходно apply_bonus_days() делала extend() (2-3 HTTP-вызова в Remnawave), и
только ПОТОМ помечала промокод использованным — чтобы при сбое панели
(инцидент 2026-07-23: ConnectTimeout во время рассылки LOVEHamzAVPN) промокод не
«сгорал» без начисления дней. Но это открывало TOCTOU-гонку: между
validate() (проверяет has_user_used) и записью использования оставалось окно
в сетевую задержку, которое ThrottlingMiddleware (L1=0.5s) не закрывает, а
aiogram обрабатывает конкурентные апдейты отдельными тасками — повторное
нажатие той же кнопки промокода из рассылки успевало проскочить (security
review 2026-07-26).

Новый порядок: сначала атомарный try_claim() (guard на uses_left и уникальный
индекс на used_promo_codes — см. database/repositories/promo_code.py и
db.py::UsedPromoCode), потом extend(); при сбое extend() — компенсирующий
release_claim(). Тесты ниже проверяют этот порядок и раздельно — сам контракт
атомарности try_claim() под гонкой (см. TryClaimRaceTests).
"""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock


def load_promo_code_service_module():
    """Загружает сервис без запуска production-синглтонов из loader/db."""
    db_module = types.ModuleType("db")
    db_module.PromoCode = type("PromoCode", (), {})

    database_module = types.ModuleType("database")
    database_module.__path__ = []
    repositories_module = types.ModuleType("database.repositories")
    repositories_module.__path__ = []
    promo_repository_module = types.ModuleType("database.repositories.promo_code")
    promo_repository_module.PromoCodeRepository = type("PromoCodeRepository", (), {})
    user_repository_module = types.ModuleType("database.repositories.user")
    user_repository_module.UserRepository = type("UserRepository", (), {})

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []
    tgbot_promo_module = types.ModuleType("tgbot.promo")
    tgbot_promo_module.get_promo_reward = lambda promo: None

    module_name = "promo_code_service_under_test"
    module_path = (
        Path(__file__).resolve().parents[1]
        / "tgbot"
        / "services"
        / "promo_code_service.py"
    )

    stubs = {
        "db": db_module,
        "database": database_module,
        "database.repositories": repositories_module,
        "database.repositories.promo_code": promo_repository_module,
        "database.repositories.user": user_repository_module,
        "tgbot": tgbot_module,
        "tgbot.promo": tgbot_promo_module,
    }
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


promo_code_service = load_promo_code_service_module()
PromoClaimError = promo_code_service.PromoClaimError


class FakePromo:
    id = 1
    code = "TESTCODE"
    bonus_days = 5
    discount_percent = 0


# =============================================================================
# --- PromoCodeService.apply_bonus_days: claim -> extend -> release-on-fail ---
# =============================================================================

class ApplyBonusDaysOrderingTests(unittest.TestCase):
    def setUp(self):
        self.promo_repo = AsyncMock()
        self.user_repo = AsyncMock()
        self.service = promo_code_service.PromoCodeService(self.promo_repo, self.user_repo)
        self.subscription_service = AsyncMock()

        # Порядок вызовов try_claim -> extend отслеживаем через общий менеджер,
        # т.к. это два разных мока и assert_called_once сам по себе порядок не видит.
        self.call_order = Mock()
        self.call_order.attach_mock(self.promo_repo.try_claim, "try_claim")
        self.call_order.attach_mock(self.subscription_service.extend, "extend")
        self.call_order.attach_mock(self.promo_repo.release_claim, "release_claim")

    def test_claim_failure_raises_without_calling_extend(self):
        """Гонка проиграна / промокод исчерпан -> extend() вообще не вызывается."""
        self.promo_repo.try_claim.return_value = False
        promo = FakePromo()

        with self.assertRaises(PromoClaimError):
            asyncio.run(
                self.service.apply_bonus_days(123, promo, self.subscription_service)
            )

        self.promo_repo.try_claim.assert_awaited_once_with(123, promo)
        self.subscription_service.extend.assert_not_awaited()
        self.promo_repo.release_claim.assert_not_awaited()

    def test_extend_failure_releases_claim_and_reraises(self):
        """Claim прошёл, но extend() упал (например, Remnawave недоступен) ->
        release_claim() компенсирует захват, и исходное исключение пробрасывается
        дальше не замаскированным."""
        self.promo_repo.try_claim.return_value = True
        self.subscription_service.extend.side_effect = RuntimeError("panel down")

        with self.assertRaises(RuntimeError):
            asyncio.run(
                self.service.apply_bonus_days(123, FakePromo(), self.subscription_service)
            )

        self.promo_repo.try_claim.assert_awaited_once()
        self.subscription_service.extend.assert_awaited_once_with(123, 5)
        self.promo_repo.release_claim.assert_awaited_once()
        release_args = self.promo_repo.release_claim.await_args.args
        self.assertEqual(release_args[0], 123)

    def test_success_claims_then_extends_without_releasing(self):
        self.promo_repo.try_claim.return_value = True
        self.subscription_service.extend.return_value = "ok"

        result = asyncio.run(
            self.service.apply_bonus_days(123, FakePromo(), self.subscription_service)
        )

        self.assertEqual(result, "ok")
        self.promo_repo.try_claim.assert_awaited_once()
        self.subscription_service.extend.assert_awaited_once_with(123, 5)
        self.promo_repo.release_claim.assert_not_awaited()

        # try_claim должен произойти строго ДО extend (не наоборот, как было раньше).
        method_order = [c[0] for c in self.call_order.mock_calls]
        self.assertLess(method_order.index("try_claim"), method_order.index("extend"))


# =============================================================================
# --- PromoCodeService.apply(): скидочный путь и ручной ввод бонусного кода ---
# =============================================================================

class ApplyDiscountClaimTests(unittest.TestCase):
    def setUp(self):
        self.promo_repo = AsyncMock()
        self.user_repo = AsyncMock()
        self.service = promo_code_service.PromoCodeService(self.promo_repo, self.user_repo)

    def test_apply_raises_promo_claim_error_when_claim_fails(self):
        self.promo_repo.try_claim.return_value = False

        with self.assertRaises(PromoClaimError):
            asyncio.run(self.service.apply(123, FakePromo()))

    def test_apply_succeeds_silently_when_claim_succeeds(self):
        self.promo_repo.try_claim.return_value = True
        promo = FakePromo()

        # Не должно бросать исключений.
        asyncio.run(self.service.apply(123, promo))

        self.promo_repo.try_claim.assert_awaited_once_with(123, promo)


# =============================================================================
# --- Контракт атомарности try_claim() под гонкой ---
#
# Реальной Postgres здесь нет (см. ограничения задачи), поэтому гонку
# проверяем не на database/repositories/promo_code.py::try_claim (там
# атомарность обеспечивает Postgres — row-level locking внутри UPDATE ...
# RETURNING + INSERT ... ON CONFLICT DO NOTHING в одной транзакции), а на
# двойнике, воспроизводящем ТОЧНО ТОТ ЖЕ контракт (guard по uses_left +
# уникальность (user_id, promo_id)) поверх asyncio.Lock. Это фиксирует
# гарантию, на которую полагается PromoCodeService: try_claim() должен
# пропустить ровно одного победителя гонки.
# =============================================================================

class FakeAtomicPromoRepo:
    def __init__(self, uses_left: int):
        self._uses_left = uses_left
        self._used: set[tuple[int, int]] = set()
        self._lock = asyncio.Lock()

    async def try_claim(self, user_id: int, promo) -> bool:
        async with self._lock:
            key = (user_id, promo.id)
            if self._uses_left <= 0 or key in self._used:
                return False
            # Имитируем сетевую задержку похода в БД между проверкой guard'ов
            # и коммитом — ровно та точка, где в исходном баге проходила гонка.
            await asyncio.sleep(0)
            self._uses_left -= 1
            self._used.add(key)
            return True

    async def release_claim(self, user_id: int, promo) -> None:
        async with self._lock:
            key = (user_id, promo.id)
            if key in self._used:
                self._used.discard(key)
                self._uses_left += 1


class TryClaimRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_concurrent_claims_exactly_one_succeeds(self):
        repo = FakeAtomicPromoRepo(uses_left=1)
        promo = FakePromo()

        results = await asyncio.gather(
            repo.try_claim(123, promo),
            repo.try_claim(123, promo),
        )

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)

    async def test_different_users_do_not_block_each_other(self):
        repo = FakeAtomicPromoRepo(uses_left=5)
        promo = FakePromo()

        results = await asyncio.gather(
            repo.try_claim(123, promo),
            repo.try_claim(456, promo),
        )

        self.assertEqual(results, [True, True])

    async def test_release_claim_lets_the_same_user_retry(self):
        """Симулирует apply_bonus_days: claim успешен, extend() падает,
        release_claim() возвращает uses_left и снимает отметку -> повторная
        попытка того же пользователя снова успешна (не «сгорело» при сбое)."""
        repo = FakeAtomicPromoRepo(uses_left=1)
        promo = FakePromo()

        first = await repo.try_claim(123, promo)
        self.assertTrue(first)

        await repo.release_claim(123, promo)

        second = await repo.try_claim(123, promo)
        self.assertTrue(second)


if __name__ == "__main__":
    unittest.main()
