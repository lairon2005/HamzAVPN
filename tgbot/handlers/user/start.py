# tgbot/handlers/user/start.py

from datetime import datetime
from html import escape

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
# --- Импорты ---
from loader import logger
from database import channel_repo, user_repo, stats_repo
from tgbot.services import user_service, referral_service, subscription_service, profile_service
from tgbot.services.referral_service import (
    REFERRAL_TRIAL_DAYS, REFERRER_LAUNCH_BONUS_DAYS, REFERRER_PAYMENT_BONUS_DAYS,
)
from tgbot.services.subscription import check_subscription
from utils.subscription_view import build_db_subscription_view
from tgbot.services.utils import get_user_attribute, decline_word
from utils.telegram_ui import replace_message_text
from tgbot.keyboards.inline import (
    main_menu_keyboard, back_to_main_menu_keyboard,
    onboarding_subscribe_keyboard, onboarding_download_app_keyboard,
    onboarding_import_keyboard,
)

# Создаем локальный роутер для этого файла
start_router = Router()

# Обычный триал для органических пользователей (без реферальной ссылки).
# Реферальный бонус другу — REFERRAL_TRIAL_DAYS (см. referral_service.py).
ORGANIC_TRIAL_DAYS = 7


def _days_word(n: int) -> str:
    return decline_word(n, ['день', 'дня', 'дней'])


# =============================================================================
# --- ХЕЛПЕР: ОТОБРАЖЕНИЕ ГЛАВНОГО МЕНЮ ---
# =============================================================================

async def _show_main_menu(target: Message | CallbackQuery, user_id: int, full_name: str):
    """Показывает главное меню только по данным локальной БД."""
    user = await user_repo.get(user_id)
    has_email = bool(user and user.email)
    subscription = build_db_subscription_view(
        user.subscription_end_date if user else None
    )
    has_active_sub = subscription.is_active
    safe_full_name = escape(full_name)

    text = (
        f"👋 Добро пожаловать, <b>{safe_full_name}</b>!\n\n"
        f"📋 <b>Ваша подписка:</b>\n"
        f"{subscription.status_icon} Статус: {subscription.status_label}\n"
        f"📅 Активна до: {subscription.expires_label}\n\n"
        "🛰 Ссылка подключения и трафик доступны по кнопке "
        "<b>«Подключиться»</b>."
    )

    reply_markup = main_menu_keyboard(
        has_active_sub=has_active_sub, has_email=has_email,
    )

    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=reply_markup)
        except TelegramBadRequest:
            try:
                await target.message.delete()
            except TelegramBadRequest:
                pass
            await target.message.answer(text, reply_markup=reply_markup)
    else:
        await target.answer(text, reply_markup=reply_markup)

# =============================================================================
# --- БЛОК: СТАРТ БОТА И ОНБОРДИНГ ---
# =============================================================================

@start_router.message(CommandStart())
async def process_start_command(message: Message, command: CommandObject, bot: Bot, state: FSMContext):
    """
    Единый обработчик команды /start.
    Новые пользователи проходят онбординг, существующие видят главное меню.
    """
    user_id = message.from_user.id
    full_name = message.from_user.full_name
    username = message.from_user.username

    # 1. Регистрируем или получаем пользователя
    user, created = await user_service.register_or_get(user_id, full_name, username)

    # Публичные документы и личная поддержка доступны без прохождения онбординга.
    if command and command.args == "support":
        from tgbot.handlers.support import show_support_intro
        await state.clear()
        await show_support_intro(message)
        return
    if command and command.args == "documents":
        from .legal import show_documents
        await show_documents(message, state)
        return

    # 2. Обрабатываем реферальную ссылку
    referrer_id = None
    if command and command.args and command.args.startswith('ref'):
        if created:
            try:
                potential_referrer_id = int(command.args[3:])
                if potential_referrer_id != user_id and await user_service.get_user(potential_referrer_id):
                    referrer_id = potential_referrer_id
            except (ValueError, IndexError, TypeError):
                pass

            if referrer_id:
                await state.update_data(referrer_id=referrer_id)
        else:
            await message.answer("Вы уже зарегистрированы. Реферальная ссылка работает только для новых пользователей.")

    # 3. Если пользователь новый — запускаем онбординг
    if created:
        await _start_onboarding(message, bot, state, referrer_id)
    else:
        # Существующий пользователь — главное меню со статусом подписки
        await _show_main_menu(message, user_id, full_name)


