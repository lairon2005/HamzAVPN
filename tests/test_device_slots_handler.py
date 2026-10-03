# tests/test_device_slots_handler.py
"""
Докупка устройств: выход из висящего счёта.

Регресс на тупик: пока неоплаченный счёт в статусе pending, второй создать
нельзя (иначе вебхук по старому начислит слоты повторно), а Platega отменяет
его сама только минут через 30. Пользователь, выбравший карту и передумавший
в пользу СБП, получал «завершите или отмените его» и одну кнопку в главное
меню — отменить счёт было нечем.

Модуль грузится через importlib мимо tgbot/handlers/__init__.py и
tgbot/services/__init__.py (тот же приём, что в test_stars_payment.py): оба на
импорте тянут loader.py -> config.load_config(), которому в тестовом окружении
не хватает REMNAWAVE_API_URL и прочего.
"""
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from test_stars_payment import _load_module


def _config_stub():
    return SimpleNamespace(
        platega=SimpleNamespace(shop_id="shop", secret_key="secret"),
        tg_bot=SimpleNamespace(ui_mode="bot", tg_bot_username="bot", tma_app_name="app"),
        webhook=SimpleNamespace(domain="localhost"),
    )


def _load_inline_keyboards():
    """Настоящие клавиатуры: тест проверяет именно callback_data кнопок.

    db.py и tgbot/services/__init__.py на импорте зовут load_config(), поэтому
    подменяются стабами; pricing.py зависимостей не имеет и грузится настоящим.
    """
    loader_module = types.ModuleType("loader")
    loader_module.config = _config_stub()
    loader_module.logger = Mock()

    db_module = types.ModuleType("db")
    db_module.Tariff = object
    db_module.PromoCode = object
    db_module.Channel = object

    pricing_module = _load_module(
        "pricing_under_test", "tgbot/services/pricing.py", {}
    )

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []
    services_module = types.ModuleType("tgbot.services")
    services_module.__path__ = []
    services_module.pricing = pricing_module

    return _load_module(
        "inline_under_test",
        "tgbot/keyboards/inline.py",
        {
            "loader": loader_module,
            "db": db_module,
            "tgbot": tgbot_module,
            "tgbot.services": services_module,
            "tgbot.services.pricing": pricing_module,
        },
    )


def load_device_slots_module():
    inline = _load_inline_keyboards()

    loader_module = types.ModuleType("loader")
    loader_module.config = _config_stub()
    loader_module.logger = Mock()

    tgbot_module = types.ModuleType("tgbot")
    tgbot_module.__path__ = []
    keyboards_module = types.ModuleType("tgbot.keyboards")
    keyboards_module.__path__ = []

    services_module = types.ModuleType("tgbot.services")
    services_module.device_slot_service = SimpleNamespace(quote=AsyncMock())
    services_module.payment = SimpleNamespace(
        create_payment=Mock(),
        get_payment_url=Mock(return_value="https://pay.platega.io/pay/1"),
    )
    services_module.payment_service = SimpleNamespace(
        get_pending_payment=AsyncMock(return_value=None),
        cancel_pending_payment=AsyncMock(return_value=None),
        create_payment_record=AsyncMock(),
    )

    database_module = types.ModuleType("database")
    database_module.user_repo = AsyncMock()

    module = _load_module(
        "device_slots_under_test",
        "tgbot/handlers/user/device_slots.py",
        {
            "loader": loader_module,
            "database": database_module,
            "tgbot": tgbot_module,
            "tgbot.keyboards": keyboards_module,
            "tgbot.keyboards.inline": inline,
            "tgbot.services": services_module,
        },
    )
    return module, services_module


def _quote(**overrides):
    defaults = {
        "ok": True,
        "error": None,
        "slots": 2,
        "price": 200.0,
        "price_per_slot": 100,
        "remaining_days": 30,
        "current_extra": 0,
        "base_limit": 3,
        "available": 5,
        "total_limit": 5,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _pending(**overrides):
    defaults = {
        "provider_payment_id": "p-1",
        "payment_url": "https://pay.platega.io/pay/1",
        "final_amount": 200.0,
        "kind": "devices",
        "extra_devices": 2,
        "tariff_id": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _call(data: str):
    message = SimpleNamespace(
        edit_text=AsyncMock(return_value=SimpleNamespace(message_id=7)),
        answer=AsyncMock(),
        delete=AsyncMock(),
    )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=42),
        answer=AsyncMock(),
        message=message,
    )


