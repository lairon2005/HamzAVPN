# tgbot/handlers/user/profile.py
from aiogram import Router, F, types, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from datetime import datetime

from loader import logger
from remnawave.client import RemnawaveClient
from tgbot.keyboards.inline import profile_keyboard, back_to_main_menu_keyboard, keys_screen_keyboard
from tgbot.services import qr_generator, profile_service, payment_service
from utils.subscription_view import build_connection_view
from tgbot.services.utils import format_traffic, get_user_attribute
from utils.telegram_ui import replace_message_text

profile_router = Router()


# --- 1. Создаем ОБЩУЮ функцию для показа профиля ---
async def show_profile_logic(event: Message | CallbackQuery, remnawave: RemnawaveClient, bot: Bot,
                              show_referral_cta: bool = False,
                              target_message: Message | None = None):
    """
    Универсальная логика для отображения профиля пользователя.
    Адаптирована для вызова из webhook_handler.

    show_referral_cta — момент счастья (§7.5): после успешной оплаты/продления
    показываем кнопку "Поделиться и получить дни" (см. webhook_handlers.py).
    """

    # Получаем ID пользователя и объект бота из события
    user_id = event.from_user.id


    # Получаем информацию о пользователе через сервис
    profile_data = await profile_service.get_profile(user_id)
    if profile_data.error:
        text = profile_data.error
        reply_markup = back_to_main_menu_keyboard()
        if isinstance(event, types.CallbackQuery):
            await replace_message_text(
                target_message or event.message,
                text,
                reply_markup=reply_markup,
            )
        else:
            await event.answer(text, reply_markup=reply_markup)
        return

    vpn_user = profile_data.vpn_user

    # --- Форматирование данных (ваш код) ---
    status = get_user_attribute(vpn_user, 'status', 'unknown')
    expire_ts = get_user_attribute(vpn_user, 'expire')
    expire_date = datetime.fromtimestamp(expire_ts).strftime('%d.%m.%Y %H:%M') if expire_ts else "Никогда"

    used_traffic = get_user_attribute(vpn_user, 'used_traffic', 0)
    data_limit = get_user_attribute(vpn_user, 'data_limit')
    used_traffic_str = format_traffic(used_traffic)
    data_limit_str = "Безлимит" if data_limit == 0 or data_limit is None else format_traffic(data_limit)

    # ВАРИАНТ A: subscription_url из Remnawave уже полный URL — используем как есть,
    # без префикса домена (sub-прокси удалён).
    sub_url = get_user_attribute(vpn_user, 'subscription_url', '')
    full_sub_url = sub_url

    profile_text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🔑 <b>Статус:</b> <code>{status}</code>\n"
        f"🗓 <b>Подписка активна до:</b> <code>{expire_date}</code>\n\n"
        f"📊 <b>Трафик:</b>\n"
        f"Использовано: <code>{used_traffic_str}</code>\n"
        f"Лимит: <code>{data_limit_str}</code>\n\n"
        f"🔗 <b>Ссылка для подписки (нажмите, чтобы скопировать):</b>\n<code>{full_sub_url}</code>"
    )

    # --- Отправка ответа с QR-кодом ---
    try:
        qr_code_stream = qr_generator.create_qr_code(full_sub_url)
        qr_photo = types.BufferedInputFile(qr_code_stream.getvalue(), filename="qr.png")

        # --- ИСПРАВЛЕННАЯ ЛОГИКА ОТПРАВКИ ---

        # Если это было нажатие на кнопку, пытаемся удалить старое сообщение
        if isinstance(event, types.CallbackQuery):
            try:
                await (target_message or event.message).delete()
            except TelegramBadRequest:
                pass # Игнорируем, если не получилось

        # Отправляем новое сообщение с фото напрямую через объект bot
        await bot.send_photo(
            chat_id=user_id,
            photo=qr_photo,
            caption=profile_text,
            reply_markup=profile_keyboard(full_sub_url, show_referral_cta=show_referral_cta)
        )

    except Exception as e:
        logger.error(f"Error sending profile with QR: {e}", exc_info=True)
        # Если что-то пошло не так, отправляем просто текст

        # --- ИСПРАВЛЕННАЯ ЛОГИКА ОТПРАВКИ ---
        await bot.send_message(
            chat_id=user_id,
            text=profile_text,
            reply_markup=profile_keyboard(full_sub_url, show_referral_cta=show_referral_cta)
        )


