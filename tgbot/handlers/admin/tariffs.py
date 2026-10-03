from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from tgbot.states.tariff_states import TariffFSM
from aiogram.types import Message, CallbackQuery
from loader import logger

from tgbot.filters.admin import IsAdmin
from database import tariff_repo
from tgbot.keyboards.inline import (tariffs_list_keyboard, single_tariff_manage_keyboard,
                                    confirm_delete_tariff_keyboard, cancel_fsm_keyboard,
                                    tariff_highlight_choice_keyboard)

admin_tariffs_router = Router()
admin_tariffs_router.message.filter(IsAdmin())
admin_tariffs_router.callback_query.filter(IsAdmin())


# --- Хелпер для показа карточки тарифа ---
async def show_tariff_card(call: CallbackQuery, tariff_id: int):
    tariff = await tariff_repo.get_by_id(tariff_id)
    if not tariff:
        await call.message.edit_text("Тариф не найден.")
        return

    status = "Активен ✅" if tariff.is_active else "Отключен ❌"
    limit_str = "Безлимит" if not tariff.data_limit_gb else f"{tariff.data_limit_gb} ГБ/мес."
    loyalty_str = f"{tariff.loyalty_price} RUB" if tariff.loyalty_price else "не задана"
    highlight_str = "⭐ да" if tariff.is_highlighted else "нет"
    text = (
        f"<b>Управление тарифом:</b> «{tariff.name}»\n\n"
        f"<b>ID:</b> <code>{tariff.id}</code>\n"
        f"<b>Цена:</b> {tariff.price} RUB\n"
        f"<b>Срок:</b> {tariff.duration_days} дн.\n"
        f"<b>Лимит трафика:</b> {limit_str}\n"
        f"<b>Цена навсегда (лояльти):</b> {loyalty_str}\n"
        f"<b>Выбор большинства:</b> {highlight_str}\n"
        f"<b>Статус:</b> {status}"
    )
    await call.message.edit_text(
        text, reply_markup=single_tariff_manage_keyboard(tariff.id, tariff.is_active, tariff.is_highlighted)
    )


# --- Основное меню управления тарифами ---
@admin_tariffs_router.callback_query(F.data == "admin_tariffs_menu")
async def tariffs_menu(call: CallbackQuery):
    tariffs = await tariff_repo.get_all()
    await call.message.edit_text(
        "<b>💳 Управление тарифами</b>\n\nВыберите тариф для редактирования или добавьте новый.",
        reply_markup=tariffs_list_keyboard(list(tariffs))
    )

# --- Показ карточки конкретного тарифа ---
@admin_tariffs_router.callback_query(F.data.startswith("admin_manage_tariff_"))
async def manage_single_tariff(call: CallbackQuery):
    tariff_id = int(call.data.split("_")[3])
    await show_tariff_card(call, tariff_id)


# --- Включение / Отключение тарифа ---
@admin_tariffs_router.callback_query(F.data.startswith("admin_toggle_tariff_"))
async def toggle_tariff_status(call: CallbackQuery):
    tariff_id = int(call.data.split("_")[3])
    tariff = await tariff_repo.get_by_id(tariff_id)
    if tariff:
        new_status = not tariff.is_active
        await tariff_repo.update_field(tariff_id, 'is_active', new_status)
        await call.answer(f"Статус изменен на {'Активен' if new_status else 'Отключен'}")
        await show_tariff_card(call, tariff_id)


# --- Включение / Отключение подсветки "выбор большинства" ---
@admin_tariffs_router.callback_query(F.data.startswith("admin_toggle_highlight_"))
async def toggle_tariff_highlight(call: CallbackQuery):
    tariff_id = int(call.data.split("_")[3])
    tariff = await tariff_repo.get_by_id(tariff_id)
    if tariff:
        new_value = not tariff.is_highlighted
        await tariff_repo.update_field(tariff_id, 'is_highlighted', new_value)
        await call.answer("Подсветка включена" if new_value else "Подсветка выключена")
        await show_tariff_card(call, tariff_id)


# --- Блок удаления тарифа ---
@admin_tariffs_router.callback_query(F.data.startswith("admin_delete_tariff_"))
async def delete_tariff_confirm(call: CallbackQuery):
    tariff_id = int(call.data.split("_")[3])
    await call.message.edit_text(
        "Вы уверены, что хотите удалить этот тариф? Это действие необратимо.",
        reply_markup=confirm_delete_tariff_keyboard(tariff_id)
    )

@admin_tariffs_router.callback_query(F.data.startswith("admin_confirm_delete_tariff_"))
async def delete_tariff_finish(call: CallbackQuery):
    tariff_id = int(call.data.split("_")[4])
    await tariff_repo.delete_by_id(tariff_id)
    await call.answer("Тариф успешно удален", show_alert=True)
    await tariffs_menu(call) # Возвращаемся к списку тарифов


# --- Блок добавления нового тарифа (FSM) ---
# Шаги 1-3 обязательные (название/цена/срок), шаги 4-6 — новые поля Тарифов 2.0,
# все опциональные (можно пропустить вводом "-", тогда останутся NULL/False).
@admin_tariffs_router.callback_query(F.data == "admin_add_tariff")
async def add_tariff_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(TariffFSM.add_name)
    await call.message.edit_text("<b>Шаг 1/6:</b> Введите название нового тарифа (например, 'Промо-тариф на неделю').",
                                reply_markup=cancel_fsm_keyboard("admin_tariffs_menu"))

@admin_tariffs_router.message(TariffFSM.add_name)
async def add_tariff_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(TariffFSM.add_price)
    await message.answer("<b>Шаг 2/6:</b> Теперь введите цену тарифа в рублях (например, 99 или 99.9).")

