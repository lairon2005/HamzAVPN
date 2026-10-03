# tgbot/middlewares/support_timeout.py

import time
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from tgbot.states.support_states import SupportFSM # Импортируем наши состояния
from tgbot.keyboards.inline import main_menu_keyboard

# Время жизни сессии поддержки в секундах (5 минут)
SUPPORT_SESSION_TIMEOUT = 5*60 

class SupportTimeoutMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: Dict[str, Any]
    ) -> Any:
        state: FSMContext = data.get('state')
        
        # Проверяем, существует ли состояние вообще
        if state is None:
            return await handler(event, data)

        current_state = await state.get_state()
        
        # Мидлварь работает только если юзер в чате поддержки
        if current_state != SupportFSM.in_chat:
            return await handler(event, data)

        state_data = await state.get_data()
        last_activity = state_data.get('last_activity_time', 0)
        
        # Проверяем, не истекло ли время
        if time.time() - last_activity > SUPPORT_SESSION_TIMEOUT:
            await state.clear()
            
            text = (
                "<b>Ваш диалог с поддержкой был автоматически завершен</b> из-за отсутствия активности.\n\n"
                "Вы были возвращены в главное меню."
            )
            
            # Отправляем или редактируем сообщение
            if isinstance(event, CallbackQuery):
                await event.message.edit_text(text, reply_markup=main_menu_keyboard())
            else:
                await event.answer(text, reply_markup=main_menu_keyboard())
            return # Прерываем обработку текущего события

        # Если время не истекло, обновляем время последней активности
        await state.update_data(last_activity_time=time.time())
        
        # И передаем управление дальше
        return await handler(event, data)