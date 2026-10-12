# tgbot/keyboards/inline.py

from typing import List
from aiogram.types import InlineKeyboardButton, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardMarkup

# Импортируем модели только для аннотации типов, чтобы избежать циклических импортов
from db import Tariff, PromoCode, Channel
from loader import config
from tgbot.services.pricing import effective_price, format_quota


# =============================================================================
# === TELEGRAM MINI APP (TMA) — web_app-кнопки (README.md фаза 4) ===
# ВАЖНО: web_app-кнопки работают ТОЛЬКО в личных чатах с ботом — Telegram не
# поддерживает их в группах/каналах. НЕ используйте tma_web_app_button/build_tma_url
# в клавиатурах для групповых чатов (например, топик поддержки в support_chat_id).
# =============================================================================

def build_tma_url(path: str = "/tma/") -> str:
    """Строит абсолютный URL Telegram Mini App.

    Mini App расположен на основном домене; HTTPS использует стандартный порт 443.
    """
    return f"https://{config.webhook.domain}{path}"


def tma_web_app_button(text: str, path: str = "/tma/") -> InlineKeyboardButton:
    """Инлайн-кнопка, открывающая экран Mini App через WebAppInfo."""
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=build_tma_url(path)))


# Домены, на которых Telegram не примет web_app-кнопку: ему нужен публично
# резолвящийся HTTPS-хост.
_LOCAL_DOMAINS = {"", "localhost", "127.0.0.1", "0.0.0.0", "::1"}


def tma_mode_enabled() -> bool:
    """Включён ли режим Mini App для клавиатур бота (UI_MODE=tma).

    Помимо самого флага требуется настоящий домен. Это не перестраховка:
    Telegram отвергает web_app-кнопку с локальным хостом ошибкой
    BUTTON_TYPE_INVALID, причём отклоняется ВСЯ клавиатура — пользователь
    получил бы не «меню без Mini App», а вообще никакого меню. В dev-режиме
    (DOMAIN=localhost, polling — см. CLAUDE.md) это гарантированный обвал
    главного меню, поэтому там всегда откатываемся в bot-режим.
    """
    if config.tg_bot.ui_mode != "tma":
        return False
    domain = (config.webhook.domain or "").strip().lower()
    return domain not in _LOCAL_DOMAINS


# =============================================================================
# === 1. КЛАВИАТУРЫ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ (ОСНОВНОЕ МЕНЮ) ===
# =============================================================================

def main_menu_keyboard(
    has_active_sub: bool = True, has_email: bool = True
) -> InlineKeyboardMarkup:
    """
    Главная клавиатура пользователя. Условные кнопки зависят от статуса подписки и email.

    Набор пунктов одинаков в обоих режимах (UI_MODE, см. tma_mode_enabled) —
    меняется только то, куда ведёт кнопка: callback-сценарий бота или экран Mini App.
    """
    builder = InlineKeyboardBuilder()
    if tma_mode_enabled():
        builder.button(text='💎 Оплатить', web_app=WebAppInfo(url=build_tma_url('/tma/tariffs')))
        builder.button(text='🛜 Подключиться', web_app=WebAppInfo(url=build_tma_url('/tma/import')))
        builder.button(text='👥 Пригласить друга', web_app=WebAppInfo(url=build_tma_url('/tma/referral')))
        builder.button(text="💬 Поддержка", web_app=WebAppInfo(url=build_tma_url('/tma/support')))
    else:
        builder.button(text='💎 Оплатить', callback_data='buy_subscription')
        builder.button(text='🛜 Подключиться', callback_data='my_keys')
        builder.button(text='👥 Пригласить друга', callback_data='referral_program')
        builder.button(text="💬 Поддержка", callback_data="support_chat_start")
    rows = [1, 1, 2]
    builder.button(text="📄 Документы и тарифы", callback_data="documents")
    rows.append(1)
    # Ниже — пункты, которые остаются callback-сценариями бота в ОБОИХ режимах:
    #   • триал требует проверки подписки на каналы через Bot API (check_subscription),
    #     из Mini App её не сделать;
    #   • привязка email — тоже бот-сценарий (FSM).
    if not has_active_sub:
        builder.button(text="🌟 +7 дней за подписку", callback_data="start_trial_process")
        rows.append(1)
    if not has_email:
        builder.button(text="📧 Привязать Email", callback_data="link_email")
        rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


