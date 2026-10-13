# tgbot/handlers/user/traffic.py
"""
Докупка трафика в середине оплаченного периода.

Пара к «Докупить устройство» (device_slots.py): тот же сценарий — степпер,
счёт без тарифа (kind='traffic'), отмена висящего счёта. Цена считается по
остатку срока подписки: докупленные ГБ всё равно сгорят вместе с ней.

Докупленный трафик — это прибавка к ежемесячной квоте, а не разовый остаток:
пока подписка жива, лимит = квота + докупленное, и он сбрасывается раз в месяц
вместе с остальным.
"""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from loader import logger
from tgbot.keyboards.inline import (
    back_to_main_menu_keyboard,
    traffic_invoice_keyboard,
    traffic_purchase_keyboard,
)
from tgbot.services import payment_service, traffic_service
from tgbot.services.payment_service import PendingPaymentError
from utils.telegram_ui import replace_message_text

traffic_router = Router()


def _render_quote(quote) -> str:
    """Карточка докупки: что есть, что станет и сколько это стоит."""
    return (
        "📊 <b>Дополнительный трафик</b>\n\n"
        f"Сейчас доступно: <b>{quote.current_limit_gb} ГБ/мес</b>\n"
        f"Станет: <b>{quote.total_limit_gb} ГБ/мес</b> (+{quote.added_gb} ГБ)\n\n"
        f"💰 К оплате: <b>{quote.price:.0f} ₽</b>\n"
        f"<i>{quote.packs} × {quote.price_per_pack} ₽ за оставшиеся "
        f"{quote.remaining_days} дн. подписки</i>\n\n"
        "При продлении подписки докупленный трафик автоматически войдёт в счёт "
        "и будет стоить столько же за месяц.\n"
        "<i>Квота общая на аккаунт и обновляется раз в месяц.</i>"
    )


async def _show_purchase_screen(call: CallbackQuery, packs: int) -> None:
    quote = await traffic_service.quote(call.from_user.id, packs)

    if not quote.ok:
        await replace_message_text(
            call.message, quote.error, reply_markup=back_to_main_menu_keyboard()
        )
        return

    await replace_message_text(
        call.message,
        _render_quote(quote),
        reply_markup=traffic_purchase_keyboard(quote.packs, quote.available, quote.total_limit_gb, total_amount=quote.price),
    )


@traffic_router.callback_query(F.data == "buy_traffic")
async def buy_traffic_handler(call: CallbackQuery):
    await call.answer()
    await _show_purchase_screen(call, packs=1)


@traffic_router.callback_query(F.data.startswith("traffic_qty:"))
async def change_traffic_quantity(call: CallbackQuery):
    await call.answer()
    try:
        packs = int(call.data.split(":", 1)[1])
    except ValueError:
        packs = 1
    await _show_purchase_screen(call, packs)


async def _show_pending_invoice(call: CallbackQuery, pending, packs: int) -> None:
    """Показывает висящий счёт и даёт его отменить (иначе — тупик на ~30 минут)."""
    kind = getattr(pending, "kind", "subscription")
    if kind == "traffic":
        subject = f"доп. трафик ({pending.extra_traffic_gb} ГБ)"
    elif kind == "devices":
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
        reply_markup=traffic_invoice_keyboard(payment_url, packs, total_amount=pending.final_amount),
    )


@traffic_router.callback_query(F.data.startswith("traffic_cancel_invoice"))
async def cancel_traffic_invoice(call: CallbackQuery):
    """Отменяет висящий счёт и возвращает на экран докупки с тем же количеством."""
    try:
        packs = int(call.data.split(":", 1)[1])
    except (IndexError, ValueError):
        packs = 1

    cancelled = await payment_service.cancel_pending_payment(call.from_user.id)

    if cancelled:
        # Отмена локальная: старая ссылка ещё какое-то время рабочая.
        await call.answer(
            "✅ Счёт отменён. Не оплачивайте старую ссылку — выставьте новый счёт.",
            show_alert=True,
        )
    else:
        await call.answer(
            "Неоплаченный счёт не найден — возможно, он уже оплачен или отменён.",
            show_alert=True,
        )

    await _show_purchase_screen(call, packs)


@traffic_router.callback_query(F.data.startswith("traffic_pay:"))
async def pay_traffic_handler(call: CallbackQuery, state: FSMContext, bot: Bot):
    """Создаёт счёт на докупку трафика (kind='traffic' — подписку не продлевает)."""
    await call.answer()

    user_id = call.from_user.id
    parts = call.data.split(":")  # ['traffic_pay', 'card'|'sbp'|'sberpay', '<packs>']
    try:
        packs = int(parts[2])
    except (IndexError, ValueError):
        packs = 1

    # Один неоплаченный счёт на пользователя: иначе вебхук по старому счёту
    # начислит ГБ поверх новых, и человек заплатит дважды за одно и то же.
    pending = await payment_service.get_pending_payment(user_id)
    if pending:
        await _show_pending_invoice(call, pending, packs)
        return

    # Пересчёт на сервере: между показом экрана и нажатием цена могла измениться.
    quote = await traffic_service.quote(user_id, packs)
    if not quote.ok:
        await replace_message_text(
            call.message, quote.error, reply_markup=back_to_main_menu_keyboard()
        )
        return

    description = f"Дополнительный трафик (+{quote.added_gb} ГБ, {quote.remaining_days} дн.)"

    try:
        payment_url, provider_payment_id = await payment_service.create_checkout(
            user_id=user_id, tariff_id=None, original_amount=quote.price, final_amount=quote.price,
            description=description, return_url=f"https://t.me/{(await bot.get_me()).username}",
            source='bot', kind='traffic', extra_traffic_gb=quote.added_gb,
            metadata={'kind': 'traffic', 'gb': str(quote.added_gb)},
        )
    except PendingPaymentError:
        pending = await payment_service.get_pending_payment(user_id)
        if pending:
            await _show_pending_invoice(call, pending, packs)
        return
    except Exception as e:
        logger.error(
            f"Ошибка создания платежа за трафик: user={user_id}, packs={quote.packs}: {e}",
            exc_info=True,
        )
        await replace_message_text(
            call.message,
            "❌ Не удалось создать счёт. Попробуйте позже или обратитесь в поддержку.",
            reply_markup=back_to_main_menu_keyboard(),
        )
        return

    sent_message = await replace_message_text(
        call.message,
        f"🧾 <b>Счёт на +{quote.added_gb} ГБ трафика</b>\n\n"
        f"Сумма: <b>{quote.price:.0f} ₽</b>\n"
        f"После оплаты лимит вырастет до <b>{quote.total_limit_gb} ГБ/мес</b> — "
        "сразу, без перевыпуска ключа.",
        reply_markup=traffic_invoice_keyboard(payment_url, quote.packs, total_amount=quote.price),
    )
    await state.update_data(payment_message_id=sent_message.message_id)
