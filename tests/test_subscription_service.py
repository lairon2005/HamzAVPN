import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


def load_subscription_service_module():
    """Загружает сервис без запуска production-синглтонов из loader/db."""
    logger = Mock()

    database_module = types.ModuleType("database")
    database_module.__path__ = []
    repositories_module = types.ModuleType("database.repositories")
    repositories_module.__path__ = []
    user_repository_module = types.ModuleType("database.repositories.user")
    user_repository_module.UserRepository = type("UserRepository", (), {})

    loader_module = types.ModuleType("loader")
    loader_module.logger = logger

    module_name = "subscription_service_under_test"
    module_path = (
        Path(__file__).resolve().parents[1]
        / "tgbot"
        / "services"
        / "subscription_service.py"
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

    return module


class SubscriptionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_trial_user_gets_default_monthly_limit(self):
        module = load_subscription_service_module()
        user = SimpleNamespace(
            user_id=123,
            vpn_username=None,
            remnawave_uuid=None,
        )
        user_repo = SimpleNamespace(
            get=AsyncMock(return_value=user),
            update_vpn_username=AsyncMock(),
            update_remnawave_uuid=AsyncMock(),
            extend_subscription=AsyncMock(),
            set_trial_received=AsyncMock(),
        )
        remnawave = SimpleNamespace(
            create_user=AsyncMock(return_value={"uuid": "rw-uuid"}),
        )

        await module.SubscriptionService(user_repo, remnawave).activate_trial(123)

        kwargs = remnawave.create_user.await_args.kwargs
        self.assertEqual(
            kwargs["traffic_limit_bytes"],
            module.DEFAULT_TRAFFIC_LIMIT_BYTES,
        )
        self.assertEqual(kwargs["traffic_limit_strategy"], "MONTH")

    async def test_bonus_extension_does_not_change_existing_limit(self):
        module = load_subscription_service_module()
        user = SimpleNamespace(
            user_id=123,
            vpn_username="user_123",
            remnawave_uuid="rw-uuid",
        )
        user_repo = SimpleNamespace(
            get=AsyncMock(return_value=user),
            update_vpn_username=AsyncMock(),
            update_remnawave_uuid=AsyncMock(),
            extend_subscription=AsyncMock(),
        )
        remnawave = SimpleNamespace(
            get_user_by_username=AsyncMock(
                return_value={"expireAt": "2026-08-01T00:00:00.000Z"}
            ),
            update_user=AsyncMock(),
            reset_user_traffic=AsyncMock(),
        )

        await module.SubscriptionService(user_repo, remnawave).extend(123, 3)

        kwargs = remnawave.update_user.await_args.kwargs
        self.assertIsNone(kwargs["traffic_limit_bytes"])
        self.assertIsNone(kwargs["traffic_limit_strategy"])
        remnawave.reset_user_traffic.assert_not_awaited()

    async def test_explicit_zero_creates_unlimited_user_without_reset(self):
        module = load_subscription_service_module()
        user = SimpleNamespace(
            user_id=123,
            vpn_username="user_123",
            remnawave_uuid=None,
        )
        user_repo = SimpleNamespace(
            get=AsyncMock(return_value=user),
            update_vpn_username=AsyncMock(),
            update_remnawave_uuid=AsyncMock(),
            extend_subscription=AsyncMock(),
        )
        remnawave = SimpleNamespace(
            create_user=AsyncMock(return_value={"uuid": "rw-uuid"}),
        )

        await module.SubscriptionService(user_repo, remnawave).extend(
            123, 30, data_limit_gb=0
        )

        kwargs = remnawave.create_user.await_args.kwargs
        self.assertEqual(kwargs["traffic_limit_bytes"], 0)
        self.assertEqual(kwargs["traffic_limit_strategy"], "NO_RESET")


if __name__ == "__main__":
    unittest.main()