# --- 2. Хендлеры для команды и кнопки ---
@profile_router.message(Command("profile"))
async def profile_command_handler(message: Message, remnawave: RemnawaveClient, bot: Bot):
    await show_profile_logic(message, remnawave, bot)

@profile_router.callback_query(F.data == "my_profile")
async def my_profile_callback_handler(call: CallbackQuery, remnawave: RemnawaveClient, bot: Bot):
    await call.answer()
    loading_message = await replace_message_text(
        call.message,
        "⏳ <b>Загружаем профиль…</b>\n\n"
        "Получаем актуальные данные из VPN-панели."
    )
    await show_profile_logic(
        call,
        remnawave,
        bot,
        target_message=loading_message,
    )

# --- Хендлер для "Мои ключи" — упрощённый экран с импортом ---
@profile_router.callback_query(F.data == "my_keys")
async def my_keys_handler(call: CallbackQuery):
    """Показывает URL подключения и трафик из Remnawave."""
    loading_message = await replace_message_text(
        call.message,
        "⏳ <b>Загружаем данные подключения…</b>\n\n"
        "Получаем ссылку и данные о трафике из VPN-панели."
    )

    profile_data = await profile_service.get_profile(call.from_user.id)
    if profile_data.error or not profile_data.vpn_user:
        error_text = profile_data.error or "Не удалось получить данные подписки."
        await replace_message_text(
            loading_message,
            error_text,
            reply_markup=back_to_main_menu_keyboard(),
        )
        return

    connection = build_connection_view(profile_data.vpn_user)
    full_sub_url = connection.subscription_url

    if not full_sub_url:
        error_text = "❌ Ссылка для подключения недоступна. Обратитесь в поддержку."
        await replace_message_text(
            loading_message,
            error_text,
            reply_markup=back_to_main_menu_keyboard(),
        )
        return

    await replace_message_text(
        loading_message,
        connection.text,
        reply_markup=keys_screen_keyboard(full_sub_url),
    )


# --- Хендлер для истории платежей пользователя ---
@profile_router.callback_query(F.data == "my_payments")
async def my_payments_handler(call: CallbackQuery):
    """Показывает историю платежей пользователя."""
    await call.answer("Загружаю историю платежей...")
    user_id = call.from_user.id

    payments = await payment_service.get_user_payments(user_id)

    if not payments:
        await replace_message_text(
            call.message,
            "💳 <b>Мои платежи</b>\n\nУ вас пока нет платежей.",
            reply_markup=back_to_main_menu_keyboard(),
        )
        return

    status_labels = {
        'pending': '⏳ Ожидает', 'succeeded': '✅ Оплачен', 'failed': '❌ Ошибка',
        'refunded': '💸 Возврат', 'cancelled': '🚫 Отменён'
    }

    lines = ["💳 <b>Мои платежи</b>\n"]
    for p in payments[:10]:
        label = status_labels.get(p.status, p.status)
        date_str = p.created_at.strftime('%d.%m.%Y') if p.created_at else '—'
        discount_info = f" (скидка {p.discount_percent}%)" if p.discount_percent else ""
        # Платёж за доп. устройства подписку не продлевал — без пометки человек
        # не поймёт, за что списание, и пойдёт в поддержку.
        kind_info = (
            f" · доп. устройства ({p.extra_devices} шт.)"
            if getattr(p, 'kind', 'subscription') == 'devices' else ""
        )
        lines.append(
            f"{label} | {date_str} | <b>{p.final_amount:.2f} RUB</b>{discount_info}{kind_info}"
        )

    await replace_message_text(
        call.message,
        "\n".join(lines),
        reply_markup=back_to_main_menu_keyboard(),
    )