@admin_tariffs_router.message(TariffFSM.add_price)
async def add_tariff_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Ошибка. Введите корректное число для цены.")
        return
    await state.update_data(price=price)
    await state.set_state(TariffFSM.add_duration)
    await message.answer("<b>Шаг 3/6:</b> Введите срок действия тарифа в днях (например, 7 или 30).")

@admin_tariffs_router.message(TariffFSM.add_duration)
async def add_tariff_duration(message: Message, state: FSMContext):
    try:
        duration = int(message.text)
    except ValueError:
        await message.answer("Ошибка. Введите целое число для количества дней.")
        return

    await state.update_data(duration=duration)
    await state.set_state(TariffFSM.add_data_limit)
    await message.answer(
        "<b>Шаг 4/6:</b> Введите лимит трафика в ГБ/месяц (0 = безлимит), "
        "или отправьте «-», чтобы оставить безлимит."
    )

@admin_tariffs_router.message(TariffFSM.add_data_limit)
async def add_tariff_data_limit(message: Message, state: FSMContext):
    raw = message.text.strip()
    if raw == "-":
        data_limit_gb = None
    else:
        try:
            data_limit_gb = int(raw)
            if data_limit_gb < 0:
                raise ValueError
        except ValueError:
            await message.answer("Ошибка. Введите целое неотрицательное число ГБ (0 = безлимит) или «-», чтобы пропустить.")
            return

    await state.update_data(data_limit_gb=data_limit_gb)
    await state.set_state(TariffFSM.add_loyalty_price)
    await message.answer(
        "<b>Шаг 5/6:</b> Введите «цену навсегда» (лоялти-цену при непрерывном "
        "продлении) в рублях, или отправьте «-», чтобы не задавать её."
    )

@admin_tariffs_router.message(TariffFSM.add_loyalty_price)
async def add_tariff_loyalty_price(message: Message, state: FSMContext):
    raw = message.text.strip()
    if raw == "-":
        loyalty_price = None
    else:
        try:
            loyalty_price = float(raw.replace(",", "."))
        except ValueError:
            await message.answer("Ошибка. Введите число (цену в рублях) или «-», чтобы пропустить.")
            return

    await state.update_data(loyalty_price=loyalty_price)
    await state.set_state(TariffFSM.add_highlight)
    await message.answer(
        "<b>Шаг 6/6:</b> Пометить тариф как «⭐ выбор большинства»?",
        reply_markup=tariff_highlight_choice_keyboard()
    )

@admin_tariffs_router.callback_query(F.data.in_(["tariff_highlight_yes", "tariff_highlight_no"]), TariffFSM.add_highlight)
async def add_tariff_highlight_finish(call: CallbackQuery, state: FSMContext):
    is_highlighted = call.data == "tariff_highlight_yes"
    data = await state.get_data()
    new_tariff = await tariff_repo.add(
        name=data['name'],
        price=data['price'],
        duration_days=data['duration'],
        data_limit_gb=data.get('data_limit_gb'),
        loyalty_price=data.get('loyalty_price'),
        is_highlighted=is_highlighted,
    )
    await state.clear()
    await call.answer()
    await call.message.edit_text(f"✅ Новый тариф «{new_tariff.name}» успешно создан!")


# --- Блок редактирования существующего тарифа (FSM) ---
# Общий хендлер для входа в режим редактирования
@admin_tariffs_router.callback_query(F.data.startswith("admin_edit_tariff_"))
async def edit_tariff_start(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    field_to_edit = parts[3]
    tariff_id = int(parts[4])

    field_map = {
        "name": "название",
        "price": "цену (число)",
        "duration": "срок в днях (целое число)",
        "datalimit": "лимит трафика в ГБ (0 = безлимит; «-» чтобы сделать безлимитным)",
        "loyalty": "цену навсегда (лояльти) в рублях («-» чтобы убрать лоялти-цену)",
    }
    prompt_text = f"Введите новое(ую) {field_map[field_to_edit]} для тарифа <code>{tariff_id}</code>"

    await state.set_state(TariffFSM.edit_field)
    await state.update_data(tariff_id=tariff_id, field_to_edit=field_to_edit)
    await call.message.edit_text(prompt_text, reply_markup=cancel_fsm_keyboard(f"admin_manage_tariff_{tariff_id}"))

@admin_tariffs_router.message(TariffFSM.edit_field)
async def edit_tariff_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    tariff_id = data['tariff_id']
    field = data['field_to_edit']
    raw_value = message.text.strip()

    # Ключ FSM-поля -> реальное имя колонки в модели Tariff
    db_field_map = {
        "name": "name",
        "price": "price",
        "duration": "duration_days",
        "datalimit": "data_limit_gb",
        "loyalty": "loyalty_price",
    }
    db_field = db_field_map.get(field)
    if db_field is None:
        await message.answer("Неизвестное поле для редактирования.")
        await state.clear()
        return

    # Валидация и приведение типов
    try:
        if field == 'price':
            new_value = float(raw_value.replace(",", "."))
        elif field == 'duration':
            new_value = int(raw_value)
        elif field == 'datalimit':
            new_value = None if raw_value == "-" else int(raw_value)
        elif field == 'loyalty':
            new_value = None if raw_value == "-" else float(raw_value.replace(",", "."))
        else:
            new_value = raw_value
    except ValueError:
        await message.answer("Неверный формат данных. Попробуйте еще раз.")
        return

    await tariff_repo.update_field(tariff_id, db_field, new_value)
    await state.clear()
    await message.answer("✅ Данные тарифа успешно обновлены!")