# =============================================================================
# --- ОНБОРДИНГ: ШАГ 1 — ПОДПИСКА НА КАНАЛЫ ---
# =============================================================================

async def _start_onboarding(message: Message, bot: Bot, state: FSMContext, referrer_id: int | None = None):
    """Начинает процесс онбординга для нового пользователя."""
    channels = await channel_repo.get_all()

    if not channels:
        # Если каналов нет — сразу выдаём триал и переходим к скачиванию приложения
        await _activate_and_show_download(message, bot, state, referrer_id)
        return

    trial_days = REFERRAL_TRIAL_DAYS if referrer_id else ORGANIC_TRIAL_DAYS
    welcome_text = (
        f"👋 Привет, <b>{message.from_user.full_name}</b>!\n\n"
        "Добро пожаловать в HamzAVPN — сервис защищённого соединения!\n\n"
        f"🎁 Чтобы получить <b>бесплатный пробный период на {trial_days} {_days_word(trial_days)}</b>, "
        "подпишитесь на наш канал и нажмите кнопку ниже."
    )

    await message.answer(
        welcome_text,
        reply_markup=onboarding_subscribe_keyboard(channels),
        disable_web_page_preview=True
    )


@start_router.callback_query(F.data == "onboarding_check_sub")
async def onboarding_check_subscription(call: CallbackQuery, bot: Bot, state: FSMContext):
    """Проверяет подписку на каналы и выдаёт триал."""
    user_id = call.from_user.id

    # Проверяем, не получал ли уже триал
    user = await user_service.get_user(user_id)
    if user and user.has_received_trial:
        await call.answer("Вы уже получили пробный период!", show_alert=True)
        await _show_main_menu(call, call.from_user.id, call.from_user.full_name)
        return

    is_subscribed = await check_subscription(bot, user_id)

    if not is_subscribed:
        await call.answer("Вы ещё не подписались на все каналы. Попробуйте снова.", show_alert=True)
        return

    await call.answer("✅ Отлично! Подписка подтверждена!")

    # Получаем referrer_id из FSM
    fsm_data = await state.get_data()
    referrer_id = fsm_data.get("referrer_id")

    await _activate_and_show_download(call, bot, state, referrer_id)


