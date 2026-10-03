# tests/test_stale_payment_cancel.py
"""
Автоотмена зависших неоплаченных счетов.

До этого джоба локальной автоотмены не было вообще: статус 'pending' снимал
ТОЛЬКО вебхук `payment.canceled` от Platega, а PaymentRepository.
get_pending_older_than не вызывался ниоткуда. Не дошёл вебхук (магазин не
подписан на событие, сеть, зависание VM) — счёт висит вечно, и
has_pending_payment не даёт человеку выставить новый ни на подписку, ни на
докупку устройств.

Что здесь проверяется:
  * сервис берёт именно настроенный таймаут и помечает счета 'cancelled';
  * отметка условная (WHERE status='pending') — иначе гонка с вебхуком оплаты
    затёрла бы 'succeeded' и платёж выпал бы из выручки;
  * ошибка на одном счёте не срывает прогон;
  * автоотмена НЕ мешает оплате по старой ссылке — process_successful_payment
    обязан обработать платёж со статусом 'cancelled' (см. payment_service.py);
  * джоб реально зарегистрирован в schedule_jobs — исходная болезнь была
    именно в том, что рабочий код ниоткуда не вызывался.

Модули грузятся через importlib мимо пакетных __init__.py — они на импорте
тянут loader.py -> config.load_config(), которому в тестовом окружении не
хватает REMNAWAVE_API_URL и т.п. (тот же приём, что в test_stars_payment.py).
"""
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_stars_payment import _load_module, load_payment_service_module
from test_payment_repository import load_payment_repository_module


def _payment(**overrides):
    defaults = {
        "id": 1,
        "provider_payment_id": "yk-1",
        "user_id": 42,
        "tariff_id": 5,
        "final_amount": 399.0,
        "status": "pending",
        "kind": "subscription",
        "extra_devices": 0,
        "source": "bot",
        "discount_percent": 0,
        "promo_code": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _service(module, payment_repo):
    """PaymentService с одним настоящим участником — payment_repo."""
    return module.PaymentService(
        subscription_service=SimpleNamespace(
            extend=AsyncMock(return_value=SimpleNamespace(is_new_user=False, username="user_42"))
        ),
        referral_service=SimpleNamespace(process_first_payment_bonus=AsyncMock(return_value=None)),
        user_repo=SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(user_id=42, is_first_payment_made=True)),
            set_first_payment_done=AsyncMock(),
        ),
        tariff_repo=SimpleNamespace(get_by_id=AsyncMock(return_value=SimpleNamespace(
            id=5, name="Три месяца", duration_days=90, data_limit_gb=None,
        ))),
        payment_repo=payment_repo,
    )


# =============================================================================
# --- PaymentService.cancel_stale_payments ---
# =============================================================================

class CancelStalePaymentsTests(unittest.IsolatedAsyncioTestCase):
    async def test_marks_every_stale_payment_cancelled(self):
        module = load_payment_service_module()
        stale = [_payment(provider_payment_id="yk-1"), _payment(provider_payment_id="yk-2")]
        payment_repo = SimpleNamespace(
            get_pending_older_than=AsyncMock(return_value=stale),
            cancel_if_pending=AsyncMock(return_value=True),
        )
        service = _service(module, payment_repo)

        cancelled = await service.cancel_stale_payments(60)

        payment_repo.get_pending_older_than.assert_awaited_once_with(60)
        self.assertEqual([p.provider_payment_id for p in cancelled], ["yk-1", "yk-2"])
        self.assertEqual(
            [c.args[0] for c in payment_repo.cancel_if_pending.await_args_list],
            ["yk-1", "yk-2"],
        )

    async def test_timeout_is_passed_through_not_hardcoded(self):
        module = load_payment_service_module()
        payment_repo = SimpleNamespace(
            get_pending_older_than=AsyncMock(return_value=[]),
            cancel_if_pending=AsyncMock(return_value=True),
        )
        service = _service(module, payment_repo)

        await service.cancel_stale_payments(15)

        payment_repo.get_pending_older_than.assert_awaited_once_with(15)

    async def test_nothing_stale_writes_nothing(self):
        module = load_payment_service_module()
        payment_repo = SimpleNamespace(
            get_pending_older_than=AsyncMock(return_value=[]),
            cancel_if_pending=AsyncMock(return_value=True),
        )
        service = _service(module, payment_repo)

        self.assertEqual(await service.cancel_stale_payments(60), [])
        payment_repo.cancel_if_pending.assert_not_awaited()

    async def test_payment_paid_in_the_meantime_is_not_reported_cancelled(self):
        """cancel_if_pending вернул False — строку уже перевёл вебхук оплаты."""
        module = load_payment_service_module()
        stale = [_payment(provider_payment_id="yk-1"), _payment(provider_payment_id="yk-2")]
        payment_repo = SimpleNamespace(
            get_pending_older_than=AsyncMock(return_value=stale),
            cancel_if_pending=AsyncMock(side_effect=[False, True]),
        )
        service = _service(module, payment_repo)

        cancelled = await service.cancel_stale_payments(60)

        self.assertEqual([p.provider_payment_id for p in cancelled], ["yk-2"])

    async def test_one_failure_does_not_abort_the_run(self):
        module = load_payment_service_module()
        stale = [_payment(provider_payment_id="yk-1"), _payment(provider_payment_id="yk-2")]
        payment_repo = SimpleNamespace(
            get_pending_older_than=AsyncMock(return_value=stale),
            cancel_if_pending=AsyncMock(side_effect=[RuntimeError("db down"), True]),
        )
        service = _service(module, payment_repo)

        cancelled = await service.cancel_stale_payments(60)

        self.assertEqual([p.provider_payment_id for p in cancelled], ["yk-2"])
        self.assertEqual(payment_repo.cancel_if_pending.await_count, 2)

