import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from remnawave.client import RemnawaveTransportError


def _real_device_pricing():
    """Настоящий device_pricing: арифметика цен и есть предмет проверки."""
    path = Path(__file__).resolve().parents[1] / "tgbot" / "services" / "device_pricing.py"
    spec = importlib.util.spec_from_file_location("tgbot.services.device_pricing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_device_slot_service_module():
    """Загружает сервис без запуска production-синглтонов из loader."""
    logger = Mock()

    database_module = types.ModuleType("database")
    database_module.__path__ = []
    repositories_module = types.ModuleType("database.repositories")
    repositories_module.__path__ = []
    user_repository_module = types.ModuleType("database.repositories.user")
    user_repository_module.UserRepository = type("UserRepository", (), {})
    settings_repository_module = types.ModuleType("database.repositories.settings")
    settings_repository_module.SettingsRepository = type("SettingsRepository", (), {})

    loader_module = types.ModuleType("loader")
    loader_module.logger = logger

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []
    tgbot_services_module = types.ModuleType("tgbot.services")
    tgbot_services_module.__path__ = []
    device_pricing_module = _real_device_pricing()

    module_name = "device_slot_service_under_test"
    module_path = (
        Path(__file__).resolve().parents[1] / "tgbot" / "services" / "device_slot_service.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    stubs = {
        "database": database_module,
        "database.repositories": repositories_module,
        "database.repositories.user": user_repository_module,
        "database.repositories.settings": settings_repository_module,
        "loader": loader_module,
        "tgbot": tgbot_module,
        "tgbot.services": tgbot_services_module,
        "tgbot.services.device_pricing": device_pricing_module,
    }
    with patch.dict(sys.modules, stubs):
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_name, None)

    return module, logger


