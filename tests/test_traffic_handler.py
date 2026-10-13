"""Telegram: серверная цена, pending-счёт, отмена, конфликт checkout."""
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from test_stars_payment import _load_module
from test_device_slots_handler import _load_inline_keyboards, _config_stub


def load_handler():
    loader=types.ModuleType('loader');loader.logger=Mock();loader.config=_config_stub()
    services=types.ModuleType('tgbot.services');services.__path__=[]
    services.payment_service=SimpleNamespace(get_pending_payment=AsyncMock(return_value=None),
        cancel_pending_payment=AsyncMock(return_value=None),create_checkout=AsyncMock(return_value=('https://example.com/pay','id')))
    services.traffic_service=SimpleNamespace(quote=AsyncMock())
    services.traffic_service.quote.return_value=SimpleNamespace(ok=True,packs=2,price=98,price_per_pack=49,
        remaining_days=30,added_gb=200,current_limit_gb=500,total_limit_gb=700,available=10)
    service=types.ModuleType('tgbot.services.payment_service');service.PendingPaymentError=type('PendingPaymentError',(Exception,),{})
    stubs={'loader':loader,'tgbot.services':services,'tgbot.services.payment_service':service,
        'tgbot.keyboards.inline':_load_inline_keyboards()}
    return _load_module('traffic_handler_under_test','tgbot/handlers/user/traffic.py',stubs),services


def call(data='traffic_pay:platega:2'):
    return SimpleNamespace(data=data,from_user=SimpleNamespace(id=42),answer=AsyncMock(),
        message=SimpleNamespace(message_id=7,edit_text=AsyncMock(return_value=SimpleNamespace(message_id=7))))


class TrafficHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_invoice_uses_fresh_server_quote_and_priced_platega_button(self):
        module,services=load_handler();event=call();bot=AsyncMock();bot.get_me.return_value=SimpleNamespace(username='ExampleBot')
        await module.pay_traffic_handler(event,AsyncMock(),bot)
        args=services.payment_service.create_checkout.await_args.kwargs
        self.assertEqual((args['kind'],args['extra_traffic_gb'],args['final_amount']),('traffic',200,98))
        markup=event.message.edit_text.call_args.kwargs['reply_markup']
        self.assertEqual(markup.inline_keyboard[0][0].text,'💳 Оплатить 98.00 ₽')
        self.assertEqual(markup.inline_keyboard[0][0].url,'https://example.com/pay')

    async def test_pending_url_and_amount_come_from_saved_invoice(self):
        module,services=load_handler();event=call()
        services.payment_service.get_pending_payment.return_value=SimpleNamespace(
            kind='traffic',extra_traffic_gb=100,final_amount=49,payment_url='https://example.com/old')
        await module.pay_traffic_handler(event,AsyncMock(),AsyncMock())
        services.traffic_service.quote.assert_not_awaited()
        services.payment_service.create_checkout.assert_not_awaited()
        button=event.message.edit_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
        self.assertEqual((button.text,button.url),('💳 Оплатить 49.00 ₽','https://example.com/old'))

    async def test_ineligible_quote_does_not_create_payment(self):
        module,services=load_handler();services.traffic_service.quote.return_value=SimpleNamespace(ok=False,error='Подписка истекла')
        event=call();await module.pay_traffic_handler(event,AsyncMock(),AsyncMock())
        services.payment_service.create_checkout.assert_not_awaited()
        self.assertIn('Подписка истекла',event.message.edit_text.call_args.args[0])

    async def test_cancel_keeps_quantity_and_recalculates(self):
        module,services=load_handler();event=call('traffic_cancel_invoice:3')
        services.payment_service.cancel_pending_payment.return_value=object()
        await module.cancel_traffic_invoice(event)
        services.payment_service.cancel_pending_payment.assert_awaited_once_with(42)
        services.traffic_service.quote.assert_awaited_once_with(42,3)
        self.assertIn('Не оплачивайте старую ссылку',event.answer.call_args.args[0])

    async def test_quantity_screen_uses_priced_button(self):
        module,services=load_handler();event=call('traffic_qty:2')
        await module.change_traffic_quantity(event)
        buttons=[b for row in event.message.edit_text.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        payment=next(b for b in buttons if (b.callback_data or '').startswith('traffic_pay'))
        self.assertEqual((payment.text,payment.callback_data),('💳 Оплатить 98.00 ₽','traffic_pay:platega:2'))
