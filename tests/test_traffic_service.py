"""Докупка трафика: кому продаём, сколько стоит, как лимит попадает в панель."""
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from remnawave.client import RemnawaveTransportError
from test_device_slot_service import _real_device_pricing
from real_traffic_pricing import real_traffic_pricing

GIB = 1024 ** 3


def load_traffic_service_module():
    """Загружает сервис без production-синглтонов из loader."""
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

    module_name = "traffic_service_under_test"
    module_path = Path(__file__).resolve().parents[1] / "tgbot" / "services" / "traffic_service.py"
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
        "tgbot.services.device_pricing": _real_device_pricing(),
        "tgbot.services.traffic_pricing": real_traffic_pricing(),
    }
    with patch.dict(sys.modules, stubs):
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_name, None)
    return module


def _user(**overrides):
    defaults = {
        "user_id": 1,
        "vpn_username": "user_1",
        "remnawave_uuid": "uuid-1",
        "subscription_data_limit_gb": 500,
        "extra_traffic_gb": 0,
        "is_first_payment_made": True,
        "subscription_end_date": datetime.now() + timedelta(days=30),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _service(module, user=None, rw_user=None, settings=None):
    user_repo = Mock()
    user_repo.get = AsyncMock(return_value=user if user is not None else _user())
    user_repo.set_extra_traffic = AsyncMock()
    user_repo.set_traffic_quota = AsyncMock()
    user_repo.set_traffic_state = AsyncMock()

    stored = settings or {}
    settings_repo = Mock()
    settings_repo.get_int = AsyncMock(side_effect=lambda key, default: stored.get(key, default))

    remnawave = Mock()
    remnawave.get_user_by_username = AsyncMock(
        return_value=rw_user if rw_user is not None
        else {"uuid": "uuid-1", "trafficLimitBytes": 500 * GIB}
    )
    remnawave.update_user = AsyncMock()

    return module.TrafficService(user_repo, settings_repo, remnawave), user_repo, remnawave


class QuoteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_traffic_service_module()

    async def test_expired_subscription_cannot_buy(self):
        service, _, _ = _service(
            self.module, user=_user(subscription_end_date=datetime.now() - timedelta(days=1))
        )
        quote = await service.quote(1)
        self.assertEqual(quote.error_code, self.module.CODE_NO_SUBSCRIPTION)

    async def test_trial_cannot_buy(self):
        service, _, _ = _service(self.module, user=_user(is_first_payment_made=False))
        self.assertEqual((await service.quote(1)).error_code, self.module.CODE_TRIAL)

    async def test_unlimited_tariff_has_nothing_to_buy(self):
        service, _, _ = _service(self.module, user=_user(subscription_data_limit_gb=0))
        self.assertEqual((await service.quote(1)).error_code, self.module.CODE_UNLIMITED)

    async def test_month_left_costs_full_pack_price(self):
        service, _, _ = _service(self.module, user=_user(subscription_end_date=datetime.now() + timedelta(days=30)))
        quote = await service.quote(1, 2)

        self.assertTrue(quote.ok)
        self.assertEqual(quote.price, 49 * 2)
        self.assertEqual(quote.total_limit_gb, 500 + 200)

    async def test_few_days_left_still_costs_a_month(self):
        service, _, _ = _service(self.module, user=_user(subscription_end_date=datetime.now() + timedelta(days=2)))
        quote = await service.quote(1, 1)
        self.assertEqual(quote.price, 49)

    async def test_long_subscription_is_prorated(self):
        service, _, _ = _service(self.module, user=_user(subscription_end_date=datetime.now() + timedelta(days=90)))
        quote = await service.quote(1, 1)
        self.assertEqual(quote.price, 49 * 3)

    async def test_cap_is_enforced(self):
        service, _, _ = _service(self.module, user=_user(extra_traffic_gb=1000))
        self.assertEqual((await service.quote(1)).error_code, self.module.CODE_MAX_REACHED)

    async def test_cannot_exceed_cap_in_one_go(self):
        service, _, _ = _service(self.module, user=_user(extra_traffic_gb=900))
        self.assertEqual((await service.quote(1, 2)).error_code, self.module.CODE_BAD_QUANTITY)

    async def test_zero_packs_rejected(self):
        service, _, _ = _service(self.module)
        self.assertEqual((await service.quote(1, 0)).error_code, self.module.CODE_BAD_QUANTITY)

    async def test_legacy_user_quota_is_read_from_panel_and_remembered(self):
        service, user_repo, _ = _service(
            self.module,
            user=_user(subscription_data_limit_gb=None),
            rw_user={"uuid": "uuid-1", "trafficLimitBytes": 1000 * GIB},
        )
        quote = await service.quote(1)

        self.assertTrue(quote.ok)
        self.assertEqual(quote.quota_gb, 1000)
        user_repo.set_traffic_quota.assert_awaited_once_with(1, 1000)

    async def test_legacy_unlimited_user_detected_from_panel(self):
        service, _, _ = _service(
            self.module,
            user=_user(subscription_data_limit_gb=None),
            rw_user={"uuid": "uuid-1", "trafficLimitBytes": 0},
        )
        self.assertEqual((await service.quote(1)).error_code, self.module.CODE_UNLIMITED)

    async def test_panel_down_gives_panel_error_not_crash(self):
        service, _, remnawave = _service(self.module, user=_user(subscription_data_limit_gb=None))
        remnawave.get_user_by_username.side_effect = RemnawaveTransportError("GET", "ConnectError")
        self.assertEqual((await service.quote(1)).error_code, self.module.CODE_PANEL)

    async def test_admin_settings_change_price_and_pack(self):
        service, _, _ = _service(
            self.module,
            settings={"traffic_pack_price": 99, "traffic_pack_gb": 50},
        )
        quote = await service.quote(1, 2)
        self.assertEqual(quote.price, 99 * 2)
        self.assertEqual(quote.added_gb, 100)


