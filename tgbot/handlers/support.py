from aiogram import Router, F, Bot, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from html import escape

from loader import logger, config
from tgbot.services import support_service, user_service
from database import user_repo
from tgbot.keyboards.inline import close_support_chat_keyboard, main_menu_keyboard
from tgbot.states.support_states import SupportFSM

support_router = Router()



# =============================================================================
# --- БЛОК 1: ВХОД В РЕЖИМ ПОДДЕРЖКИ ---
# =============================================================================

def support_intro_keyboard():
    """Клавиатура с одной кнопкой 'Начать диалог'."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Начать диалог с поддержкой", callback_data="confirm_start_support")
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def support_resolved_keyboard():
    """
    Клавиатура после решения обращения в поддержку — момент счастья (§7.5):
    CTA на реферальную программу вместо простого "назад в меню".
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="🎁 Поделиться и получить дни", callback_data="referral_program")
    builder.button(text="⬅️ Главное меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()

async def show_support_intro(event: types.Message | types.CallbackQuery):
    """Показывает приветственное сообщение поддержки."""
    text = (
        "💬 <b>Поддержка</b>\n\n"
        "Личный тикет HamzAVPN: вопросы по подключению, подписке, оплате, возвратам и учетным данным. "
        "Ответ придет в этот диалог с ботом; вступать в группу не требуется.\n\n"
        "Нажмите кнопку ниже, чтобы начать диалог. Документы и тарифы всегда доступны по /documents."
    )
    reply_markup = support_intro_keyboard()

    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=reply_markup)
    else:
        await event.answer(text, reply_markup=reply_markup)

@support_router.message(Command("support"))
async def support_command_handler(message: types.Message, state: FSMContext):
    """Хендлер для команды /support."""
    await state.clear()
    await show_support_intro(message)

@support_router.callback_query(F.data == "support_chat_start")
async def support_callback_handler(call: types.CallbackQuery, state: FSMContext):
    """Хендлер для кнопки 'Поддержка' из главного меню."""
    await call.answer()
    await state.clear()
    await show_support_intro(call)


# =============================================================================
# --- БЛОК 2: УПРАВЛЕНИЕ ДИАЛОГОМ (ДЛЯ ПОЛЬЗОВАТЕЛЯ) ---
# =============================================================================

@support_router.callback_query(F.data == "confirm_start_support")
async def start_support_chat_confirmed(call: types.CallbackQuery, state: FSMContext, bot: Bot):
    """Создает тему и переводит пользователя в режим чата после подтверждения."""
    await state.clear()
    user_id = call.from_user.id

    # Поддержка доступна даже при первом обращении через публичные документы.
    # Строка пользователя должна существовать до сохранения идентификатора тикета.
    await user_service.register_or_get(user_id, call.from_user.full_name, call.from_user.username)

    topic_id = await support_service.get_topic_id(user_id)

    if topic_id:
        text = "Вы уже находитесь в чате с поддержкой. Просто продолжайте писать сообщения ниже."
    else:
        try:
            topic = await bot.create_forum_topic(
                chat_id=config.tg_bot.support_chat_id,
                name=f"Тикет #{user_id} | @{call.from_user.username or 'NoUsername'}"
            )
            await support_service.save_topic(user_id, topic.message_thread_id)
            await bot.send_message(
                chat_id=config.tg_bot.support_chat_id,
                message_thread_id=topic.message_thread_id,
                text=f"👤 Пользователь <b>{escape(call.from_user.full_name)}</b> (ID: <code>{user_id}</code>) открыл новый тикет."
            )
            text = "Вы начали диалог с поддержкой. Опишите вашу проблему, и вам скоро ответят."
        except Exception as e:
            logger.error(f"Failed to create support topic for user {user_id}: {e}", exc_info=True)
            await call.answer("Не удалось создать чат с поддержкой. Попробуйте позже.", show_alert=True)
            return

    await state.set_state(SupportFSM.in_chat)

    await call.message.edit_text(text, reply_markup=close_support_chat_keyboard())
    await call.answer()


@support_router.callback_query(F.data == "support_chat_close", SupportFSM.in_chat)
async def close_support_chat_by_user(call: types.CallbackQuery, state: FSMContext, bot: Bot):
    """Обрабатывает закрытие диалога со стороны пользователя."""
    await state.clear()
    topic_id = await support_service.get_topic_id(call.from_user.id)
    if topic_id:
        await bot.send_message(
            chat_id=config.tg_bot.support_chat_id,
            message_thread_id=topic_id,
            text="💬 Пользователь завершил диалог."
        )
    await support_service.close_topic(call.from_user.id)
    await call.message.edit_text(
        "✅ <b>Диалог с поддержкой завершен.</b>\n\nВы вернулись в главное меню.",
        reply_markup=support_resolved_keyboard()
    )


