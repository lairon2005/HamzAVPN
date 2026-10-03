# tgbot/handlers/user/device_slots.py
"""
Докупка дополнительных устройств в середине оплаченного периода.

Пара к экрану «Мои устройства»: там человек освобождает слот, удаляя чужой или
старый телефон, здесь — покупает ещё один, когда удалять нечего.

Цена считается по остатку срока подписки (см. device_pricing): слот всё равно
сгорит вместе с ней, поэтому платить за 12 месяцев вперёд в последний месяц
подписки человек не должен — как и получать годовой слот по цене месяца.
"""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from loader import config, logger
from tgbot.keyboards.inline import (
    back_to_main_menu_keyboard,
    slot_invoice_keyboard,
    slot_purchase_keyboard,
)
from tgbot.services import device_slot_service, payment, payment_service
from utils.telegram_ui import replace_message_text

device_slots_router = Router()


def _plural_devices(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "устройство"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "устройства"
    return "устройств"


def _render_quote(quote) -> str:
    """Карточка докупки: что есть сейчас, что станет и сколько это стоит."""
    current_limit = quote.base_limit + quote.current_extra
    return (
        "➕ <b>Дополнительные устройства</b>\n\n"
        f"Сейчас доступно: <b>{current_limit}</b> {_plural_devices(current_limit)}\n"
        f"Станет: <b>{quote.total_limit}</b> {_plural_devices(quote.total_limit)}\n\n"
        f"💰 К оплате: <b>{quote.price:.0f} ₽</b>\n"
        f"<i>{quote.slots} × {quote.price_per_slot} ₽ за оставшиеся "
        f"{quote.remaining_days} дн. подписки</i>\n\n"
        "При продлении подписки доп. устройства автоматически войдут в счёт "
        "и будут стоить столько же за месяц.\n"
        "<i>Трафик общий на аккаунт — от количества устройств он не растёт.</i>"
    )


async def _show_purchase_screen(call: CallbackQuery, slots: int) -> None:
    quote = await device_slot_service.quote(call.from_user.id, slots)

    if not quote.ok:
        await replace_message_text(
            call.message, quote.error, reply_markup=back_to_main_menu_keyboard()
        )
        return

    await replace_message_text(
        call.message,
        _render_quote(quote),
        reply_markup=slot_purchase_keyboard(quote.slots, quote.available, quote.total_limit),
    )


@device_slots_router.callback_query(F.data == "buy_slots")
async def buy_slots_handler(call: CallbackQuery):
    await call.answer()
    await _show_purchase_screen(call, slots=1)


@device_slots_router.callback_query(F.data.startswith("slots_qty:"))
async def change_slots_quantity(call: CallbackQuery):
    await call.answer()
    try:
        slots = int(call.data.split(":", 1)[1])
    except ValueError:
        slots = 1
    await _show_purchase_screen(call, slots)


async def _show_pending_invoice(call: CallbackQuery, pending, slots: int) -> None:
    """Показывает висящий счёт и даёт его отменить.

    Локальная отмена освобождает создание нового счета.
    """
    is_devices_invoice = getattr(pending, "kind", "subscription") == "devices"
    if is_devices_invoice:
        subject = f"доп. устройства ({pending.extra_devices} шт.)"
    else:
        subject = "подписку"

    payment_url = pending.payment_url

    await replace_message_text(
        call.message,
        f"⏳ <b>У вас уже есть неоплаченный счёт</b> на {subject}.\n\n"
        f"Сумма: <b>{pending.final_amount:.0f} ₽</b>\n\n"
        "Оплатите его — или отмените, чтобы выставить новый счёт "
        "и выбрать другой способ оплаты.",
        reply_markup=slot_invoice_keyboard(payment_url, slots),
    )


@device_slots_router.callback_query(F.data.startswith("slots_cancel_invoice"))
async def cancel_slot_invoice(call: CallbackQuery):
    """Отменяет висящий счёт и возвращает на экран докупки — с тем же количеством."""
    try:
        slots = int(call.data.split(":", 1)[1])
    except (IndexError, ValueError):
        slots = 1

    cancelled = await payment_service.cancel_pending_payment(call.from_user.id)

    if cancelled:
        # Отмена локальная: подтвержденную позже оплату все равно зачисляем.
        await call.answer(
            "✅ Счёт отменён. Не оплачивайте старую ссылку — выставьте новый счёт.",
            show_alert=True,
        )
    else:
        await call.answer(
            "Неоплаченный счёт не найден — возможно, он уже оплачен или отменён.",
            show_alert=True,
        )

    await _show_purchase_screen(call, slots)


@device_slots_router.callback_query(F.data.startswith("slots_pay:"))
async def pay_slots_handler(call: CallbackQuery, state: FSMContext, bot: Bot):
    """Создаёт счёт на докупку слотов (kind='devices' — подписку не продлевает)."""
    await call.answer()

    user_id = call.from_user.id
    parts = call.data.split(":")  # ['slots_pay', 'card'|'sbp', '<slots>']
    method = parts[1] if len(parts) > 1 else "card"
    try:
        slots = int(parts[2])
    except (IndexError, ValueError):
        slots = 1

    # Один неоплаченный счёт на пользователя: иначе вебхук по старому счёту
    # начислит слоты поверх новых, и человек заплатит дважды за одно и то же.
    pending = await payment_service.get_pending_payment(user_id)
    if pending:
        await _show_pending_invoice(call, pending, slots)
        return

    # Пересчитываем на сервере: между показом экрана и нажатием цена могла
    # измениться (прошли сутки, админ поправил тариф), а платить человек должен
    # ровно за то, что получит.
    quote = await device_slot_service.quote(user_id, slots)
    if not quote.ok:
        await replace_message_text(
            call.message, quote.error, reply_markup=back_to_main_menu_keyboard()
        )
        return

    from database import user_repo
    user = await user_repo.get(user_id)
    description = f"Дополнительные устройства ({quote.slots} шт., {quote.remaining_days} дн.)"

    try:
        payment_url, provider_payment_id = await payment_service.create_checkout(
            user_id=user_id, tariff_id=None, original_amount=quote.price, final_amount=quote.price,
            description=description, return_url=f"https://t.me/{(await bot.get_me()).username}",
            source='bot', kind='devices', extra_devices=quote.slots,
            metadata={'kind': 'devices', 'slots': str(quote.slots)},
        )
    except Exception as e:
        logger.error(
            f"Ошибка создания платежа за устройства: user={user_id}, slots={quote.slots}: {e}",
            exc_info=True,
        )
        await replace_message_text(
            call.message,
            "❌ Не удалось создать счёт. Попробуйте позже или обратитесь в поддержку.",
            reply_markup=back_to_main_menu_keyboard(),
        )
        return

    logger.info(
        f"Device slots payment created: user={user_id}, slots={quote.slots}, "
        f"amount={quote.price}, days_left={quote.remaining_days}, provider_id={provider_payment_id}"
    )

    sent_message = await replace_message_text(
        call.message,
        f"🧾 <b>Счёт на {quote.slots} доп. {_plural_devices(quote.slots)}</b>\n\n"
        f"Сумма: <b>{quote.price:.0f} ₽</b>\n"
        f"После оплаты лимит вырастет до <b>{quote.total_limit}</b> "
        f"{_plural_devices(quote.total_limit)} — сразу, без перевыпуска ключа.",
        reply_markup=slot_invoice_keyboard(payment_url, quote.slots),
    )
    await state.update_data(payment_message_id=sent_message.message_id)