def _user(**overrides):
    defaults = {
        "user_id": 1,
        "vpn_username": "user_1",
        "remnawave_uuid": "uuid-1",
        "extra_devices": 0,
        "is_first_payment_made": True,
        "subscription_end_date": datetime.now() + timedelta(days=30),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _service(module, user=None, rw_user=None, devices=None, settings=None):
    """Сервис с моками репозиториев и панели."""
    user_repo = Mock()
    user_repo.get = AsyncMock(return_value=user if user is not None else _user())
    user_repo.set_extra_devices = AsyncMock()

    stored = settings or {}
    settings_repo = Mock()
    settings_repo.get_int = AsyncMock(side_effect=lambda key, default: stored.get(key, default))

    remnawave = Mock()
    remnawave.get_user_by_username = AsyncMock(
        return_value=rw_user if rw_user is not None else {"uuid": "uuid-1", "hwidDeviceLimit": 5}
    )
    remnawave.update_user = AsyncMock()
    remnawave.get_user_devices = AsyncMock(return_value=devices if devices is not None else [])
    remnawave.delete_user_device = AsyncMock()

    service = module.DeviceSlotService(user_repo, settings_repo, remnawave)
    return service, user_repo, remnawave


# =============================================================================
# --- quote: кому и почём продаём ---
# =============================================================================

class QuoteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module, _ = load_device_slot_service_module()

    async def test_expired_subscription_cannot_buy(self):
        service, _, _ = _service(
            self.module, user=_user(subscription_end_date=datetime.now() - timedelta(days=1))
        )
        quote = await service.quote(1)

        self.assertFalse(quote.ok)
        self.assertEqual(quote.error_code, self.module.CODE_NO_SUBSCRIPTION)

    async def test_trial_user_cannot_buy(self):
        """На пробном периоде (ни одного платежа) слоты не продаём."""
        service, _, _ = _service(self.module, user=_user(is_first_payment_made=False))
        quote = await service.quote(1)

        self.assertEqual(quote.error_code, self.module.CODE_TRIAL)

    async def test_cap_reached(self):
        service, _, _ = _service(self.module, user=_user(extra_devices=5))
        quote = await service.quote(1)

        self.assertEqual(quote.error_code, self.module.CODE_MAX_REACHED)
        self.assertEqual(quote.available, 0)

    async def test_cannot_exceed_cap_in_one_purchase(self):
        service, _, _ = _service(self.module, user=_user(extra_devices=3))
        quote = await service.quote(1, slots=3)  # доступно только 2

        self.assertEqual(quote.error_code, self.module.CODE_BAD_QUANTITY)

    async def test_price_follows_remaining_days(self):
        """Год впереди — платим за весь остаток, а не 49 ₽ за 12 месяцев."""
        service, _, _ = _service(
            self.module, user=_user(subscription_end_date=datetime.now() + timedelta(days=300))
        )
        quote = await service.quote(1)

        self.assertTrue(quote.ok)
        self.assertEqual(quote.price, 490.0)

    async def test_short_remainder_costs_a_full_month(self):
        service, _, _ = _service(
            self.module, user=_user(subscription_end_date=datetime.now() + timedelta(days=3))
        )
        quote = await service.quote(1)

        self.assertEqual(quote.price, 49.0)

    async def test_total_limit_includes_existing_slots(self):
        service, _, _ = _service(self.module, user=_user(extra_devices=2))
        quote = await service.quote(1)

        self.assertEqual(quote.total_limit, 5 + 2 + 1)

    async def test_admin_price_from_settings_is_used(self):
        service, _, _ = _service(
            self.module,
            settings={"extra_device_price": 99, "base_device_limit": 3, "max_extra_devices": 2},
        )
        quote = await service.quote(1)

        self.assertEqual(quote.price, 99.0)
        self.assertEqual(quote.base_limit, 3)
        self.assertEqual(quote.max_extra, 2)


# =============================================================================
# --- начисление и снятие слотов ---
# =============================================================================

class SlotMutationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module, _ = load_device_slot_service_module()

    async def test_add_slots_respects_cap(self):
        """Между счётом и вебхуком человек мог докупить слоты другим платежом."""
        service, user_repo, _ = _service(self.module, user=_user(extra_devices=4))

        result = await service.add_slots(1, 3)

        self.assertEqual(result, 5)
        user_repo.set_extra_devices.assert_awaited_once_with(1, 5)

    async def test_add_negative_slots_is_a_refund(self):
        service, user_repo, _ = _service(self.module, user=_user(extra_devices=3))

        result = await service.add_slots(1, -2)

        self.assertEqual(result, 1)
        user_repo.set_extra_devices.assert_awaited_once_with(1, 1)

    async def test_refund_cannot_push_slots_below_zero(self):
        service, user_repo, _ = _service(self.module, user=_user(extra_devices=1))

        result = await service.add_slots(1, -5)

        self.assertEqual(result, 0)

    async def test_set_slots_applies_absolute_value(self):
        """Количество с чекаута — осознанный выбор, уменьшение применяется сразу."""
        service, user_repo, _ = _service(self.module, user=_user(extra_devices=4))

        result = await service.set_slots(1, 1)

        self.assertEqual(result, 1)
        user_repo.set_extra_devices.assert_awaited_once_with(1, 1)

    async def test_expire_slots_resets_to_base(self):
        service, user_repo, remnawave = _service(self.module, user=_user(extra_devices=2))

        await service.expire_slots(1)

        user_repo.set_extra_devices.assert_awaited_once_with(1, 0)


# =============================================================================
# --- синхронизация лимита с панелью ---
# =============================================================================

class SyncLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module, self.logger = load_device_slot_service_module()

    async def test_writes_absolute_limit(self):
        """В панель уходит база + слоты: персональное поле отменяет глобальный fallback."""
        service, _, remnawave = _service(self.module, user=_user(extra_devices=2))

        result = await service.sync_limit(1)

        self.assertEqual(result.limit, 7)
        remnawave.update_user.assert_awaited_once_with("uuid-1", hwid_device_limit=7)

    async def test_expired_subscription_falls_back_to_base(self):
        service, _, remnawave = _service(
            self.module,
            user=_user(extra_devices=3, subscription_end_date=datetime.now() - timedelta(days=1)),
            rw_user={"uuid": "uuid-1", "hwidDeviceLimit": 8},
        )

        result = await service.sync_limit(1)

        self.assertEqual(result.limit, 5)
        remnawave.update_user.assert_awaited_once_with("uuid-1", hwid_device_limit=5)

    async def test_no_write_when_limit_already_matches(self):
        """Джоб ходит раз в час по всем владельцам слотов — лишний PATCH не нужен."""
        service, _, remnawave = _service(
            self.module,
            user=_user(extra_devices=2),
            rw_user={"uuid": "uuid-1", "hwidDeviceLimit": 7},
        )

        await service.sync_limit(1)

        remnawave.update_user.assert_not_awaited()

    async def test_panel_unavailable_returns_none(self):
        """Платёж не должен падать из-за лежащей панели — лимит догонит джоб."""
        service, _, remnawave = _service(self.module)
        remnawave.get_user_by_username.side_effect = RemnawaveTransportError("boom", "timeout")

        self.assertIsNone(await service.sync_limit(1))

    async def test_user_without_vpn_username_is_skipped(self):
        service, _, remnawave = _service(self.module, user=_user(vpn_username=None))

        self.assertIsNone(await service.sync_limit(1))
        remnawave.get_user_by_username.assert_not_awaited()


# =============================================================================
# --- авточистка устройств сверх лимита ---
# =============================================================================

def _device(hwid: str, updated_at: str):
    return {"hwid": hwid, "updatedAt": updated_at, "createdAt": updated_at}


class EnforceLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module, _ = load_device_slot_service_module()

    async def test_removes_stalest_devices_over_limit(self):
        """
        Панель проверяет лимит только при регистрации нового hwid — уже
        записанные устройства пережили бы снижение лимита без этой чистки.
        """
        devices = [
            _device("fresh", "2026-09-13T10:00:00Z"),
            _device("stale", "2026-01-01T10:00:00Z"),
            _device("middle", "2026-06-01T10:00:00Z"),
        ]
        service, _, remnawave = _service(
            self.module,
            user=_user(extra_devices=0, subscription_end_date=datetime.now() - timedelta(days=1)),
            rw_user={"uuid": "uuid-1", "hwidDeviceLimit": 1},
            devices=devices,
            settings={"base_device_limit": 1},
        )

        result = await service.sync_limit(1)

        self.assertEqual(result.removed, 2)
        removed = [call.args[1] for call in remnawave.delete_user_device.await_args_list]
        self.assertEqual(sorted(removed), ["middle", "stale"])

    async def test_devices_within_limit_are_untouched(self):
        devices = [_device("a", "2026-09-01T10:00:00Z"), _device("b", "2026-09-02T10:00:00Z")]
        service, _, remnawave = _service(
            self.module, user=_user(extra_devices=0), devices=devices
        )

        result = await service.sync_limit(1)

        self.assertEqual(result.removed, 0)
        remnawave.delete_user_device.assert_not_awaited()

    async def test_device_without_timestamp_is_dropped_first(self):
        devices = [
            _device("dated", "2026-09-01T10:00:00Z"),
            {"hwid": "undated"},
        ]
        service, _, remnawave = _service(
            self.module,
            user=_user(extra_devices=0),
            rw_user={"uuid": "uuid-1", "hwidDeviceLimit": 1},
            devices=devices,
            settings={"base_device_limit": 1},
        )

        await service.sync_limit(1)

        remnawave.delete_user_device.assert_awaited_once_with("uuid-1", "undated")


if __name__ == "__main__":
    unittest.main()
