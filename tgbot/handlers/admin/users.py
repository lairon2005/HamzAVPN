# tgbot/handlers/admin/users.py (Полная, рабочая версия)

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from tgbot.states.admin_states import AdminFSM
from aiogram.types import Message, CallbackQuery
from loader import logger

# --- Фильтры и Клавиатуры ---
from tgbot.filters.admin import IsAdmin
from tgbot.keyboards.inline import user_manage_keyboard, confirm_delete_keyboard, back_to_main_menu_keyboard, back_to_admin_main_menu_keyboard

# --- Сервисы ---
from tgbot.services import user_service, subscription_service, payment_service
from remnawave.client import RemnawaveClient


admin_users_router = Router()
# Применяем фильтр админа ко всем хендлерам в этом роутере
admin_users_router.message.filter(IsAdmin())
admin_users_router.callback_query.filter(IsAdmin())


# =============================================================================
# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ---
# =============================================================================

async def show_user_card(message_or_call, user_id: int):
    """
    Отображает карточку пользователя с информацией и кнопками управления.
    Вынесена в отдельную функцию для переиспользования.
    """
    user = await user_service.get_user(user_id)
    if not user:
        text = "Пользователь не найден."
        reply_markup = back_to_main_menu_keyboard()
    else:
        sub_end_str = user.subscription_end_date.strftime('%d.%m.%Y %H:%M') if user.subscription_end_date else "Отсутствует"
        text = (
            f"<b>Пользователь найден:</b>\n\n"
            f"<b>ID:</b> <code>{user.user_id}</code>\n"
            f"<b>Username:</b> @{user.username or 'Отсутствует'}\n"
            f"<b>Имя:</b> {user.full_name}\n\n"
            f"<b>Подписка до:</b> {sub_end_str}\n"
            f"<b>VPN аккаунт:</b> <code>{user.vpn_username or 'Не создан'}</code>"
        )
        reply_markup = user_manage_keyboard(user.user_id)

    # Определяем, как ответить - изменить сообщение или отправить новое
    if isinstance(message_or_call, CallbackQuery):
        # Используем try-except на случай, если сообщение уже удалено
        try:
            await message_or_call.message.edit_text(text, reply_markup=reply_markup)
        except:
            await message_or_call.bot.send_message(message_or_call.from_user.id, text, reply_markup=reply_markup)
    else:  # если это Message
        await message_or_call.answer(text, reply_markup=reply_markup)


# =============================================================================
# --- ОСНОВНЫЕ ХЕНДЛЕРЫ УПРАВЛЕНИЯ ПОЛЬЗОВАТЕЛЯМИ ---
# =============================================================================

@admin_users_router.callback_query(F.data == "admin_users_menu")
async def users_menu(call: CallbackQuery, state: FSMContext):
    """Меню поиска пользователя."""
    await state.clear()
    await call.message.edit_text(
        "<b>👤 Управление пользователями</b>\n\n"
        "Введите ID (например: <code>123456</code> или <code>-123456</code>), @username или email:",
        reply_markup=back_to_admin_main_menu_keyboard()
    )
    await state.set_state(AdminFSM.find_user)


@admin_users_router.message(AdminFSM.find_user)
async def find_user(message: Message, state: FSMContext):
    """Находит пользователя по ID или username и показывает его карточку."""
    await state.clear()
    query = message.text.strip()

    user = await user_service.find_user(query)

    if not user:
        await message.answer("Пользователь с таким ID или username не найден в базе данных бота.\n\n"
                             "Попробуйте еще раз:")
        await state.set_state(AdminFSM.find_user)
        return

    await show_user_card(message, user.user_id)


# --- Блок добавления дней ---

@admin_users_router.callback_query(F.data.startswith("admin_add_days_"))
async def add_days_start(call: CallbackQuery, state: FSMContext):
    """Начало сценария добавления дней подписки."""
    user_id = int(call.data.split("_")[3])
    await state.update_data(user_id=user_id)

    await call.message.edit_text(f"Введите количество дней для добавления пользователю <code>{user_id}</code>:")
    await state.set_state(AdminFSM.add_days_amount)


