"""
Админ-раздел «Доп. устройства»: цена слота, базовый лимит и потолок докупки.

Живёт в БД (app_settings), а не в .env, потому что цену правят заметно чаще,
чем выкатывают релиз, а правка .env требует refresh.sh и пересоздания
контейнеров.

Важно про базовый лимит: он же записывается в панель как часть персонального
`hwidDeviceLimit` (база + слоты). Пока у пользователя это поле пустое, на него
действует глобальный fallbackDeviceLimit панели — поэтому смена базы здесь
доезжает до людей не мгновенно, а прогоном джоба sync_device_limits (раз в час),
и только до тех, у кого есть докупленные слоты. Остальных двигает сама панель.
"""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from database import settings_repo
from loader import logger
from tgbot.filters.admin import IsAdmin
from tgbot.keyboards.inline import (
    cancel_fsm_keyboard,
    device_settings_keyboard,
)
from tgbot.services import device_slot_service
from tgbot.services.device_pricing import (
    SETTING_BASE_LIMIT,
    SETTING_MAX_EXTRA,
    SETTING_PRICE,
)
from tgbot.states.device_settings_states import DeviceSettingsFSM

admin_device_settings_router = Router()
admin_device_settings_router.message.filter(IsAdmin())
admin_device_settings_router.callback_query.filter(IsAdmin())

# Что редактируем: ключ → (заголовок, подсказка, границы допустимых значений)
EDITABLE = {
    SETTING_PRICE: ("Цена доп. устройства", "рублей в месяц за одно устройство", 1, 10_000),
    SETTING_BASE_LIMIT: ("Базовый лимит", "устройств входит в любой тариф", 1, 100),
    SETTING_MAX_EXTRA: ("Потолок докупки", "сколько устройств можно докупить сверх базы", 0, 50),
}


async def _render_menu(message: Message) -> None:
    settings = await device_slot_service.settings()
    await message.edit_text(
        "📱 <b>Дополнительные устройства</b>\n\n"
        f"<b>Цена слота:</b> {settings.price} ₽ / месяц\n"
        f"<b>Базовый лимит:</b> {settings.base_limit} устройств в тарифе\n"
        f"<b>Потолок докупки:</b> +{settings.max_extra} "
        f"(максимум {settings.base_limit + settings.max_extra} на аккаунт)\n\n"
        "Цена слота умножается на количество месяцев тарифа: тариф на 90 дней "
        f"с одним доп. устройством стоит цена тарифа + {settings.price * 3} ₽.\n\n"
        "<i>Смена базового лимита доезжает до пользователей со слотами в течение "
        "часа (джоб сверки), остальных двигает глобальная настройка панели.</i>",
        reply_markup=device_settings_keyboard(),
    )


@admin_device_settings_router.callback_query(F.data == "admin_device_settings")
async def device_settings_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _render_menu(call.message)


@admin_device_settings_router.callback_query(F.data.startswith("admin_devset_"))
async def device_settings_edit_start(call: CallbackQuery, state: FSMContext):
    key = call.data.removeprefix("admin_devset_")
    if key not in EDITABLE:
        await call.answer("Неизвестная настройка", show_alert=True)
        return

    title, hint, minimum, maximum = EDITABLE[key]
    current = await settings_repo.get_int(key, 0)

    await state.set_state(DeviceSettingsFSM.edit_value)
    await state.update_data(setting_key=key)

    await call.message.edit_text(
        f"✏️ <b>{title}</b>\n\n"
        f"Текущее значение: <b>{current}</b> ({hint}).\n\n"
        f"Введите новое число от {minimum} до {maximum}:",
        reply_markup=cancel_fsm_keyboard("admin_device_settings"),
    )


@admin_device_settings_router.message(DeviceSettingsFSM.edit_value)
async def device_settings_edit_apply(message: Message, state: FSMContext):
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
            reply_markup=cancel_fsm_keyboard("admin_device_settings"),
        )
        return

    if not (minimum <= value <= maximum):
        await message.answer(
            f"Значение должно быть от {minimum} до {maximum}. Попробуйте ещё раз:",
            reply_markup=cancel_fsm_keyboard("admin_device_settings"),
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

    # Меню показываем отдельным сообщением: предыдущее было приглашением к
    # вводу и теперь висит выше ответа пользователя — редактировать его поздно.
    menu = await message.answer("Загружаю настройки…")
    await _render_menu(menu)
