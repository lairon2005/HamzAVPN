"""Цена платёжной кнопки совпадает с серверным счётом, включая скидки и устройства."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from test_broadcast_promo import load_payment_handlers_module, _fsm_state
from test_device_slots_handler import _load_inline_keyboards, _call, load_device_slots_module, _quote, _pending


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def _priced_payment_buttons(markup):
    return [button for button in _buttons(markup) if "Оплатить" in button.text]


def _payment_module():
    module, deps = load_payment_handlers_module()
    keyboards = _load_inline_keyboards()
    module.tariff_slots_keyboard = keyboards.tariff_slots_keyboard
    module.effective_price = keyboards.effective_price
    module.format_quota = keyboards.format_quota
    deps.user_repo.get.return_value = SimpleNamespace(
        subscription_end_date=datetime.now() + timedelta(days=10), extra_devices=0,
    )
    return module, deps, keyboards


def _tariff(**overrides):
    values = dict(id=7, name="<Квартал>", price=499.99, loyalty_price=349.50,
                  duration_days=90, data_limit_gb=100, is_active=True, is_highlighted=False)
    values.update(overrides)
    return SimpleNamespace(**values)


class SubscriptionPaymentPriceTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_and_platega_invoice_share_discounted_total_with_devices(self):
        module, deps, _ = _payment_module()
        tariff = _tariff()
        state = _fsm_state()
        state.get_data.return_value = {"discount": 25, "promo_code": "<SALE>"}
        call = _call("tpay_7_2")
        bot = AsyncMock()
        bot.get_me.return_value = SimpleNamespace(username="ExampleBot")
        module.payment_service.create_checkout.return_value = ("https://pay.platega.io/payment", "payment-id")

        await module._show_slots_step(call, state, tariff, slots=2)
        preview_markup = call.message.edit_text.call_args.kwargs["reply_markup"]
        preview = _priced_payment_buttons(preview_markup)
        # 349.50 × 0.75 -> 262.12, плюс 2 × 49 × 3 -> 294.00.
        self.assertEqual([button.text for button in preview], ["💳 Оплатить 556.12 ₽"])
        self.assertEqual(preview[0].callback_data, "tpay_7_2")

        await module._create_and_send_payment(call, state, bot, tariff, slots=2)
        request = module.payment_service.create_checkout.call_args.kwargs
        self.assertEqual(request["final_amount"], 556.12)
        self.assertEqual(request["original_amount"], 643.50)
        self.assertEqual(request["extra_devices"], 2)
        text = call.message.edit_text.call_args.args[0]
        markup = call.message.edit_text.call_args.kwargs["reply_markup"]
        paid = _priced_payment_buttons(markup)
        self.assertEqual([button.text for button in paid], ["💳 Оплатить 556.12 ₽"])
        self.assertEqual(paid[0].url, "https://pay.platega.io/payment")
        for detail in ("90 дней", "100 ГБ/мес", "Устройств: <b>7</b>", "556.12 ₽"):
            self.assertIn(detail, text)
        self.assertIn("&lt;Квартал&gt;", text)
        self.assertIn("&lt;SALE&gt;", text)
        self.assertNotIn("<SALE>", text)
        self.assertIn("documents", [button.callback_data for button in _buttons(markup)])

    async def test_slots_from_callback_are_clamped_before_button_and_checkout(self):
        module, deps, _ = _payment_module()
        deps.user_repo.get.return_value = None
        tariff = _tariff(price=129, loyalty_price=None, duration_days=30)
        state = _fsm_state()
        call = _call("tpay_7_999")
        bot = AsyncMock()
        bot.get_me.return_value = SimpleNamespace(username="ExampleBot")
        module.payment_service.create_checkout.return_value = ("https://pay.platega.io/payment", "payment-id")

        await module._show_slots_step(call, state, tariff, slots=999)
        button = _priced_payment_buttons(call.message.edit_text.call_args.kwargs["reply_markup"])[0]
        self.assertEqual(button.text, "💳 Оплатить 374.00 ₽")
        self.assertEqual(button.callback_data, "tpay_7_5")
        await module._create_and_send_payment(call, state, bot, tariff, slots=999)
        self.assertEqual(module.payment_service.create_checkout.call_args.kwargs["final_amount"], 374.00)
        self.assertEqual(module.payment_service.create_checkout.call_args.kwargs["extra_devices"], 5)

    async def test_pending_invoice_keeps_its_saved_amount_after_tariff_price_changes(self):
        module, deps, _ = _payment_module()
        deps.tariff_repo.get_by_id.return_value = _tariff(price=9999)
        pending = _pending(kind="subscription", tariff_id=7, final_amount=277.23,
                           original_amount=349.50, discount_percent=20, promo_code="<PROMO>")
        call = _call("select_tariff_7")

        await module._show_pending_invoice(call, pending)
        markup = call.message.edit_text.call_args.kwargs["reply_markup"]
        button = _priced_payment_buttons(markup)[0]
        self.assertEqual(button.text, "💳 Оплатить 277.23 ₽")
        self.assertEqual(button.url, pending.payment_url)
        text = call.message.edit_text.call_args.args[0]
        self.assertIn("277.23 ₽", text)
        self.assertIn("&lt;PROMO&gt;", text)
        self.assertIn("documents", [button.callback_data for button in _buttons(markup)])
        module.payment_service.create_checkout.assert_not_awaited()

    async def test_unavailable_cashier_keeps_priced_retry_and_documents_without_url(self):
        module, _, _ = _payment_module()
        state = _fsm_state()
        call = _call("tpay_7_0")
        bot = AsyncMock()
        bot.get_me.return_value = SimpleNamespace(username="ExampleBot")
        module.payment_service.create_checkout.side_effect = RuntimeError("cashier unavailable")

        await module._create_and_send_payment(call, state, bot, _tariff(), slots=0)
        text = call.message.edit_text.call_args.args[0]
        markup = call.message.edit_text.call_args.kwargs["reply_markup"]
        self.assertIn("Оплата временно недоступна", text)
        self.assertEqual(_priced_payment_buttons(markup)[0].text, "💳 Оплатить 349.50 ₽")
        self.assertFalse(any(button.url for button in _buttons(markup)))
        self.assertIn("documents", [button.callback_data for button in _buttons(markup)])


class AdditionalDevicePaymentPriceTests(unittest.IsolatedAsyncioTestCase):
    async def test_purchase_button_uses_current_server_quote(self):
        module, services = load_device_slots_module()
        services.device_slot_service.quote.return_value = _quote(price=117.35)
        call = _call("slots_qty:2")
        await module._show_purchase_screen(call, slots=2)
        markup = call.message.edit_text.call_args.kwargs["reply_markup"]
        button = _priced_payment_buttons(markup)[0]
        self.assertEqual(button.text, "💳 Оплатить 117.35 ₽")
        self.assertEqual(button.callback_data, "slots_pay:platega:2")

    async def test_successful_device_invoice_and_checkout_use_fresh_quote_amount(self):
        module, services = load_device_slots_module()
        services.device_slot_service.quote.return_value = _quote(price=117.35)
        services.payment_service.create_checkout = AsyncMock(return_value=("https://pay.platega.io/payment", "payment-id"))
        call = _call("slots_pay:platega:2")
        call.message.message_id = 7
        state = AsyncMock()
        bot = AsyncMock()
        bot.get_me.return_value = SimpleNamespace(username="ExampleBot")
        await module.pay_slots_handler(call, state=state, bot=bot)
        request = services.payment_service.create_checkout.call_args.kwargs
        self.assertEqual(request["final_amount"], 117.35)
        self.assertEqual(request["extra_devices"], 2)
        markup = call.message.edit_text.call_args.kwargs["reply_markup"]
        button = _priced_payment_buttons(markup)[0]
        self.assertEqual(button.text, "💳 Оплатить 117.35 ₽")
        self.assertEqual(button.url, "https://pay.platega.io/payment")

    async def test_pending_device_button_uses_saved_invoice_not_current_quote(self):
        module, services = load_device_slots_module()
        services.payment_service.get_pending_payment.return_value = _pending(final_amount=123.45)
        call = _call("slots_pay:sbp:1")  # Совместимость старых сообщений.
        await module.pay_slots_handler(call, state=AsyncMock(), bot=AsyncMock())
        markup = call.message.edit_text.call_args.kwargs["reply_markup"]
        self.assertEqual(_priced_payment_buttons(markup)[0].text, "💳 Оплатить 123.45 ₽")
        services.device_slot_service.quote.assert_not_awaited()


class TariffSelectionPriceTests(unittest.TestCase):
    def test_tariff_list_keeps_kopecks_and_duration_in_discounted_price(self):
        keyboards = _load_inline_keyboards()
        markup = keyboards.tariffs_keyboard([_tariff()], promo_procent=25, user_has_active_sub=True)
        text = _buttons(markup)[0].text
        self.assertIn("262.12 ₽", text)
        self.assertIn("90 дней", text)
        self.assertIn("100 ГБ/мес", text)

    def test_platega_step_has_one_real_total_without_unsupported_payment_choices(self):
        keyboards = _load_inline_keyboards()
        markup = keyboards.payment_method_choice_keyboard(7, slots=2, total_amount=556.12)
        payment_buttons = _priced_payment_buttons(markup)
        self.assertEqual([button.text for button in payment_buttons], ["💳 Оплатить 556.12 ₽"])
        self.assertEqual(payment_buttons[0].callback_data, "tpay_7_2")
        self.assertIn("documents", [button.callback_data for button in _buttons(markup)])
