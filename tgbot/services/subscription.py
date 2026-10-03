# tgbot/services/subscription.py

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from database import channel_repo

async def check_subscription(bot: Bot, user_id: int) -> bool:
    """
    Проверяет, подписан ли пользователь на все каналы из БД.
    Возвращает True, если подписан на все, иначе False.
    """
    required_channels = await channel_repo.get_all()
    if not required_channels:
        return True # Если каналов в списке нет, проверка пройдена

    for channel in required_channels:
        try:
            member = await bot.get_chat_member(chat_id=channel.channel_id, user_id=user_id)
            if member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED]:
                return False # Если пользователь не подписан хотя бы на один канал
        except Exception:
            # Если бот не админ в канале или ID неверный, считаем, что пользователь не подписан
            return False

    return True # Если циклы завершились, пользователь подписан на все