def onboarding_subscribe_keyboard(channels: List[Channel]) -> InlineKeyboardMarkup:
    """Клавиатура для первого шага онбординга — подписка на каналы."""
    builder = InlineKeyboardBuilder()
    for i, channel in enumerate(channels):
        builder.button(text=f"📢 {channel.title}", url=channel.invite_link)
    builder.button(text="✅ Я подписался, продолжить", callback_data="onboarding_check_sub")
    builder.button(text="📄 Документы и тарифы", callback_data="documents")
    builder.adjust(1)
    return builder.as_markup()


def onboarding_download_app_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для второго шага — скачать приложение Happ."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📱 iOS (App Store)", url="https://apps.apple.com/ru/app/happ-proxy-utility/id6783623643")
    builder.button(text="🤖 Android (Google Play)", url="https://play.google.com/store/apps/details?id=com.happproxy")
    builder.button(text="➡️ Приложение установлено", callback_data="onboarding_app_installed")
    builder.button(text="📄 Документы и тарифы", callback_data="documents")
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def onboarding_import_keyboard(subscription_url: str) -> InlineKeyboardMarkup:
    """Клавиатура для третьего шага — страница подписки Remnawave с импортом в приложения."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Открыть страницу подписки", url=subscription_url)
    builder.button(text="➡️ Перейти в главное меню", callback_data="back_to_main_menu")
    builder.button(text="📄 Документы и тарифы", callback_data="documents")
    builder.adjust(1)
    return builder.as_markup()


def profile_keyboard(subscription_url: str, show_referral_cta: bool = False) -> InlineKeyboardMarkup:
    """
    Клавиатура для раздела "Мой профиль" с полным набором действий.
    show_referral_cta — момент счастья (§7.5): показывать кнопку "Поделиться" сразу
    после успешной оплаты/продления (см. webhook_handlers.py::_notify_tg_user).
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Открыть страницу подписки", url=subscription_url)
    builder.button(text="🔄 Обновить", callback_data="my_profile")
    builder.button(text="📱 Мои устройства", callback_data="my_devices")
    builder.button(text="🔁 Автопродление", callback_data="autorenew")
    if show_referral_cta:
        builder.button(text="🎁 Поделиться и получить дни", callback_data="referral_program")
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def simple_profile_keyboard() -> InlineKeyboardMarkup:
    """Простая клавиатура профиля."""
    builder = InlineKeyboardBuilder()
    builder.button(text='👤 Мой профиль', callback_data='my_profile')
    return builder.as_markup()


def tariffs_keyboard(
    tariffs: list[Tariff],
    promo_procent: int = 0,
    user_has_active_sub: bool = False,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру со списком тарифов для покупки.

    user_has_active_sub: если True — тарифам с заданной loyalty_price показывается
    "цена навсегда" вместо обычной (§7.1). Скидка по промокоду применяется поверх
    уже эффективной цены.
    """
    builder = InlineKeyboardBuilder()
    for tariff in tariffs:
        base_price = effective_price(tariff, user_has_active_sub)
        is_loyalty = user_has_active_sub and bool(tariff.loyalty_price) and base_price == tariff.loyalty_price

        if promo_procent > 0:
            discounted_price = round(base_price * (1 - promo_procent / 100), 2)
            price_part = f"{discounted_price:.2f} ₽ (скидка {promo_procent}%)"
        else:
            price_part = f"{base_price:.2f} ₽"

        loyalty_mark = " · ваша цена" if is_loyalty else ""
        star = "⭐ " if tariff.is_highlighted else ""
        highlight_mark = " · выбор большинства" if tariff.is_highlighted else ""
        gb_part = format_quota(tariff.data_limit_gb)

        builder.button(
            text=f"{star}{tariff.name} · {tariff.duration_days} дней · {gb_part} — {price_part}{loyalty_mark}{highlight_mark}",
            callback_data=f"select_tariff_{tariff.id}"
        )

    if promo_procent <= 0:
        builder.button(text="🎁 У меня есть промокод", callback_data="enter_promo_code")
    builder.button(text="💳 История платежей", callback_data="my_payments")
    builder.button(text="📄 Документы и тарифы", callback_data="documents")
    builder.button(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


# =============================================================================
# === LIFECYCLE-РАССЫЛКИ (напоминания о продлении / win-back / активация) ===
# =============================================================================

def lifecycle_cta_keyboard(*buttons: tuple[str, str]) -> InlineKeyboardMarkup:
    """
    Универсальная клавиатура lifecycle-касаний: принимает список (текст, callback_data).
    Например: lifecycle_cta_keyboard(("💎 Продлить", "buy_subscription")).
    Для промо-кода переиспользуем существующий механизм apply_promo_<code>
    (см. tgbot/handlers/user/payment.py::apply_promo_from_broadcast).
    """
    builder = InlineKeyboardBuilder()
    for text, callback_data in buttons:
        builder.button(text=text, callback_data=callback_data)
    builder.adjust(1)
    return builder.as_markup()


def winback_survey_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура опроса причины ухода (win-back, волна 3, §7.3)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Дорого", callback_data="winback_survey_price")
    builder.button(text="Скорость/стабильность", callback_data="winback_survey_speed")
    builder.button(text="Больше не нужно", callback_data="winback_survey_noneed")
    builder.adjust(1)
    return builder.as_markup()


def payment_method_choice_keyboard(tariff_id: int, slots: int = 0,
                                   *, total_amount: float) -> InlineKeyboardMarkup:
    """Переход к Platega с итоговой суммой, рассчитанной обработчиком.

    Доступные способы пользователь выбирает на странице платёжного сервиса.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=f"💳 Оплатить {total_amount:.2f} ₽", callback_data=f"tpay_{tariff_id}_{slots}")
    builder.button(text="📄 Документы и тарифы", callback_data="documents")
    builder.button(text="⬅️ Назад", callback_data=f"select_tariff_{tariff_id}")
    builder.adjust(1)
    return builder.as_markup()


