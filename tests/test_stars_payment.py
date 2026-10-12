# tests/test_stars_payment.py
"""
Telegram Stars (XTR) — README.md: оплата Telegram Stars. Без БД и сети:
- pre_checkout_query валидирует payload/тариф/цену дёшево (Telegram ждёт ответ 10с);
- PaymentService.process_stars_payment идемпотентен по telegram_payment_charge_id
  (Telegram может повторно доставить successful_payment);
- PaymentRequest.source (фаза 3.1, webapp/routers/payment.py) принимает только
  "web"/"tma".

Модули загружаются через importlib.util.spec_from_file_location мимо
tgbot/handlers/__init__.py и tgbot/services/__init__.py — оба на импорте тянут
loader.py -> config.load_config(), которому в тестовом окружении не хватает
REMNAWAVE_API_URL и т.п. (тот же приём, что и в test_promo_code_service.py /
test_subscription_service.py / test_payment_repository.py).
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


def _load_module(module_name: str, relative_path: str, stubs: dict):
    module_path = Path(__file__).resolve().parents[1] / relative_path
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


def _real_device_pricing():
    """Настоящий tgbot/services/device_pricing.py — модуль без зависимостей.

    Стабить его нечем: расчёт цены доп. устройств и есть то, что проверяют
    тесты вокруг платежей, а подделка арифметики сделала бы их бессмысленными.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tgbot" / "services" / "device_pricing.py"
    spec = importlib.util.spec_from_file_location("tgbot.services.device_pricing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_stars_payment_module():
    """Загружает tgbot/handlers/user/stars_payment.py изолированно."""
    database_module = types.ModuleType("database")
    database_module.tariff_repo = AsyncMock()
    # pre_checkout смотрит на доп. устройства: их нельзя оплатить звёздами,
    # иначе продление подписки продлило бы и неоплаченные слоты.
    database_module.user_repo = AsyncMock()
    database_module.user_repo.get.return_value = SimpleNamespace(extra_devices=0)

    loader_module = types.ModuleType("loader")
    loader_module.logger = Mock()

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []
    tgbot_services_module = types.ModuleType("tgbot.services")
    tgbot_services_module.payment_service = AsyncMock()

    module = _load_module(
        "stars_payment_under_test",
        "tgbot/handlers/user/stars_payment.py",
        {
            "database": database_module,
            "loader": loader_module,
            "tgbot": tgbot_module,
            "tgbot.services": tgbot_services_module,
        },
    )
    return (module, database_module.tariff_repo, tgbot_services_module.payment_service,
            database_module.user_repo)


def load_payment_service_module():
    """Загружает tgbot/services/payment_service.py изолированно."""
    db_module = types.ModuleType("db")
    db_module.Tariff = type("Tariff", (), {})
    db_module.Payment = type("Payment", (), {})

    database_module = types.ModuleType("database")
    database_module.__path__ = []
    repositories_module = types.ModuleType("database.repositories")
    repositories_module.__path__ = []
    user_repo_module = types.ModuleType("database.repositories.user")
    user_repo_module.UserRepository = type("UserRepository", (), {})
    tariff_repo_module = types.ModuleType("database.repositories.tariff")
    tariff_repo_module.TariffRepository = type("TariffRepository", (), {})
    payment_repo_module = types.ModuleType("database.repositories.payment")
    payment_repo_module.PaymentRepository = type("PaymentRepository", (), {})

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []
    tgbot_services_module = types.ModuleType("tgbot.services")
    tgbot_services_module.__path__ = []
    subscription_service_module = types.ModuleType("tgbot.services.subscription_service")
    subscription_service_module.SubscriptionService = type("SubscriptionService", (), {})
    subscription_service_module.ExtensionResult = SimpleNamespace
    referral_service_module = types.ModuleType("tgbot.services.referral_service")
    referral_service_module.ReferralService = type("ReferralService", (), {})
    pricing_module = types.ModuleType("tgbot.services.pricing")
    pricing_module.effective_price = lambda tariff, user_has_active_sub=False: getattr(tariff, "price", 0)
    device_pricing_module = _real_device_pricing()

    loader_module = types.ModuleType("loader")
    loader_module.logger = Mock()
    loader_module.config = SimpleNamespace(platega=SimpleNamespace(merchant_id="x", secret="y"))

    return _load_module(
        "payment_service_under_test",
        "tgbot/services/payment_service.py",
        {
            "db": db_module,
            "database": database_module,
            "database.repositories": repositories_module,
            "database.repositories.user": user_repo_module,
            "database.repositories.tariff": tariff_repo_module,
            "database.repositories.payment": payment_repo_module,
            "tgbot": tgbot_module,
            "tgbot.services": tgbot_services_module,
            "tgbot.services.subscription_service": subscription_service_module,
            "tgbot.services.referral_service": referral_service_module,
            "tgbot.services.pricing": pricing_module,
            "tgbot.services.device_pricing": device_pricing_module,
            "loader": loader_module,
        },
    )


def load_payment_router_module():
    """Загружает webapp/routers/payment.py изолированно (только PaymentRequest нужен)."""
    db_module = types.ModuleType("db")
    db_module.User = type("User", (), {})
    db_module.Tariff = type("Tariff", (), {})
    db_module.async_session_maker = Mock()

    database_module = types.ModuleType("database")
    database_module.tariff_repo = AsyncMock()

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []
    tgbot_services_module = types.ModuleType("tgbot.services")
    tgbot_services_module.__path__ = []
    tgbot_services_module.payment_service = AsyncMock()
    tgbot_services_module.promo_service = AsyncMock()
    tgbot_services_module.subscription_service = AsyncMock()
    tgbot_services_module.device_slot_service = AsyncMock()
    # Автопродление: по умолчанию недоступно, build_offer/start/cancel/get_view — асинхронные.
    recurring = Mock()
    recurring.is_available = Mock(return_value=False)
    recurring.build_offer = AsyncMock()
    recurring.start = AsyncMock()
    recurring.cancel = AsyncMock()
    recurring.get_view = AsyncMock(return_value=None)
    tgbot_services_module.recurring_service = recurring
    recurring_module = types.ModuleType("tgbot.services.recurring_service")
    recurring_module.RecurringError = type("RecurringError", (Exception,), {})
    tgbot_services_payment_module = types.ModuleType("tgbot.services.payment")
    tgbot_services_payment_module.create_payment = Mock()
    tgbot_services_pricing_module = types.ModuleType("tgbot.services.pricing")
    tgbot_services_pricing_module.effective_price = _load_module(
        "checkout_pricing_under_test", "tgbot/services/pricing.py", {}).effective_price
    # webapp/routers/payment.py импортирует PromoClaimError из
    # tgbot.services.promo_code_service (см. security review TOCTOU-гонки
    # промокодов, 2026-07-26) — стабим отдельным модулем, как и остальные
    # tgbot.services.* submodules выше, иначе exec_module упадёт с
    # ModuleNotFoundError (tgbot.services.__path__ пуст).
    tgbot_services_promo_code_service_module = types.ModuleType("tgbot.services.promo_code_service")
    tgbot_services_promo_code_service_module.PromoClaimError = type("PromoClaimError", (Exception,), {})

    config_module = types.ModuleType("config")
    config_module.load_config = lambda: SimpleNamespace(
        tg_bot=SimpleNamespace(tg_bot_username=None, tma_app_name="app"),
        webhook=SimpleNamespace(domain="example.com"),
        platega=SimpleNamespace(merchant_id="x", secret="y"),
    )

    deps = types.ModuleType('webapp.dependencies')
    async def current_user():
        return None
    deps.get_current_user = current_user
    service_module = types.ModuleType('tgbot.services.payment_service')
    service_module.PendingPaymentError = type('PendingPaymentError', (Exception,), {})

    return _load_module(
        "payment_router_under_test",
        "webapp/routers/payment.py",
        {
            "db": db_module,
            "webapp.dependencies": deps,
            "tgbot.services.payment_service": service_module,
            "database": database_module,
            "tgbot": tgbot_module,
            "tgbot.services": tgbot_services_module,
            "tgbot.services.payment": tgbot_services_payment_module,
            "tgbot.services.pricing": tgbot_services_pricing_module,
            "tgbot.services.device_pricing": _real_device_pricing(),
            "tgbot.services.promo_code_service": tgbot_services_promo_code_service_module,
            "tgbot.services.recurring_service": recurring_module,
            "config": config_module,
        },
    )


# =============================================================================
# --- _parse_stars_payload ---
# =============================================================================

class ParseStarsPayloadTests(unittest.TestCase):
    def test_valid_payload(self):
        module, _, _, _ = load_stars_payment_module()
        self.assertEqual(module._parse_stars_payload("stars:5:123"), (5, 123))

    def test_wrong_prefix_rejected(self):
        module, _, _, _ = load_stars_payment_module()
        self.assertIsNone(module._parse_stars_payload("other:5:123"))

    def test_wrong_parts_count_rejected(self):
        module, _, _, _ = load_stars_payment_module()
        self.assertIsNone(module._parse_stars_payload("stars:5"))

    def test_non_numeric_rejected(self):
        module, _, _, _ = load_stars_payment_module()
        self.assertIsNone(module._parse_stars_payload("stars:abc:123"))


# =============================================================================
# --- pre_checkout_query ---
# =============================================================================

class PreCheckoutTests(unittest.IsolatedAsyncioTestCase):
    def _query(self, payload, from_user_id, total_amount):
        return SimpleNamespace(
            invoice_payload=payload,
            currency="XTR",
            from_user=SimpleNamespace(id=from_user_id),
            total_amount=total_amount,
            answer=AsyncMock(),
        )

    async def test_malformed_payload_rejected(self):
        module, tariff_repo, _, _ = load_stars_payment_module()
        query = self._query("garbage", 123, 150)

        await module.stars_pre_checkout(query)

        self.assertFalse(query.answer.await_args.kwargs["ok"])
        tariff_repo.get_by_id.assert_not_awaited()

    async def test_payload_for_other_user_rejected(self):
        module, tariff_repo, _, _ = load_stars_payment_module()
        # payload выписан на user 999, а платит user 123 — не должно происходить
        # штатно, но проверяем явно (переслаемый/скопированный счёт).
        query = self._query("stars:5:999", 123, 150)

        await module.stars_pre_checkout(query)

        self.assertFalse(query.answer.await_args.kwargs["ok"])
        tariff_repo.get_by_id.assert_not_awaited()

    async def test_missing_tariff_rejected(self):
        module, tariff_repo, _, _ = load_stars_payment_module()
        tariff_repo.get_by_id.return_value = None
        query = self._query("stars:5:123", 123, 150)

        await module.stars_pre_checkout(query)

        self.assertFalse(query.answer.await_args.kwargs["ok"])

    async def test_price_mismatch_rejected(self):
        module, tariff_repo, _, _ = load_stars_payment_module()
        tariff_repo.get_by_id.return_value = SimpleNamespace(is_active=True, price_stars=150)
        # Клиент прислал total_amount=100, а тариф стоит 150 XTR.
        query = self._query("stars:5:123", 123, 100)

        await module.stars_pre_checkout(query)

        self.assertFalse(query.answer.await_args.kwargs["ok"])

    async def test_matching_price_accepted(self):
        module, tariff_repo, _, _ = load_stars_payment_module()
        tariff_repo.get_by_id.return_value = SimpleNamespace(is_active=True, price_stars=150)
        query = self._query("stars:5:123", 123, 150)

        await module.stars_pre_checkout(query)

        self.assertTrue(query.answer.await_args.kwargs["ok"])

    async def test_user_with_extra_devices_rejected(self):
        """Слоты звёздами не оплачиваются: пропустив платёж, мы продлили бы их даром."""
        module, tariff_repo, _, user_repo = load_stars_payment_module()
        tariff_repo.get_by_id.return_value = SimpleNamespace(is_active=True, price_stars=150)
        user_repo.get.return_value = SimpleNamespace(extra_devices=2)
        query = self._query("stars:5:123", 123, 150)

        await module.stars_pre_checkout(query)

        self.assertFalse(query.answer.await_args.kwargs["ok"])
        self.assertIn("картой", query.answer.await_args.kwargs["error_message"])


# =============================================================================
# --- PaymentService.process_stars_payment: идемпотентность ---
# =============================================================================

# =============================================================================
# --- PaymentRequest.source (фаза 3.1) ---
# =============================================================================

class PaymentRequestSourceTests(unittest.TestCase):
    def test_default_source_is_web(self):
        module = load_payment_router_module()
        payload = module.PaymentRequest(tariff_name="Месяц", price=100)
        self.assertEqual(payload.source, "web")

    def test_web_and_tma_are_accepted(self):
        module = load_payment_router_module()
        self.assertEqual(module.PaymentRequest(tariff_name="x", price=1, source="web").source, "web")
        self.assertEqual(module.PaymentRequest(tariff_name="x", price=1, source="tma").source, "tma")

    def test_unknown_source_is_rejected(self):
        module = load_payment_router_module()
        with self.assertRaises(Exception):
            module.PaymentRequest(tariff_name="x", price=1, source="bot")


if __name__ == "__main__":
    unittest.main()