@support_router.message(SupportFSM.in_chat, Command("cancel"))
async def cancel_support_from_command(message: types.Message, state: FSMContext, bot: Bot):
    """Позволяет пользователю выйти из чата поддержки командой /cancel."""
    # Создаем фейковый колбэк, чтобы вызвать логику кнопки "Завершить диалог"
    fake_call = types.CallbackQuery(id="fake_call", from_user=message.from_user, chat_instance="", message=message)
    await close_support_chat_by_user(fake_call, state, bot)
    await message.delete() # Удаляем сообщение /cancel


@support_router.message(
    SupportFSM.in_chat,
    ~F.text.regexp(r"^/(?:documents|privacy|terms|tariffs)(?:@\w+)?(?:\s|$)"),
)
async def process_message_in_support_chat(message: Message, state: FSMContext, bot: Bot):
    """
    Обрабатывает все сообщения от пользователя, находящегося в состоянии чата с поддержкой.
    Если это команда - выходит из чата.
    Если это обычное сообщение - пересылает в поддержку.
    """

    # 1. Проверяем, является ли сообщение командой
    if message.text and message.text.startswith('/'):
        # Если это команда, выходим из режима поддержки
        await state.clear()
        await message.answer(
        "<b>Вы вышли из режима чата с поддержкой.</b>\n\n"
        "Ваша команда не была выполнена. Пожалуйста, отправьте ее еще раз, чтобы бот мог ее обработать.",
        reply_markup=main_menu_keyboard()
    )

        return # Завершаем выполнение хендлера

    # 2. Если это не команда, обрабатываем как обычное сообщение для поддержки
    topic_id = await support_service.get_topic_id(message.from_user.id)
    if not topic_id:
        await state.clear()
        await message.answer("Произошла ошибка. Пожалуйста, начните чат с поддержкой заново.", reply_markup=main_menu_keyboard())
        return

    # Пересылаем сообщение админу
    await message.forward(
        chat_id=config.tg_bot.support_chat_id,
        message_thread_id=topic_id
    )


# =============================================================================
# --- БЛОК 3: ОБРАБОТКА ОТВЕТОВ ИЗ ГРУППЫ ПОДДЕРЖКИ (ДЛЯ АДМИНА) ---
# =============================================================================

@support_router.message(F.chat.id == config.tg_bot.support_chat_id, F.message_thread_id, Command("close"))
async def admin_close_topic_command(message: types.Message, bot: Bot):
    """Закрывает тикет по команде /close от админа."""
    user_to_reply = await support_service.get_user_by_topic(message.message_thread_id)
    if not user_to_reply:
        await message.reply("Не удалось найти пользователя для этой темы.")
        return

    try:
        await bot.send_message(
            user_to_reply.user_id,
            "Оператор поддержки завершил ваш диалог. Если у вас возникнут новые вопросы, вы всегда можете открыть новый тикет.\n"
            "Вы были возвращены в главное меню!",
            reply_markup=support_resolved_keyboard()
        )
    except Exception as e:
        logger.warning(f"Could not send '/close' notification to user {user_to_reply.user_id}: {e}")

    await support_service.close_topic(user_to_reply.user_id)
    await bot.close_forum_topic(config.tg_bot.support_chat_id, message.message_thread_id)
    await message.reply("✅ Тикет успешно закрыт.")


@support_router.message(F.chat.id == config.tg_bot.support_chat_id, F.message_thread_id)
async def admin_reply_to_user_from_topic(message: types.Message, bot: Bot):
    """
    Пересылает ответ админа пользователю с припиской "Ответ от поддержки".
    """
    # Игнорируем сообщения от самого бота
    if message.from_user.id == bot.id:
        return

    user_to_reply = await support_service.get_user_by_topic(message.message_thread_id)
    if not user_to_reply:
        return

    try:
        # Формируем нашу "шапку" для сообщения
        header = "💬 <b>Ответ от поддержки:</b>\n"

        # 1. Если админ отправил только текст
        if message.text:
            # Просто соединяем нашу шапку и текст админа
            await bot.send_message(
                chat_id=user_to_reply.user_id,
                text=header + message.text,
                reply_markup=message.reply_markup # Копируем кнопки, если они были
            )

        # 2. Если админ отправил фото, видео, документ и т.д. с подписью (caption)
        elif message.caption:
            # Копируем сообщение, но изменяем его подпись, добавляя нашу шапку
            await message.copy_to(
                chat_id=user_to_reply.user_id,
                caption=header + message.caption,
            )

        # 3. Если админ отправил медиа БЕЗ подписи
        else:
            # Сначала отправляем нашу "шапку" отдельным сообщением
            await bot.send_message(
                chat_id=user_to_reply.user_id,
                text=header
            )
            # А затем копируем медиафайл без изменений
            await message.copy_to(chat_id=user_to_reply.user_id)

    except Exception as e:
        logger.error(f"Failed to send admin's reply to user {user_to_reply.user_id}: {e}", exc_info=True)
        await message.reply(f"❌ Не удалось отправить ответ пользователю. Ошибка: {e}")
