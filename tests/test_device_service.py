import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from remnawave.client import RemnawaveTransportError


def load_device_service_module():
    """Загружает модуль сервиса без запуска production-синглтонов из loader."""
    logger = Mock()

    database_module = types.ModuleType("database")
    database_module.__path__ = []
    repositories_module = types.ModuleType("database.repositories")
    repositories_module.__path__ = []
    user_repository_module = types.ModuleType("database.repositories.user")
    user_repository_module.UserRepository = type("UserRepository", (), {})

    loader_module = types.ModuleType("loader")
    loader_module.logger = logger

    module_name = "device_service_under_test"
    module_path = (
        Path(__file__).resolve().parents[1] / "tgbot" / "services" / "device_service.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    stubs = {
        "database": database_module,
        "database.repositories": repositories_module,
        "database.repositories.user": user_repository_module,
        "loader": loader_module,
    }
    with patch.dict(sys.modules, stubs):
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_name, None)

    return module, logger


RAW_DEVICES = [
    {
        "hwid": "aaa111",
        "platform": "Android",
        "osVersion": "11",
        "deviceModel": "SM-A405FM",
        "userAgent": "Happ/3.26.3/Android/test-device-1",
        "requestIp": "192.0.2.1",
        "createdAt": "2026-07-27T10:18:22.101Z",
        "updatedAt": "2026-07-27T10:18:22.101Z",
    },
    {
        "hwid": "bbb222",
        "platform": "iOS",
        "osVersion": "18.1",
        "deviceModel": "iPhone14,2",
        "userAgent": "Happ/2.9.0/iOS/998",
        "requestIp": "198.51.100.2",
        "createdAt": "2026-08-01T09:00:00.000Z",
        "updatedAt": "2026-09-01T09:00:00.000Z",
    },
]


def _service(module, *, devices=None, rw_user=None, remnawave_extra=None):
    user = SimpleNamespace(vpn_username="user_123", remnawave_uuid="uuid-123")
    user_repo = SimpleNamespace(get=AsyncMock(return_value=user))
    remnawave = SimpleNamespace(
        get_user_by_username=AsyncMock(
            return_value=rw_user if rw_user is not None else {"uuid": "uuid-123", "hwidDeviceLimit": 3}
        ),
        get_user_devices=AsyncMock(return_value=devices if devices is not None else list(RAW_DEVICES)),
        delete_user_device=AsyncMock(return_value=[]),
    )
    for name, value in (remnawave_extra or {}).items():
        setattr(remnawave, name, value)
    return module.DeviceService(user_repo, remnawave), remnawave


class DeviceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_devices_sorts_recent_first_and_reports_limit(self):
        module, _ = load_device_service_module()
        service, _ = _service(module)

        result = await service.list_devices(1)

        self.assertIsNone(result.error)
        self.assertEqual([d.hwid for d in result.devices], ["bbb222", "aaa111"])
        self.assertEqual(result.used, 2)
        self.assertEqual(result.limit, 3)
        self.assertFalse(result.is_full)

    async def test_device_fields_are_humanised(self):
        module, _ = load_device_service_module()
        service, _ = _service(module)

        device = (await service.list_devices(1)).devices[1]

        self.assertEqual(device.title, "SM-A405FM")
        self.assertEqual(device.app, "Happ 3.26.3")
        self.assertEqual(device.platform, "Android 11")
        self.assertEqual(device.last_seen_display, "27.07.2026 10:18")

    async def test_is_full_when_limit_reached(self):
        module, _ = load_device_service_module()
        service, _ = _service(module, rw_user={"uuid": "uuid-123", "hwidDeviceLimit": 2})

        self.assertTrue((await service.list_devices(1)).is_full)

    async def test_no_limit_means_never_full(self):
        module, _ = load_device_service_module()
        service, _ = _service(module, rw_user={"uuid": "uuid-123", "hwidDeviceLimit": None})

        result = await service.list_devices(1)
        self.assertFalse(result.is_full)
        self.assertEqual(result.limit_display, "без ограничения")

    async def test_delete_resolves_key_to_hwid_of_own_device(self):
        module, _ = load_device_service_module()
        service, remnawave = _service(module)
        key = module.device_key("bbb222")

        ok, error = await service.delete_device(1, key)

        self.assertTrue(ok)
        self.assertIsNone(error)
        remnawave.delete_user_device.assert_awaited_once_with("uuid-123", "bbb222")

    async def test_delete_rejects_key_that_is_not_users_device(self):
        module, _ = load_device_service_module()
        service, remnawave = _service(module)

        ok, error = await service.delete_device(1, module.device_key("someone-elses"))

        self.assertFalse(ok)
        self.assertEqual(error, module.DEVICE_NOT_FOUND)
        remnawave.delete_user_device.assert_not_awaited()

    async def test_transport_failure_returns_retry_message(self):
        module, logger = load_device_service_module()
        service, remnawave = _service(module)
        remnawave.get_user_devices = AsyncMock(
            side_effect=RemnawaveTransportError("GET", "timeout")
        )

        result = await service.list_devices(1)

        self.assertEqual(result.error, module.PANEL_UNAVAILABLE)
        self.assertEqual(result.devices, [])
        logger.error.assert_not_called()

    async def test_user_without_subscription_gets_friendly_error(self):
        module, _ = load_device_service_module()
        user_repo = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(vpn_username=None)))
        service = module.DeviceService(user_repo, SimpleNamespace())

        result = await service.list_devices(1)

        self.assertEqual(result.error, module.NO_SUBSCRIPTION)

    def test_device_key_is_short_enough_for_callback_data(self):
        module, _ = load_device_service_module()
        key = module.device_key("a" * 200)

        # callback_data ограничена 64 байтами, префикс "dev_del_ok:" занимает 11
        self.assertEqual(len(key), 12)
        self.assertLess(len(f"dev_del_ok:{key}".encode()), 64)


if __name__ == "__main__":
    unittest.main()
