import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from remnawave.client import RemnawaveTransportError


def load_key_service_module():
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

    module_name = "key_service_under_test"
    module_path = (
        Path(__file__).resolve().parents[1] / "tgbot" / "services" / "key_service.py"
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


NEW_URL = "https://sub.example/newshortuuid"


def _service(module, *, rw_user=None, devices=None, revoked_at=None):
    user = SimpleNamespace(vpn_username="user_123", remnawave_uuid="uuid-123")
    user_repo = SimpleNamespace(get=AsyncMock(return_value=user))

    base_user = {"uuid": "uuid-123", "subRevokedAt": revoked_at}
    remnawave = SimpleNamespace(
        get_user_by_username=AsyncMock(
            return_value=base_user if rw_user is None else rw_user
        ),
        get_user_devices=AsyncMock(
            return_value=devices if devices is not None else [{"hwid": "a"}, {"hwid": "b"}]
        ),
        revoke_user_subscription=AsyncMock(return_value={"subscriptionUrl": NEW_URL}),
        delete_all_user_devices=AsyncMock(return_value=None),
    )
    return module.KeyService(user_repo, remnawave), remnawave


def _ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat().replace("+00:00", "Z")


class KeyServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoke_returns_new_link_and_releases_devices(self):
        module, _ = load_key_service_module()
        service, remnawave = _service(module)

        result = await service.revoke(1)

        self.assertTrue(result.ok)
        self.assertEqual(result.subscription_url, NEW_URL)
        self.assertEqual(result.devices_released, 2)
        self.assertEqual(result.code, None)
        remnawave.revoke_user_subscription.assert_awaited_once_with("uuid-123")
        remnawave.delete_all_user_devices.assert_awaited_once_with("uuid-123")

    async def test_devices_are_cleaned_after_revoke_not_before(self):
        """
        Порядок критичен: пока ссылка жива, утёкший клиент успеет
        зарегистрироваться заново и займёт только что освобождённый слот.
        """
        module, _ = load_key_service_module()
        service, remnawave = _service(module)
        calls = []
        remnawave.revoke_user_subscription = AsyncMock(
            side_effect=lambda *a: calls.append("revoke") or {"subscriptionUrl": NEW_URL}
        )
        remnawave.delete_all_user_devices = AsyncMock(
            side_effect=lambda *a: calls.append("cleanup")
        )

        await service.revoke(1)

        self.assertEqual(calls, ["revoke", "cleanup"])

    async def test_cooldown_blocks_repeated_revoke(self):
        module, _ = load_key_service_module()
        service, remnawave = _service(module, revoked_at=_ago(minutes=2))

        result = await service.revoke(1)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, module.CODE_COOLDOWN)
        self.assertIn("8 мин", result.error)
        remnawave.revoke_user_subscription.assert_not_awaited()

    async def test_revoke_allowed_after_cooldown_expired(self):
        module, _ = load_key_service_module()
        service, _ = _service(module, revoked_at=_ago(minutes=30))

        self.assertTrue((await service.revoke(1)).ok)

    async def test_naive_timestamp_from_panel_does_not_crash(self):
        """Панель отдаёт aware-время, но naive не должен ронять перевыпуск."""
        module, _ = load_key_service_module()
        naive = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None)
        service, _ = _service(module, revoked_at=naive.isoformat())

        result = await service.revoke(1)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, module.CODE_COOLDOWN)

    async def test_cleanup_failure_still_reports_success(self):
        """Ссылка уже сменилась — сказать «не удалось» значило бы соврать."""
        module, logger = load_key_service_module()
        service, remnawave = _service(module)
        remnawave.delete_all_user_devices = AsyncMock(side_effect=RuntimeError("boom"))

        result = await service.revoke(1)

        self.assertTrue(result.ok)
        self.assertEqual(result.subscription_url, NEW_URL)
        self.assertEqual(result.devices_released, 0)
        logger.error.assert_called()

    async def test_device_count_failure_does_not_block_revoke(self):
        module, _ = load_key_service_module()
        service, remnawave = _service(module)
        remnawave.get_user_devices = AsyncMock(
            side_effect=RemnawaveTransportError("GET", "timeout")
        )

        result = await service.revoke(1)

        self.assertTrue(result.ok)
        self.assertEqual(result.devices_released, 0)
        remnawave.revoke_user_subscription.assert_awaited_once()

    async def test_transport_failure_returns_retry_message(self):
        module, logger = load_key_service_module()
        service, remnawave = _service(module)
        remnawave.revoke_user_subscription = AsyncMock(
            side_effect=RemnawaveTransportError("POST", "timeout")
        )

        result = await service.revoke(1)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, module.CODE_PANEL)
        self.assertEqual(result.error, module.PANEL_UNAVAILABLE)
        logger.error.assert_not_called()

    async def test_user_without_subscription_gets_friendly_error(self):
        module, _ = load_key_service_module()
        user_repo = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(vpn_username=None)))
        service = module.KeyService(user_repo, SimpleNamespace())

        result = await service.revoke(1)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, module.CODE_NO_SUBSCRIPTION)

    async def test_unknown_failure_is_generic(self):
        module, _ = load_key_service_module()
        service, remnawave = _service(module)
        remnawave.revoke_user_subscription = AsyncMock(side_effect=RuntimeError("boom"))

        result = await service.revoke(1)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, module.CODE_GENERIC)

    def test_every_result_code_has_web_notice(self):
        """Веб показывает текст только по коду — ненайденный код = немое окно."""
        module, _ = load_key_service_module()
        codes = {
            module.CODE_OK,
            module.CODE_COOLDOWN,
            module.CODE_NO_SUBSCRIPTION,
            module.CODE_PANEL,
            module.CODE_GENERIC,
        }
        self.assertEqual(codes, set(module.REVOKE_NOTICES))

    def test_unknown_query_value_yields_no_notice(self):
        module, _ = load_key_service_module()
        self.assertIsNone(module.REVOKE_NOTICES.get("<script>alert(1)</script>"))


if __name__ == "__main__":
    unittest.main()
