# tgbot/filters/admin.py (исправленная версия)

from aiogram.filters import Filter
from aiogram.types import Message, CallbackQuery
from typing import Union

# Импортируем наш конфиг
from loader import config

class IsAdmin(Filter):
    def __init__(self) -> None:
        pass

    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        # Проверяем, есть ли ID пользователя В СПИСКЕ админов
        return event.from_user.id in config.tg_bot.admin_ids