def tariff_slots_keyboard(tariff_id: int, slots: int, max_slots: int,
                          base_limit: int, *, total_amount: float,
                          autorenew_amount: int | None = None) -> InlineKeyboardMarkup:
    """
    Шаг чекаута «сколько устройств». Степпер −/+ и переход к оплате.

    Количество едет в callback_data, а не в FSM: человек может открыть экран
    из старого сообщения, и состояние из FSM к тому моменту уже очищено.
    """
    builder = InlineKeyboardBuilder()

    stepper = []
    if slots > 0:
        stepper.append(InlineKeyboardButton(text="➖", callback_data=f"tslots_{tariff_id}_{slots - 1}"))
    stepper.append(InlineKeyboardButton(
        text=f"📱 {base_limit + slots} устройств", callback_data="noop"
    ))
    if slots < max_slots:
        stepper.append(InlineKeyboardButton(text="➕", callback_data=f"tslots_{tariff_id}_{slots + 1}"))
    builder.row(*stepper)

    builder.row(InlineKeyboardButton(
        text=f"💳 Оплатить {total_amount:.2f} ₽", callback_data=f"tpay_{tariff_id}_{slots}"
    ))
    if autorenew_amount:
        builder.row(InlineKeyboardButton(
            text=f"🔁 С автопродлением · {autorenew_amount} ₽", callback_data=f"tauto_{tariff_id}_{slots}"
        ))
    builder.row(InlineKeyboardButton(text="📄 Документы и тарифы", callback_data="documents"))
    builder.row(InlineKeyboardButton(text="⬅️ К выбору тарифа", callback_data="buy_subscription"))
    return builder.as_markup()


def autorenew_prompt_keyboard() -> InlineKeyboardMarkup:
    """Под уведомлениями об отключении или сбое автопродления."""
    builder = InlineKeyboardBuilder()
    builder.button(text="💎 Продлить подписку", callback_data="buy_subscription")
    builder.button(text="🔁 Об автопродлении", callback_data="autorenew")
    builder.adjust(1)
    return builder.as_markup()