async def _activate_and_show_download(event: Message | CallbackQuery, bot: Bot,
                                       state: FSMContext, referrer_id: int | None = None):
    """Активирует триал/реферальный бонус и показывает шаг скачивания приложения."""
    user_id = event.from_user.id
    trial_days = REFERRAL_TRIAL_DAYS if referrer_id else ORGANIC_TRIAL_DAYS

    try:
        if referrer_id:
            # Реферальный путь: установить реферера + выдать подписку
            await referral_service.activate_new_user_referral(user_id, referrer_id, trial_days)
            try:
                await bot.send_message(
                    referrer_id,
                    f"По вашей ссылке зарегистрировался новый пользователь: {event.from_user.full_name}!\n"
                    f"🎁 Вам начислено <b>{REFERRER_LAUNCH_BONUS_DAYS} {_days_word(REFERRER_LAUNCH_BONUS_DAYS)}</b> подписки. "
                    f"Ещё +{REFERRER_PAYMENT_BONUS_DAYS} {_days_word(REFERRER_PAYMENT_BONUS_DAYS)} — после его первой оплаты."
                )
            except Exception as e:
                logger.error(f"Could not notify referrer {referrer_id}: {e}")
        else:
            # Обычный триал
            await subscription_service.activate_trial(user_id, trial_days)

        logger.info(f"Trial activated for user {user_id} (referrer={referrer_id})")

    except Exception as e:
        # Активация упала (например, Remnawave недоступен — ConnectError). Подписка
        # НЕ выдана: extend() пишет в БД только после успеха Remnawave, поэтому БД
        # осталась чистой и пользователь может безопасно повторить. Показывать экран
        # «Поздравляем» здесь НЕЛЬЗЯ — иначе обманем пользователя, триала у него нет.
        logger.error(f"Failed to activate trial for user {user_id}: {e}", exc_info=True)
        error_text = (
            "😔 <b>Не удалось активировать пробный период</b>\n\n"
            "Похоже, у нас временные технические неполадки. "
            "Пожалуйста, попробуйте ещё раз через минуту."
        )
        retry_kb = InlineKeyboardBuilder()
        retry_kb.button(text="🔄 Попробовать снова", callback_data="onboarding_check_sub")
        retry_kb.adjust(1)
        if isinstance(event, CallbackQuery):
            try:
                await event.message.edit_text(error_text, reply_markup=retry_kb.as_markup())
            except TelegramBadRequest:
                await event.message.answer(error_text, reply_markup=retry_kb.as_markup())
        else:
            await event.answer(error_text, reply_markup=retry_kb.as_markup())
        return

    # Показываем шаг 2: скачивание приложения (только при успешной активации)
    text = (
        f"🎉 <b>Поздравляем!</b> Вам предоставлен пробный период на <b>{trial_days} {_days_word(trial_days)}</b>.\n\n"
        "📲 <b>Шаг 1:</b> Скачайте приложение <b>Happ</b> для вашего устройства:"
    )

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=onboarding_download_app_keyboard())
    else:
        await event.answer(text, reply_markup=onboarding_download_app_keyboard())


# =============================================================================
# --- ОНБОРДИНГ: ШАГ 2 — СКАЧИВАНИЕ ПРИЛОЖЕНИЯ ---
# =============================================================================

@start_router.callback_query(F.data == "onboarding_app_installed")
async def onboarding_app_installed(call: CallbackQuery, bot: Bot):
    """Пользователь установил приложение — показываем кнопку импорта."""
    user_id = call.from_user.id
    loading_message = await replace_message_text(
        call.message,
        "⏳ <b>Загружаем ссылку подключения…</b>\n\n"
        "Обычно это занимает несколько секунд."
    )

    # Получаем subscription_url из профиля
    profile_data = await profile_service.get_profile(user_id)
    if profile_data.error or not profile_data.vpn_user:
        await replace_message_text(
            loading_message,
            "📲 <b>Шаг 2:</b> Подключите VPN\n\n"
            "Ваш ключ подключения ещё формируется. "
            "Перейдите в главное меню и нажмите <b>«Подключиться»</b>.",
            reply_markup=main_menu_keyboard(has_active_sub=False)
        )
        return

    # ВАРИАНТ A: subscription_url уже полный URL Remnawave — используем напрямую.
    sub_url = get_user_attribute(profile_data.vpn_user, 'subscription_url', '')
    full_sub_url = sub_url

    if full_sub_url:
        text = (
            "📲 <b>Шаг 2:</b> Подключите VPN\n\n"
            "Откройте страницу подписки по кнопке ниже и выберите своё приложение — "
            "подписка добавится в <b>Happ</b> автоматически.\n\n"
            "После подключения вы сможете пользоваться VPN!"
        )
        await replace_message_text(
            loading_message,
            text,
            reply_markup=onboarding_import_keyboard(full_sub_url),
        )
    else:
        await replace_message_text(
            loading_message,
            "📲 Ваш профиль ещё создаётся. Перейдите в главное меню и нажмите <b>«Подключиться»</b>.",
            reply_markup=main_menu_keyboard(has_active_sub=False)
        )


# =============================================================================
# --- БЛОК: ОТОБРАЖЕНИЕ РЕФЕРАЛЬНОЙ ПРОГРАММЫ ---
# =============================================================================