@admin_users_router.message(AdminFSM.add_days_amount)
async def add_days_finish(message: Message, state: FSMContext, bot: Bot):
    """
    Завершение сценария добавления дней подписки.
    Продлевает подписку или создает нового пользователя в Remnawave, если его нет.
    Отправляет уведомления админу и пользователю.
    """

    # --- 1. Валидация ввода от администратора ---
    try:
        days_to_add = int(message.text)
        if days_to_add <= 0:
            await message.answer("❌ <b>Ошибка:</b> Введите целое положительное число.")
            return
    except (ValueError, TypeError):
        await message.answer("❌ <b>Ошибка:</b> Пожалуйста, введите корректное число.")
        return

    # --- 2. Получение данных и предварительная обратная связь ---
    data = await state.get_data()
    user_id = data.get("user_id")
    await state.clear()

    await message.answer(f"⏳ Продлеваю/создаю подписку для пользователя <code>{user_id}</code> на <b>{days_to_add}</b> дн...")

    # --- 3. Основная логика через сервис подписок ---
    try:
        result = await subscription_service.extend(user_id, days_to_add)

        if result.is_new_user:
            logger.info(f"Admin CREATED and subscribed VPN user '{result.username}' for {days_to_add} days.")
        else:
            logger.info(f"Admin EXTENDED subscription for VPN user '{result.username}' by {days_to_add} days.")

        # Получаем обновленные данные для отчета.
        updated_user = await user_service.get_user(user_id)
        new_sub_end_date = updated_user.subscription_end_date.strftime('%d.%m.%Y')

        # --- 4. Отправляем отчеты об успехе ---

        # Отчет для администратора
        await message.answer(
            f"✅ <b>Успешно!</b>\n\n"
            f"Пользователю <code>{user_id}</code> добавлено <b>{days_to_add}</b> дн.\n"
            f"Новая дата окончания подписки: <b>{new_sub_end_date}</b>"
        )

        # Уведомление для пользователя
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"🎉 Администратор продлил вашу подписку на <b>{days_to_add}</b> дн.!\n"
                     f"Новая дата окончания: <b>{new_sub_end_date}</b>"
            )
        except Exception as e:
            logger.warning(f"Could not send notification to user {user_id} about subscription extension: {e}")
            await message.answer("❗️Не удалось уведомить пользователя (возможно, он заблокировал бота).")

    except ValueError as e:
        await message.answer(f"❌ Ошибка: {e}")
    except Exception as e:
        logger.error(f"Admin failed to add days for user {user_id}: {e}", exc_info=True)
        await message.answer(f"❌ Произошла ошибка при взаимодействии с VPN-панелью для пользователя <code>{user_id}</code>. Проверьте логи.")

    # В любом случае (успех или ошибка) показываем админу обновленную карточку пользователя.
    await show_user_card(message, user_id)

# --- Блок удаления пользователя ---

@admin_users_router.callback_query(F.data.startswith("admin_delete_user_"))
async def delete_user_confirm(call: CallbackQuery):
    """Запрашивает подтверждение на удаление."""
    user_id = int(call.data.split("_")[3])
    await call.message.edit_text(
        f"Вы уверены, что хотите полностью удалить пользователя <code>{user_id}</code>?\n\n"
        "<b>Это действие необратимо.</b> Пользователь будет удален из VPN-панели и из базы данных бота.",
        reply_markup=confirm_delete_keyboard(user_id)
    )

@admin_users_router.callback_query(F.data.startswith("admin_confirm_delete_user_"))
async def delete_user_finish(call: CallbackQuery, remnawave: RemnawaveClient):
    await call.answer("Удаляю пользователя...")

    try:
        user_id = int(call.data.split("_")[4])

        success = await user_service.delete_user(user_id, remnawave)

        if success:
            await call.message.edit_text(f"✅ Пользователь <code>{user_id}</code> успешно удален.")
        else:
            await call.message.edit_text(f"❌ **Ошибка!**\n\nНе удалось удалить пользователя. Проверьте логи.")

    except Exception as e:
        logger.error(f"Unexpected error in delete_user_finish for user_id from call {call.data}: {e}", exc_info=True)
        await call.message.edit_text("❌ Произошла непредвиденная ошибка в процессе удаления.")

# --- Хендлер для возврата к карточке пользователя ---
@admin_users_router.callback_query(F.data.startswith("admin_show_user_"))
async def show_user_handler(call: CallbackQuery):
    """Показывает карточку пользователя (нужно для кнопки 'Отмена' при удалении)."""
    user_id = int(call.data.split("_")[3])
    await show_user_card(call, user_id)


# --- Блок истории платежей ---
@admin_users_router.callback_query(F.data.startswith("admin_payments_"))
async def admin_user_payments(call: CallbackQuery):
    """Показывает историю платежей пользователя."""
    await call.answer("Загружаю историю платежей...")
    user_id = int(call.data.split("_")[2])

    payments = await payment_service.get_user_payments(user_id)

    if not payments:
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад к пользователю", callback_data=f"admin_show_user_{user_id}")
        kb.adjust(1)
        await call.message.edit_text(
            f"💳 <b>История платежей</b> (ID: <code>{user_id}</code>)\n\n"
            "Платежей не найдено.",
            reply_markup=kb.as_markup()
        )
        return

    status_icons = {
        'pending': '⏳', 'succeeded': '✅', 'failed': '❌',
        'refunded': '💸', 'cancelled': '🚫'
    }

    lines = [f"💳 <b>История платежей</b> (ID: <code>{user_id}</code>)\n"]
    for p in payments[:15]:
        icon = status_icons.get(p.status, '❓')
        date_str = p.created_at.strftime('%d.%m.%Y %H:%M') if p.created_at else '—'
        discount_info = f" (скидка {p.discount_percent}%)" if p.discount_percent else ""
        lines.append(
            f"{icon} {date_str} — <b>{p.final_amount:.2f} RUB</b>{discount_info} [{p.source}]"
        )

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к пользователю", callback_data=f"admin_show_user_{user_id}")
    kb.adjust(1)

    await call.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