def autorenew_manage_keyboard() -> InlineKeyboardMarkup:
    """Под уведомлениями об успешном списании."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⚙️ Управление автопродлением", callback_data="autorenew")
    builder.adjust(1)
    return builder.as_markup()


def autorenew_screen_keyboard(status: str | None, payment_url: str | None = None) -> InlineKeyboardMarkup:
    """Экран автопродления: действия зависят от состояния подписки."""
    builder = InlineKeyboardBuilder()
    if status == 'active':
        builder.button(text="❌ Отключить автопродление", callback_data="autorenew_off")
    elif status == 'pending':
        if payment_url:
            builder.button(text="💳 Привязать счёт в банке", url=payment_url)
        builder.button(text="❌ Отменить", callback_data="autorenew_off")
    else:
        builder.button(text="💎 Выбрать тариф", callback_data="buy_subscription")
    builder.button(text="📄 Документы и тарифы", callback_data="documents")
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def autorenew_confirm_off_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, отключить", callback_data="autorenew_off_yes")
    builder.button(text="⬅️ Оставить", callback_data="autorenew")
    builder.adjust(1)
    return builder.as_markup()


def slot_purchase_keyboard(slots: int, max_slots: int, total_limit: int,
                           *, total_amount: float) -> InlineKeyboardMarkup:
    """Докупка устройств в середине оплаченного периода: степпер + оплата."""
    builder = InlineKeyboardBuilder()

    stepper = []
    if slots > 1:
        stepper.append(InlineKeyboardButton(text="➖", callback_data=f"slots_qty:{slots - 1}"))
    stepper.append(InlineKeyboardButton(text=f"📱 {total_limit} устройств", callback_data="noop"))
    if slots < max_slots:
        stepper.append(InlineKeyboardButton(text="➕", callback_data=f"slots_qty:{slots + 1}"))
    builder.row(*stepper)

    builder.row(InlineKeyboardButton(
        text=f"💳 Оплатить {total_amount:.2f} ₽", callback_data=f"slots_pay:platega:{slots}"
    ))
    builder.row(InlineKeyboardButton(text="📄 Документы и тарифы", callback_data="documents"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад к устройствам", callback_data="my_devices"))
    return builder.as_markup()


def slot_invoice_keyboard(payment_url: str | None, slots: int = 1,
                          *, total_amount: float) -> InlineKeyboardMarkup:
    """Счёт на докупку устройств: оплатить, отменить и вернуться к списку.

    Пользователь может закрыть счет локально и выбрать другой способ оплаты.
    """
    builder = InlineKeyboardBuilder()
    if payment_url:
        builder.button(text=f"💳 Оплатить {total_amount:.2f} ₽", url=payment_url)
    builder.button(text="📄 Документы и тарифы", callback_data="documents")
    builder.button(text="❌ Отменить счёт", callback_data=f"slots_cancel_invoice:{slots}")
    builder.button(text="⬅️ К устройствам", callback_data="my_devices")
    builder.adjust(1)
    return builder.as_markup()


def legal_menu_keyboard() -> InlineKeyboardMarkup:
    """Постоянный доступ к условиям, ценам и личной поддержке."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔒 Политика конфиденциальности", callback_data="privacy_policy")
    builder.button(text="📋 Пользовательское соглашение", callback_data="terms_of_service")
    builder.button(text="💎 Актуальные тарифы и цены", callback_data="public_tariffs")
    builder.button(text="💬 Личная поддержка", callback_data="support_chat_start")
    builder.button(text="📄 Все документы", callback_data="documents")
    builder.button(text="⬅️ Главное меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def keys_screen_keyboard(subscription_url: str) -> InlineKeyboardMarkup:
    """Клавиатура экрана 'Мои ключи': страница подписки, открыть в приложении, инструкция, назад."""
    builder = InlineKeyboardBuilder()
    # Ведём прямо на subscription_url: Remnawave сам отдаёт по нему sub-страницу
    # с кнопками импорта под каждое приложение — промежуточный /import не нужен.
    builder.button(text="🔗 Открыть страницу подписки", url=subscription_url)
    if tma_mode_enabled():
        builder.button(text="📲 Открыть в приложении", web_app=WebAppInfo(url=build_tma_url("/tma/import")))
        builder.button(text="📱 Мои устройства", web_app=WebAppInfo(url=build_tma_url("/tma/devices")))
    else:
        builder.button(text="📱 Мои устройства", callback_data="my_devices")
        # В tma-режиме перевыпуск живёт на экране Mini App (/tma/import):
        # держать рядом две разные кнопки с одним смыслом — верный способ
        # заставить человека перевыпустить ключ дважды подряд.
        builder.button(text="♻️ Перевыпустить ключ", callback_data="revoke_key")
    builder.button(text="📖 Инструкция", callback_data="instruction_info")
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


DEVICES_PER_PAGE = 8


def devices_keyboard(devices: list, page: int = 0, can_buy_slots: bool = False) -> InlineKeyboardMarkup:
    """
    Экран "Мои устройства": по кнопке на устройство + пагинация.

    Список нарезаем на страницы, потому что у одного аккаунта устройств может
    быть много (при неограниченном hwidDeviceLimit — сотни), а Telegram не
    покажет клавиатуру такого размера.
    """
    builder = InlineKeyboardBuilder()
    start = page * DEVICES_PER_PAGE
    chunk = devices[start:start + DEVICES_PER_PAGE]

    for device in chunk:
        builder.button(
            text=f"🗑 {device.title} · {device.app}",
            callback_data=f"dev_del:{device.key}",
        )
    builder.adjust(1)

    pages = max(1, (len(devices) + DEVICES_PER_PAGE - 1) // DEVICES_PER_PAGE)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"my_devices:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"my_devices:{page + 1}"))
        builder.row(*nav)

    if can_buy_slots:
        builder.row(InlineKeyboardButton(text="➕ Докупить устройство", callback_data="buy_slots"))

    builder.row(InlineKeyboardButton(text="🔄 Обновить", callback_data=f"my_devices:{page}"))
    builder.row(InlineKeyboardButton(text="♻️ Перевыпустить ключ", callback_data="revoke_key:dev"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_main_menu"))
    return builder.as_markup()


def device_delete_confirm_keyboard(key: str, page: int = 0) -> InlineKeyboardMarkup:
    """Подтверждение удаления: устройству придётся заново импортировать подписку."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Да, удалить", callback_data=f"dev_del_ok:{key}")
    builder.button(text="⬅️ Отмена", callback_data=f"my_devices:{page}")
    builder.adjust(1)
    return builder.as_markup()


def revoke_key_confirm_keyboard(back: str = "my_keys") -> InlineKeyboardMarkup:
    """
    Подтверждение перевыпуска ключа.

    back — куда вернуть по отмене: экран, с которого пришли ("my_keys" или
    "my_devices"). Без него отмена на экране устройств выбрасывала бы
    пользователя в другой раздел.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="♻️ Да, перевыпустить", callback_data="revoke_key_ok")
    builder.button(text="⬅️ Отмена", callback_data=back)
    builder.adjust(1)
    return builder.as_markup()


def revoked_key_keyboard(subscription_url: str) -> InlineKeyboardMarkup:
    """Экран после перевыпуска: сразу увести на импорт новой ссылки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Открыть страницу подписки", url=subscription_url)
    builder.button(text="📖 Инструкция", callback_data="instruction_info")
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def channels_subscribe_keyboard(channels: List[Channel]) -> InlineKeyboardMarkup:
    """Создает клавиатуру со ссылками на каналы и кнопкой проверки."""
    builder = InlineKeyboardBuilder()
    for i, channel in enumerate(channels):
        builder.button(text=f"Канал {i+1}: {channel.title}", url=channel.invite_link)
    builder.button(text="✅ Я подписался, проверить", callback_data="check_subscription")
    builder.button(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def close_support_chat_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для закрытия чата с поддержкой."""
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Завершить диалог", callback_data="support_chat_close")
    return builder.as_markup()

# --- 1. Клавиатура со ссылками на клиенты ---
def os_client_keyboard():
    """Создает клавиатуру со ссылками на рекомендованные клиенты для VLESS."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Мой профиль", callback_data="my_profile")
    builder.button(text="🤖 Android (Happ)", url="https://play.google.com/store/apps/details?id=com.happproxy")
    builder.button(text="🍏 iOS (Happ)", url="https://apps.apple.com/ru/app/happ-proxy-utility/id6783623643")
    builder.button(text="💻 Windows (Happ)", url="https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe")
    builder.button(text="🍎 mac OS (Happ)", url="https://apps.apple.com/ru/app/happ-proxy-utility/id6783623643")
    builder.button(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")
    builder.adjust(1) # Располагаем кнопки по одной в ряд
    return builder.as_markup()


# =============================================================================
# === 2. КЛАВИАТУРЫ ДЛЯ АДМИН-ПАНЕЛИ ===
# =============================================================================

def admin_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню админ-панели."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📈 Статистика", callback_data="admin_stats")
    builder.button(text="👤 Управление пользователями", callback_data="admin_users_menu")
    builder.button(text="📢 Управление каналами", callback_data="admin_channels_menu")
    builder.button(text="💳 Управление тарифами", callback_data="admin_tariffs_menu")
    builder.button(text="🎁 Промокоды", callback_data="admin_promo_codes")
    builder.button(text="📱 Доп. устройства", callback_data="admin_device_settings")
    builder.button(text="📤 Рассылка", callback_data="admin_broadcast")
    builder.button(text="⬅️ Выйти из админ-панели", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()

def device_settings_keyboard() -> InlineKeyboardMarkup:
    """Настройки продажи доп. устройств: цена слота, базовый лимит, потолок."""
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Цена слота", callback_data="admin_devset_extra_device_price")
    builder.button(text="📦 Базовый лимит", callback_data="admin_devset_base_device_limit")
    builder.button(text="🔝 Потолок докупки", callback_data="admin_devset_max_extra_devices")
    builder.button(text="⬅️ Назад в админ-панель", callback_data="admin_main_menu")
    builder.adjust(1)
    return builder.as_markup()


# --- 2.1. Управление пользователями ---

def user_manage_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для управления конкретным пользователем."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Добавить дни", callback_data=f"admin_add_days_{user_id}")
    builder.button(text="💳 История платежей", callback_data=f"admin_payments_{user_id}")
    builder.button(text="🔄 Сбросить ключ", callback_data=f"admin_reset_user_{user_id}")
    builder.button(text="🗑 Удалить пользователя", callback_data=f"admin_delete_user_{user_id}")
    builder.button(text="⬅️ Назад к поиску", callback_data="admin_users_menu")
    builder.adjust(1)
    return builder.as_markup()


def confirm_delete_keyboard(user_id_to_delete: int) -> InlineKeyboardMarkup:
    """Клавиатура для подтверждения удаления пользователя."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить", callback_data=f"admin_confirm_delete_user_{user_id_to_delete}")
    builder.button(text="❌ Отмена", callback_data=f"admin_show_user_{user_id_to_delete}")
    builder.adjust(1)
    return builder.as_markup()

# --- 2.2. Управление каналами (НОВОЕ) ---

def manage_channels_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для добавления/удаления каналов."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить канал", callback_data="admin_add_channel")
    builder.button(text="➖ Удалить канал", callback_data="admin_delete_channel")
    builder.button(text="⬅️ Назад в админ-меню", callback_data="admin_main_menu")
    builder.adjust(2, 1)
    return builder.as_markup()

# --- 2.3. Управление тарифами ---

def tariffs_list_keyboard(tariffs: list[Tariff]) -> InlineKeyboardMarkup:
    """Показывает список всех тарифов с кнопкой "Добавить новый"."""
    builder = InlineKeyboardBuilder()
    for tariff in tariffs:
        status_icon = "✅" if tariff.is_active else "❌"
        star = "⭐ " if tariff.is_highlighted else ""
        builder.button(
            text=f"{status_icon} {star}{tariff.name} - {tariff.price} RUB",
            callback_data=f"admin_manage_tariff_{tariff.id}"
        )
    builder.button(text="➕ Добавить новый тариф", callback_data="admin_add_tariff")
    builder.button(text="⬅️ Назад в админ-меню", callback_data="admin_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def single_tariff_manage_keyboard(tariff_id: int, is_active: bool, is_highlighted: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура для управления одним тарифом."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить название", callback_data=f"admin_edit_tariff_name_{tariff_id}")
    builder.button(text="💰 Изменить цену", callback_data=f"admin_edit_tariff_price_{tariff_id}")
    builder.button(text="⏳ Изменить срок (дни)", callback_data=f"admin_edit_tariff_duration_{tariff_id}")
    builder.button(text="📊 Изменить лимит трафика (ГБ)", callback_data=f"admin_edit_tariff_datalimit_{tariff_id}")
    builder.button(text="💎 Изменить цену навсегда (лояльти)", callback_data=f"admin_edit_tariff_loyalty_{tariff_id}")

    highlight_text = "➖ Убрать «выбор большинства»" if is_highlighted else "⭐ Пометить «выбор большинства»"
    builder.button(text=highlight_text, callback_data=f"admin_toggle_highlight_{tariff_id}")

    action_text, action_cb = ("❌ Отключить", "admin_toggle_tariff_") if is_active else ("✅ Включить", "admin_toggle_tariff_")
    builder.button(text=action_text, callback_data=f"{action_cb}{tariff_id}")

    builder.button(text="🗑️ Удалить тариф", callback_data=f"admin_delete_tariff_{tariff_id}")
    builder.button(text="⬅️ Назад к списку тарифов", callback_data="admin_tariffs_menu")
    builder.adjust(1)
    return builder.as_markup()


def tariff_highlight_choice_keyboard() -> InlineKeyboardMarkup:
    """Да/нет-выбор для пометки тарифа «⭐ выбор большинства» при создании (шаг 6/6)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐ Да, пометить", callback_data="tariff_highlight_yes")
    builder.button(text="➖ Нет", callback_data="tariff_highlight_no")
    builder.adjust(2)
    return builder.as_markup()

def confirm_delete_tariff_keyboard(tariff_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для подтверждения удаления тарифа."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить", callback_data=f"admin_confirm_delete_tariff_{tariff_id}")
    builder.button(text="❌ Нет, отмена", callback_data=f"admin_manage_tariff_{tariff_id}")
    builder.adjust(1)
    return builder.as_markup()

# --- 2.4. Управление промокодами ---

def promo_codes_list_keyboard(promo_codes: list[PromoCode]) -> InlineKeyboardMarkup:
    """Показывает список всех промокодов с кнопкой 'Удалить' и 'Добавить'."""
    builder = InlineKeyboardBuilder()
    if promo_codes:
        for code in promo_codes:
            info = []
            if code.bonus_days > 0: info.append(f"{code.bonus_days} дн.")
            if code.discount_percent > 0: info.append(f"{code.discount_percent}%")
            info.append(f"{code.uses_left}/{code.max_uses} исп.")
            builder.button(text=f"🗑️ {code.code} ({', '.join(info)})", callback_data=f"admin_delete_promo_{code.id}")
    
    builder.button(text="➕ Добавить новый промокод", callback_data="admin_add_promo")
    builder.button(text="⬅️ Назад в админ-меню", callback_data="admin_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def promo_type_keyboard() -> InlineKeyboardMarkup:
    """Предлагает выбрать тип создаваемого промокода."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🎁 Бонусные дни", callback_data="promo_type_days")
    builder.button(text="💰 Скидка (%)", callback_data="promo_type_discount")
    builder.adjust(1)
    return builder.as_markup()

# --- 2.5. Рассылка ---


# tgbot/keyboards/inline.py (или admin_keyboards.py)

def broadcast_audience_keyboard():
    """Клавиатура для выбора аудитории рассылки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Всем пользователям", callback_data="broadcast_audience_all")
    builder.button(text="⏳ Тем, кто не покупал", callback_data="broadcast_audience_never")
    builder.button(text="❌ Отмена", callback_data="admin_main_menu")
    builder.adjust(1)
    return builder.as_markup()

def broadcast_promo_keyboard():
    """Клавиатура для добавления промокода к рассылке."""
    builder = InlineKeyboardBuilder()
    # Эта кнопка будет вести в FSM для ввода промокода
    builder.button(text="🎁 Прикрепить скидку или бонусные дни", callback_data="broadcast_attach_promo")
    # Эта кнопка пропустит шаг с промокодом
    builder.button(text="➡️ Продолжить без промокода", callback_data="broadcast_skip_promo")
    builder.button(text="❌ Отмена", callback_data="admin_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def confirm_broadcast_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для подтверждения рассылки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Начать рассылку", callback_data="broadcast_start")
    builder.button(text="❌ Отмена", callback_data="admin_panel") # Изменено для единообразия
    builder.adjust(1)
    return builder.as_markup()



# =============================================================================
# === 3. УНИВЕРСАЛЬНЫЕ И СЛУЖЕБНЫЕ КЛАВИАТУРЫ ===
# =============================================================================

def back_to_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Простая клавиатура с одной кнопкой "Назад в главное меню"."""
    builder = InlineKeyboardBuilder()
    builder.button(text='⬅️ Назад в главное меню', callback_data='back_to_main_menu')
    return builder.as_markup()


def back_to_admin_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой "Назад в админ-меню"."""
    builder = InlineKeyboardBuilder()
    builder.button(text='⬅️ Назад в админ-меню', callback_data='admin_main_menu')
    return builder.as_markup()


def cancel_fsm_keyboard(back_callback_data: str) -> InlineKeyboardMarkup:
    """Универсальная клавиатура для отмены любого состояния FSM."""
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=back_callback_data)
    return builder.as_markup()

def back_to_promo_list_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для возврата к списку промокодов."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К списку промокодов", callback_data="admin_promo_codes")
    return builder.as_markup()
