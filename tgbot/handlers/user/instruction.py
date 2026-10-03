# tgbot/handlers/user/instruction.py (Полная, обновленная версия)

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InputFile, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Импортируем клавиатуру для кнопки "Назад"
from tgbot.keyboards.inline import os_client_keyboard
from loader import config, logger # Импортируем конфиг, чтобы взять оттуда ID видео

instruction_router = Router(name="instruction")




# --- 2. Универсальная функция для показа инструкции ---
async def show_instruction_message(event: types.Message | types.CallbackQuery):
    """
    Показывает пользователю видео и текстовую инструкцию по подключению.
    """
    # ID вашего видео-файла, который вы предварительно загрузили
    # Лучше хранить его в .env и загружать через config
    VIDEO_FILE_ID = config.tg_bot.instruction_video_id # <-- Нужно будет добавить в config.py
    
    # Новый, упрощенный текст инструкции
    text = (
        "📲 <b>Инструкция по подключению</b>\n\n"
        "0️⃣ <b>Установите приложение Happ</b>\n"
        "Выберите своё устройство из кнопок ниже и установите приложение.\n\n"
        "1️⃣ <b>Перейдите в «🛜Подключиться»</b>\n"
        "В главном меню нажмите кнопку <b>«🛜 Подключиться»</b>.\n\n"
        "2️⃣ <b>Откройте страницу подписки</b>\n"
        "Нажмите кнопку <b>«🔗 Открыть страницу подписки»</b> — откроется страница, "
        "где нужно выбрать своё приложение: подписка добавится в Happ автоматически.\n\n"
        "3️⃣ <b>Подключитесь</b>\n"
        "В приложении Happ выберите сервер и нажмите кнопку подключения. Готово!\n\n"
        "❓ <b>Не работает авто-импорт?</b>\n"
        "Скопируйте ссылку из раздела «🛜 Подключиться» и добавьте её вручную через "
        "<b>Настройки → Подписки → Добавить</b> в приложении Happ.\n"
    )
    
    chat_id = event.from_user.id
    
    # --- Логика отправки ---
    if isinstance(event, types.CallbackQuery):
        # Если это колбэк, сначала нужно ответить на него
        await event.answer()
        # И удаляем старое сообщение, чтобы отправить новое с видео
        try: await event.message.delete()
        except: pass
    
    # Отправляем видео по его file_id, а текст - в подписи (caption)
    try:
        if VIDEO_FILE_ID:
            await event.bot.send_video(
                chat_id=chat_id,
                video=VIDEO_FILE_ID,
                caption=text,
                reply_markup=os_client_keyboard()
            )
        else: # Если видео не задано, отправляем только текст
            await event.bot.send_message(
                chat_id=chat_id,
                text=text + "\n\n<i>Видео-инструкция скоро появится.</i>",
                reply_markup=os_client_keyboard()
            )
    except Exception as e:
        logger.error(f"Failed to send instruction video: {e}")
        # Если не удалось отправить видео (например, ID неверный), шлем только текст
        await event.bot.send_message(chat_id=chat_id, text=text, reply_markup=os_client_keyboard())


# --- 3. Хендлеры для команды и кнопки ---
@instruction_router.message(Command("instruction"))
async def instruction_command_handler(message: types.Message):
    await show_instruction_message(message)

@instruction_router.callback_query(F.data == "instruction_info")
async def instruction_callback_handler(call: types.CallbackQuery):
    await show_instruction_message(call)