# =============================================================================
# --- PaymentRepository.cancel_if_pending: условие в WHERE ---
# =============================================================================

def _mapped_payment_class():
    """Настоящая маппленная модель — заглушка-`type` не переживёт update()."""
    from sqlalchemy import Column, DateTime, Integer, String
    from sqlalchemy.orm import declarative_base

    Base = declarative_base()

    class Payment(Base):
        __tablename__ = "payments"
        id = Column(Integer, primary_key=True)
        provider_payment_id = Column(String)
        status = Column(String)
        completed_at = Column(DateTime)

    return Payment


class _FakeSession:
    def __init__(self, captured, rowcount):
        self._captured = captured
        self._rowcount = rowcount
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, stmt):
        self._captured.append(stmt)
        return SimpleNamespace(rowcount=self._rowcount)

    async def commit(self):
        self.committed = True


def _repo(rowcount=1):
    module = load_payment_repository_module()
    module.Payment = _mapped_payment_class()
    captured = []
    sessions = []

    def session_maker():
        session = _FakeSession(captured, rowcount)
        sessions.append(session)
        return session

    return module.PaymentRepository(session_maker), captured, sessions


class CancelIfPendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_is_guarded_by_pending_status(self):
        repo, captured, sessions = _repo()

        self.assertTrue(await repo.cancel_if_pending("yk-1"))

        params = captured[0].compile().params
        self.assertEqual(params["status"], "cancelled")
        self.assertIsNone(params["completed_at"])
        # Гонка с вебхуком оплаты: без этого условия безусловный UPDATE затёр бы
        # уже проставленный 'succeeded' и платёж исчез бы из выручки.
        self.assertIn("pending", params.values())
        self.assertIn("yk-1", params.values())
        self.assertTrue(sessions[0].committed)

    async def test_returns_false_when_row_already_moved_on(self):
        repo, _, _ = _repo(rowcount=0)

        self.assertFalse(await repo.cancel_if_pending("yk-1"))


# =============================================================================
# --- Проводка джоба в планировщик ---
# =============================================================================

def _scheduler_stubs():
    database_module = types.ModuleType("database")
    for name in ("user_repo", "tariff_repo", "payment_method_repo", "lifecycle_repo",
                 "stats_repo", "channel_repo"):
        setattr(database_module, name, AsyncMock())

    keyboards_module = types.ModuleType("tgbot.keyboards.inline")
    for name in ("tariffs_keyboard", "lifecycle_cta_keyboard", "winback_survey_keyboard",
                 "tma_web_app_button", "tma_mode_enabled"):
        setattr(keyboards_module, name, Mock())
    keyboards_pkg = types.ModuleType("tgbot.keyboards")
    keyboards_pkg.__path__ = []

    utils_module = types.ModuleType("utils")
    utils_module.__path__ = []
    utils_module.broadcaster = AsyncMock()

    services_utils_module = types.ModuleType("tgbot.services.utils")
    services_utils_module.decline_word = Mock(return_value="дней")

    loader_module = types.ModuleType("loader")
    loader_module.logger = Mock()
    loader_module.config = SimpleNamespace(tg_bot=SimpleNamespace(admin_ids=[1]))

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []
    tgbot_services_module = types.ModuleType("tgbot.services")
    tgbot_services_module.__path__ = []
    tgbot_services_module.payment_service = AsyncMock()

    return {
        "database": database_module,
        "tgbot": tgbot_module,
        "tgbot.keyboards": keyboards_pkg,
        "tgbot.keyboards.inline": keyboards_module,
        "tgbot.services": tgbot_services_module,
        "tgbot.services.utils": services_utils_module,
        "utils": utils_module,
        "loader": loader_module,
    }


def load_scheduler_module():
    stubs = _scheduler_stubs()
    module = _load_module("tgbot.services.scheduler", "tgbot/services/scheduler.py", stubs)
    return module, stubs


class SchedulerWiringTests(unittest.IsolatedAsyncioTestCase):
    def test_job_is_registered(self):
        module, _ = load_scheduler_module()
        scheduler = Mock()

        module.schedule_jobs(scheduler, Mock())

        registered = [call.args[0] for call in scheduler.add_job.call_args_list]
        self.assertIn(module.cancel_stale_payments, registered)

    def test_default_timeout_is_60_minutes(self):
        """Тексты в боте и вебе обещают отмену «через 30 минут» — локальный
        таймаут держим с запасом, чтобы не опережать саму Platega."""
        module, _ = load_scheduler_module()
        self.assertEqual(module.PENDING_PAYMENT_TTL_MINUTES, 60)

    async def test_job_calls_service_with_configured_timeout(self):
        module, stubs = load_scheduler_module()
        payment_service = stubs["tgbot.services"].payment_service
        payment_service.cancel_stale_payments = AsyncMock(return_value=[_payment()])

        with patch.dict(sys.modules, stubs):
            await module.cancel_stale_payments()

        payment_service.cancel_stale_payments.assert_awaited_once_with(
            module.PENDING_PAYMENT_TTL_MINUTES
        )

    async def test_job_survives_service_failure(self):
        """Падение прогона не должно валить APScheduler-джоб."""
        module, stubs = load_scheduler_module()
        payment_service = stubs["tgbot.services"].payment_service
        payment_service.cancel_stale_payments = AsyncMock(side_effect=RuntimeError("db down"))

        with patch.dict(sys.modules, stubs):
            await module.cancel_stale_payments()


if __name__ == "__main__":
    unittest.main()