async def show_referral_info(message: Message, bot: Bot):
    """Вспомогательная функция для показа информации о реферальной программе (§7.5)."""
    user_id = message.from_user.id
    bot_info = await bot.get_me()
    referral_link = f"https://t.me/{bot_info.username}?start=ref{user_id}"
    ref_info = await user_service.get_referral_info(user_id)

    # --- Лидерборд месяца: топ-10 + место текущего пользователя ---
    now = datetime.now()
    leaderboard = await stats_repo.get_monthly_referral_leaderboard(now.year, now.month, limit=10)

    user_rank = None
    user_month_count = 0
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    leaderboard_lines = []
    for idx, (ref_id, cnt) in enumerate(leaderboard, start=1):
        icon = medals.get(idx, f"{idx}.")
        marker = " (вы)" if ref_id == user_id else ""
        leaderboard_lines.append(f"{icon} ID {ref_id} — {cnt} чел.{marker}")
        if ref_id == user_id:
            user_rank = idx
            user_month_count = cnt

    if user_rank is None:
        user_month_count = await stats_repo.count_user_referrals_in_month(user_id, now.year, now.month)

    leaderboard_text = "\n".join(leaderboard_lines) if leaderboard_lines else "Пока никто не приглашал в этом месяце — станьте первым!"
    place_text = f"Ваше место: {user_rank}" if user_rank else "Вы пока не в топ-10"

    share_text = (
        "Пользуюсь HamzAVPN для личного защищённого соединения. "
        f"По моей ссылке {REFERRAL_TRIAL_DAYS} {_days_word(REFERRAL_TRIAL_DAYS)} бесплатно → {referral_link}"
    )

    text = (
        "🎁 Приглашайте друзей — получайте подписку.\n"
        f"Другу: {REFERRAL_TRIAL_DAYS} {_days_word(REFERRAL_TRIAL_DAYS)} бесплатно. "
        f"Вам: {REFERRER_LAUNCH_BONUS_DAYS} {_days_word(REFERRER_LAUNCH_BONUS_DAYS)} за друга + "
        f"{REFERRER_PAYMENT_BONUS_DAYS} {_days_word(REFERRER_PAYMENT_BONUS_DAYS)} после его оплаты.\n"
        "3 оплативших друга = месяц бесплатно. Лучший за месяц получает год 🏆\n\n"
        f"Ваша ссылка: <code>{referral_link}</code>\n"
        f"Приглашено: {ref_info['referral_count']} · Начислено дней: {ref_info['bonus_days']}\n\n"
        "🏆 <b>Топ-10 этого месяца:</b>\n"
        f"{leaderboard_text}\n"
        f"📍 {place_text} ({user_month_count} чел. в этом месяце)\n\n"
        "📤 <b>Текст для пересылки другу</b> (нажмите, чтобы скопировать):\n"
        f"<code>{share_text}</code>"
    )

    # Если это колбэк, редактируем сообщение. Если команда - отправляем новое.
    if isinstance(message, CallbackQuery):
        try:
            await message.message.edit_text(text, reply_markup=back_to_main_menu_keyboard())
        except TelegramBadRequest:
            await message.message.delete()
            await message.message.answer(text, reply_markup=back_to_main_menu_keyboard())
    else:
        await message.answer(text, reply_markup=back_to_main_menu_keyboard())

# Хендлер для команды /referral
@start_router.message(Command("referral"))
async def referral_command_handler(message: Message, bot: Bot):
    await show_referral_info(message, bot)

# Хендлер для кнопки "Реферальная программа"
@start_router.callback_query(F.data == "referral_program")
async def referral_program_handler(call: CallbackQuery, bot: Bot):
    await call.answer()
    await show_referral_info(call, bot)

@start_router.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu_handler(call: CallbackQuery, state: FSMContext):
    """Возвращает пользователя в главное меню со статусом подписки."""
    await state.clear()
    await call.answer()
    await _show_main_menu(call, call.from_user.id, call.from_user.full_name)
