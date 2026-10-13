"""
Админ-раздел «Трафик»: базовая квота, размер и цена пакета, потолок докупки.

Живёт в БД (app_settings), по тем же причинам, что и настройки устройств:
цену правят чаще, чем выкатывают релиз.

Важно про базовую квоту: она применяется к НОВЫМ выдачам и продлениям (триал,
оплата тарифа без собственного лимита). У действующих подписчиков лимит в панели
не меняется до их следующей оплаты — это сознательно: нельзя урезать то, что
человек уже оплатил.
"""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from database import settings_repo
from loader import logger
from tgbot.filters.admin import IsAdmin
from tgbot.keyboards.inline import cancel_fsm_keyboard, traffic_settings_keyboard
from tgbot.services import traffic_service
from tgbot.services.traffic_pricing import (
    SETTING_BASE_TRAFFIC,
    SETTING_MAX_PACKS,
    SETTING_PACK_GB,
    SETTING_PACK_PRICE,
)
from tgbot.states.traffic_settings_states import TrafficSettingsFSM

admin_traffic_settings_router = Router()
admin_traffic_settings_router.message.filter(IsAdmin())
admin_traffic_settings_router.callback_query.filter(IsAdmin())

# ключ → (заголовок, подсказка, минимум, максимум)
EDITABLE = {
    SETTING_PACK_GB: ("Размер пакета", "ГБ в одном докупаемом пакете", 1, 10_000),
    SETTING_PACK_PRICE: ("Цена пакета", "рублей в месяц за один пакет", 1, 10_000),
    SETTING_MAX_PACKS: ("Потолок докупки", "сколько пакетов можно докупить сверх квоты", 0, 100),
}


async def _render_menu(message: Message) -> None:
    s = await traffic_service.settings()
    await message.edit_text(
        "📊 <b>Трафик</b>\n\n"
        f"<b>Пакет:</b> +{s.pack_gb} ГБ/мес за {s.pack_price} ₽ в месяц\n"
        f"<b>Потолок докупки:</b> {s.max_packs} пакетов (+{s.max_extra_gb} ГБ, "
        f"дополнительно к квоте тарифа)\n\n"
        f"Пакет умножается на количество месяцев тарифа: тариф на 90 дней с одним "
        f"пакетом стоит цена тарифа + {s.pack_price * 3} ₽. Докупка в середине срока — "
        "по остатку дней, но не дешевле месячной цены.\n\n"
        "<i>Квота берётся из каталога тарифов. NULL и 0 означают безлимит. "
        "Настройки пакетов не меняют уже оплаченные счета.</i>",
        reply_markup=traffic_settings_keyboard(),
    )


@admin_traffic_settings_router.callback_query(F.data == "admin_traffic_settings")
async def traffic_settings_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _render_menu(call.message)


@admin_traffic_settings_router.callback_query(F.data.startswith("admin_trafset_"))
async def traffic_settings_edit_start(call: CallbackQuery, state: FSMContext):
    key = call.data.removeprefix("admin_trafset_")
    if key not in EDITABLE:
        await call.answer("Неизвестная настройка", show_alert=True)
        return

    title, hint, minimum, maximum = EDITABLE[key]
    effective = await traffic_service.settings()
    current = {
        SETTING_BASE_TRAFFIC: effective.base_gb,
        SETTING_PACK_GB: effective.pack_gb,
        SETTING_PACK_PRICE: effective.pack_price,
        SETTING_MAX_PACKS: effective.max_packs,
    }[key]

    await state.set_state(TrafficSettingsFSM.edit_value)
    await state.update_data(setting_key=key)

    await call.message.edit_text(
        f"✏️ <b>{title}</b>\n\n"
        f"Текущее значение: <b>{current}</b> ({hint}).\n\n"
        f"Введите новое число от {minimum} до {maximum}:",
        reply_markup=cancel_fsm_keyboard("admin_traffic_settings"),
    )


@admin_traffic_settings_router.message(TrafficSettingsFSM.edit_value)
async def traffic_settings_edit_apply(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("setting_key")
    if key not in EDITABLE:
        await state.clear()
        return

    title, hint, minimum, maximum = EDITABLE[key]

    try:
        value = int((message.text or "").strip())
    except ValueError:
        await message.answer(
            f"Нужно целое число от {minimum} до {maximum}. Попробуйте ещё раз:",
            reply_markup=cancel_fsm_keyboard("admin_traffic_settings"),
        )
        return

    if not (minimum <= value <= maximum):
        await message.answer(
            f"Значение должно быть от {minimum} до {maximum}. Попробуйте ещё раз:",
            reply_markup=cancel_fsm_keyboard("admin_traffic_settings"),
        )
        return

    await settings_repo.set(key, value)
    await state.clear()
    logger.info(f"[admin] {key} изменён на {value} админом {message.from_user.id}")

    await message.answer(
        f"✅ <b>{title}</b> — теперь <b>{value}</b>.\n\n"
        "Новое значение применяется к новым счетам сразу. "
        "Уже выставленные счета оплачиваются по старой цене."
    )

    menu = await message.answer("Загружаю настройки…")
    await _render_menu(menu)
