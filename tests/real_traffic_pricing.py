# tests/real_traffic_pricing.py
"""Настоящий tgbot/services/traffic_pricing.py для изолированных загрузчиков.

Чистые функции цены трафика — как и intro_offer, подделывать их нельзя.
Модуль сам импортирует device_pricing (billing_months), поэтому на время
загрузки подкладываем пакеты-пустышки и настоящий device_pricing.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

_SERVICES = Path(__file__).resolve().parents[1] / "tgbot" / "services"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def real_traffic_pricing():
    tgbot = types.ModuleType("tgbot")
    tgbot.__path__ = []
    services = types.ModuleType("tgbot.services")
    services.__path__ = []
    device_pricing = _load("tgbot.services.device_pricing", _SERVICES / "device_pricing.py")
    with patch.dict(sys.modules, {
        "tgbot": tgbot,
        "tgbot.services": services,
        "tgbot.services.device_pricing": device_pricing,
    }):
        return _load("tgbot.services.traffic_pricing", _SERVICES / "traffic_pricing.py")


def real_custom_pricing():
    """Настоящий tgbot/services/custom_pricing.py (чистые функции без зависимостей)."""
    return _load("tgbot.services.custom_pricing", _SERVICES / "custom_pricing.py")


def real_traffic_checkout():
    with patch.dict(sys.modules, {"tgbot.services.traffic_pricing": real_traffic_pricing()}):
        return _load("tgbot.services.traffic_checkout", _SERVICES / "traffic_checkout.py")
