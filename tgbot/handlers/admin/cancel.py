# tgbot/handlers/admin/cancel.py

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from tgbot.filters.admin import IsAdmin
from tgbot.keyboards.inline import admin_main_menu_keyboard

cancel_router = Router()
cancel_router.message.filter(IsAdmin())

@cancel_router.message(Command("cancel"))
async def cancel_any_state(message: Message, state: FSMContext):
    """
    Отменяет любое FSM состояние администратора.
    """
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нет активных действий для отмены.")
        return

    await state.clear()
    await message.answer(
        "Текущее действие отменено. Вы в главном меню.",
        reply_markup=admin_main_menu_keyboard()
    )