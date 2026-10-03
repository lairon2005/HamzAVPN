import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from remnawave.client import RemnawaveTransportError


def load_profile_service_module():
    """Загружает модуль сервиса без запуска production-синглтонов из loader."""
    logger = Mock()

    db_module = types.ModuleType("db")
    db_module.User = type("User", (), {})

    database_module = types.ModuleType("database")
    database_module.__path__ = []
    repositories_module = types.ModuleType("database.repositories")
    repositories_module.__path__ = []
    user_repository_module = types.ModuleType("database.repositories.user")
    user_repository_module.UserRepository = type("UserRepository", (), {})

    loader_module = types.ModuleType("loader")
    loader_module.logger = logger

    module_name = "profile_service_under_test"
    module_path = (
        Path(__file__).resolve().parents[1]
        / "tgbot"
        / "services"
        / "profile_service.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    stubs = {
        "db": db_module,
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


class ProfileServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_failure_returns_retry_message_without_error_traceback(self):
        module, logger = load_profile_service_module()
        user = SimpleNamespace(vpn_username="user_123")
        user_repo = SimpleNamespace(get=AsyncMock(return_value=user))
        remnawave = SimpleNamespace(
            get_user_by_username=AsyncMock(
                side_effect=RemnawaveTransportError("GET", "ConnectTimeout")
            )
        )

        result = await module.ProfileService(user_repo, remnawave).get_profile(123)

        self.assertIsNone(result.vpn_user)
        self.assertIn("временно недоступна", result.error)
        self.assertIn("повторите попытку", result.error)
        logger.warning.assert_called_once()
        logger.error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
