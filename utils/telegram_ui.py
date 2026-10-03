from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message


async def replace_message_text(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """Редактирует сообщение, а для фото/нередактируемого сообщения создаёт новое."""
    try:
        result = await message.edit_text(text, reply_markup=reply_markup)
        return result if isinstance(result, Message) else message
    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            return message

        # Сначала создаём замену. Если ошибка вызвана HTML/клавиатурой, повторная
        # отправка тоже упадёт, но исходное сообщение останется у пользователя.
        replacement = await message.answer(text, reply_markup=reply_markup)
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        return replacement
