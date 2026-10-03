"""Ответы на кнопки управления картой из ранее отправленных сообщений."""
from aiogram import Router, F
from aiogram.types import CallbackQuery
from tgbot.keyboards.inline import back_to_main_menu_keyboard
from utils.telegram_ui import replace_message_text

payment_methods_router = Router()

@payment_methods_router.callback_query((F.data == 'manage_card') | F.data.startswith('pm_'))
async def manage_card_handler(call: CallbackQuery):
    await call.answer()
    await replace_message_text(call.message,
        '💳 Оплата через Platega\n\nКаждый заказ оплачивается отдельно. '
        'Выберите тариф в меню для продления подписки.',
        reply_markup=back_to_main_menu_keyboard())