def _callback_data(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def _urls(markup):
    return [btn.url for row in markup.inline_keyboard for btn in row if btn.url]


class PendingInvoiceScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_invoice_offers_cancel_instead_of_dead_end(self):
        """Главный регресс: экран висящего счёта обязан давать его отменить."""
        module, services = load_device_slots_module()
        services.payment_service.get_pending_payment.return_value = _pending()

        call = _call("slots_pay:card:2")
        await module.pay_slots_handler(call, state=AsyncMock(), bot=AsyncMock())

        # Счёт не выставлялся второй раз — защита от двойного начисления цела.
        services.payment.create_payment.assert_not_called()

        _, kwargs = call.message.edit_text.call_args
        callbacks = _callback_data(kwargs["reply_markup"])
        self.assertIn("slots_cancel_invoice:2", callbacks)
        self.assertNotEqual(callbacks, ["back_to_main_menu"])

    async def test_pending_invoice_shows_payment_link(self):
        module, services = load_device_slots_module()
        services.payment_service.get_pending_payment.return_value = _pending()

        call = _call("slots_pay:sbp:1")
        await module.pay_slots_handler(call, state=AsyncMock(), bot=AsyncMock())

        _, kwargs = call.message.edit_text.call_args
        self.assertEqual(_urls(kwargs["reply_markup"]), ["https://pay.platega.io/pay/1"])

    async def test_unavailable_platega_still_allows_cancel(self):
        """Панель оплаты может не ответить — отмена нужна именно тогда."""
        module, services = load_device_slots_module()
        services.payment_service.get_pending_payment.return_value = _pending(payment_url=None)

        call = _call("slots_pay:card:3")
        await module.pay_slots_handler(call, state=AsyncMock(), bot=AsyncMock())

        _, kwargs = call.message.edit_text.call_args
        self.assertEqual(_urls(kwargs["reply_markup"]), [])
        self.assertIn("slots_cancel_invoice:3", _callback_data(kwargs["reply_markup"]))

    async def test_pending_subscription_invoice_named_correctly(self):
        """Висеть может и счёт на подписку — показываем его, а не «доп. устройства»."""
        module, services = load_device_slots_module()
        services.payment_service.get_pending_payment.return_value = _pending(
            kind="subscription", tariff_id=5, extra_devices=0, final_amount=399.0
        )

        call = _call("slots_pay:card:1")
        await module.pay_slots_handler(call, state=AsyncMock(), bot=AsyncMock())

        text = call.message.edit_text.call_args[0][0]
        self.assertIn("подписку", text)
        self.assertNotIn("доп. устройства (0 шт.)", text)


class CancelInvoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_returns_to_purchase_screen_with_same_quantity(self):
        module, services = load_device_slots_module()
        services.payment_service.cancel_pending_payment.return_value = _pending()
        services.device_slot_service.quote.return_value = _quote(slots=3)

        call = _call("slots_cancel_invoice:3")
        await module.cancel_slot_invoice(call)

        services.payment_service.cancel_pending_payment.assert_awaited_once_with(42)
        services.device_slot_service.quote.assert_awaited_once_with(42, 3)

        # Человек снова может получить ссылку Platega — ради этого и отменял.
        _, kwargs = call.message.edit_text.call_args
        callbacks = _callback_data(kwargs["reply_markup"])
        self.assertIn("slots_pay:platega:3", callbacks)
        buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn("💳 Оплатить 200.00 ₽", [button.text for button in buttons])

    async def test_cancel_without_pending_does_not_crash(self):
        """Счёт мог оплатиться или отмениться сам, пока человек жал кнопку."""
        module, services = load_device_slots_module()
        services.payment_service.cancel_pending_payment.return_value = None
        services.device_slot_service.quote.return_value = _quote(slots=1)

        call = _call("slots_cancel_invoice:1")
        await module.cancel_slot_invoice(call)

        call.answer.assert_awaited_once()
        self.assertIn("не найден", call.answer.call_args[0][0])

    async def test_cancel_with_broken_callback_data_falls_back_to_one_slot(self):
        module, services = load_device_slots_module()
        services.payment_service.cancel_pending_payment.return_value = _pending()
        services.device_slot_service.quote.return_value = _quote(slots=1)

        call = _call("slots_cancel_invoice")
        await module.cancel_slot_invoice(call)

        services.device_slot_service.quote.assert_awaited_once_with(42, 1)


if __name__ == "__main__":
    unittest.main()
