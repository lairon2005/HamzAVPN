# tgbot/handlers/user/payment.py

from datetime import datetime
from html import escape

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import Command

from loader import logger, config
from database import tariff_repo, user_repo
from remnawave.client import RemnawaveClient
from tgbot.services import (
    promo_service, subscription_service, payment_service, device_slot_service,
)
from tgbot.services.promo_code_service import PromoClaimError
from tgbot.promo import get_promo_reward
from tgbot.services.pricing import effective_price, format_quota
from tgbot.services.device_pricing import build_checkout, receipt_items
from tgbot.handlers.user.profile import show_profile_logic
from tgbot.keyboards.inline import (
    cancel_fsm_keyboard,
    tariffs_keyboard,
    back_to_main_menu_keyboard,
    tariff_slots_keyboard,
)
from tgbot.services import payment
from tgbot.states.payment_states import PromoApplyFSM
from utils.telegram_ui import replace_message_text

payment_router = Router()

# =============================================================================
# --- БЛОК 1: ПОКАЗ ТАРИФОВ ---
# =============================================================================

async def _user_has_active_sub(user_id: int) -> bool:
    """Непрерывность продления (§7.1): подписка ещё активна на момент покупки."""
    user = await user_repo.get(user_id)
    return bool(user and user.subscription_end_date and user.subscription_end_date > datetime.now())


async def show_tariffs_logic(event: Message | CallbackQuery, state: FSMContext):
    """Универсальная логика для показа списка тарифов."""
    fsm_data = await state.get_data()
    discount = fsm_data.get("discount")

    active_tariffs = await tariff_repo.get_active()
    tariffs_list = list(active_tariffs) if active_tariffs else []

    user_has_active_sub = await _user_has_active_sub(event.from_user.id)

    text = "Пожалуйста, выберите тарифный план:"
    if discount:
        text = f"✅ Промокод на <b>{discount}%</b> применен!\n\n" + text

    reply_markup = tariffs_keyboard(
        tariffs_list, promo_procent=discount or 0, user_has_active_sub=user_has_active_sub
    )

    if not tariffs_list:
        text = "К сожалению, сейчас нет доступных тарифов для покупки."
        reply_markup = back_to_main_menu_keyboard()

    if isinstance(event, CallbackQuery):
        # Кнопка может висеть на сообщении рассылки с фотографией (bot.copy_message
        # копирует медиа как есть) — у такого сообщения нет text, и edit_text падает
        # с "there is no text in the message to edit". replace_message_text в этом
        # случае отправляет новое сообщение и убирает исходное.
        await replace_message_text(event.message, text, reply_markup=reply_markup)
    else:
        await event.answer(text, reply_markup=reply_markup)

@payment_router.message(Command("payment"))
async def payment_command_handler(message: Message, state: FSMContext):
    await show_tariffs_logic(message, state)

@payment_router.callback_query(F.data == "buy_subscription")
async def buy_subscription_callback_handler(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await show_tariffs_logic(call, state)

# =============================================================================
# --- БЛОК 2: ПРИМЕНЕНИЕ ПРОМОКОДА ---
# =============================================================================


@payment_router.callback_query(F.data.startswith("apply_promo_"))
async def apply_promo_from_broadcast(call: CallbackQuery, state: FSMContext):
    """
    Обрабатывает нажатие на кнопку с промокодом из рассылки.
    Применяет скидку или сразу начисляет бонусные дни.
    """
    promo_code = call.data.removeprefix("apply_promo_").strip().upper()
    if not promo_code:
        await call.answer("Ошибка в данных промокода.", show_alert=True)
        return

    user_id = call.from_user.id

    # --- Валидация промокода через сервис ---
    result = await promo_service.validate(promo_code, user_id)

    if not result.is_valid:
        await call.answer(result.error_message, show_alert=True)
        return

    reward = get_promo_reward(result.promo)
    if not reward:
        await call.answer("Промокод не содержит бонуса.", show_alert=True)
        return

    if reward.kind == "bonus_days":
        await call.answer("⏳ Начисляем бонусные дни…")
        loading_message = await call.message.answer(
            "⏳ <b>Применяем промокод…</b>\n\n"
            "Обновляем срок действия вашей подписки."
        )
        try:
            await promo_service.apply_bonus_days(user_id, result.promo, subscription_service)
            await state.clear()
            await loading_message.edit_text(
                f"✅ Промокод <code>{promo_code}</code> успешно применён!\n\n"
                f"Вам начислено <b>{reward.description}</b>.",
                reply_markup=back_to_main_menu_keyboard(),
            )
        except PromoClaimError:
            # Гонка (повторное нажатие той же кнопки из рассылки) или промокод
            # закончился между validate() и try_claim() — это не сбой Remnawave,
            # показываем понятную причину, а не "обратитесь в поддержку".
            await loading_message.edit_text(
                "❌ Этот промокод уже использован или закончился.",
                reply_markup=back_to_main_menu_keyboard(),
            )
        except Exception as e:
            logger.error(
                f"Error applying bonus promo code '{promo_code}' for user {user_id}: {e}",
                exc_info=True,
            )
            await loading_message.edit_text(
                "❌ Не удалось начислить бонусные дни. Пожалуйста, обратитесь в поддержку.",
                reply_markup=back_to_main_menu_keyboard(),
            )
        return

    # --- Применение скидки и показ тарифов ---
    try:
        await promo_service.apply(user_id, result.promo)
    except PromoClaimError:
        await call.answer("Этот промокод уже использован или закончился.", show_alert=True)
        return
    except Exception as e:
        logger.error(f"Error applying promo code '{promo_code}' for user {user_id}: {e}", exc_info=True)
        await call.answer("Произошла непредвиденная ошибка. Пожалуйста, попробуйте позже.", show_alert=True)
        return

    try:
        # Сохраняем скидку в FSM для следующего шага
        await state.set_state(None)
        await state.update_data(discount=result.promo.discount_percent, promo_code=promo_code)

        # Вызываем нашу универсальную функцию для показа тарифов со скидкой
        await show_tariffs_logic(call, state)
        await call.answer()
    except Exception as e:
        # Промокод уже захвачен (try_claim), а показать тарифы не удалось —
        # возвращаем попытку пользователю, иначе скидка сгорает молча
        # (инцидент 2026-07-31: рассылка AUGUST с фото, edit_text по caption-сообщению).
        logger.error(
            f"Failed to show discounted tariffs for promo '{promo_code}', user {user_id}: {e}",
            exc_info=True,
        )
        try:
            await promo_service.release(user_id, result.promo)
        except Exception:
            logger.error(
                f"Failed to release promo claim '{promo_code}' for user {user_id}", exc_info=True
            )
        await call.answer("Произошла непредвиденная ошибка. Пожалуйста, попробуйте позже.", show_alert=True)

async def _start_promo_input(event: Message | CallbackQuery, state: FSMContext):
    """
    Универсальная функция для начала сценария ввода промокода.
    """
    await state.set_state(PromoApplyFSM.awaiting_code)

    text = "Введите ваш промокод:"
    reply_markup = back_to_main_menu_keyboard()

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=reply_markup)
    else: # если это Message
        await event.answer(text, reply_markup=reply_markup)


@payment_router.message(Command("promo"))
async def promo_command_handler(message: Message, state: FSMContext):
    """Начинает сценарий ввода промокода по команде."""
    await _start_promo_input(message, state)


# --- ОБНОВЛЕННЫЙ хендлер для кнопки "Ввести промокод" ---
@payment_router.callback_query(F.data == "enter_promo_code")
async def enter_promo_callback_handler(call: CallbackQuery, state: FSMContext):
    """Начинает сценарий ввода промокода по кнопке."""
    await call.answer()
    await _start_promo_input(call, state)

@payment_router.message(PromoApplyFSM.awaiting_code)
async def process_promo_code(message: Message, state: FSMContext, bot: Bot, remnawave: RemnawaveClient):
    """Обрабатывает введенный промокод."""
    code = message.text.upper()
    user_id = message.from_user.id

    await message.delete() # Сразу удаляем сообщение с кодом

    # --- Валидация через сервис ---
    result = await promo_service.validate(code, user_id)
    if not result.is_valid:
        await message.answer(result.error_message)
        return

    promo = result.promo

    if promo.bonus_days > 0:
        # apply_bonus_days сама атомарно захватывает промокод (try_claim) и
        # только потом начисляет дни; при сбое extend() снимает захват обратно
        # (release_claim), так что промокод не "сгорает" и не гасится дважды
        # при повторном вводе того же кода (см. tgbot/services/promo_code_service.py).
        await state.clear()
        try:
            await promo_service.apply_bonus_days(user_id, promo, subscription_service)
            await message.answer(f"✅ Промокод успешно применен! Вам начислено <b>{promo.bonus_days} бонусных дней</b>.")
            # Показываем обновленный профиль
            await show_profile_logic(message, remnawave, bot)

        except PromoClaimError:
            await message.answer("❌ Этот промокод уже использован или закончился.")
        except Exception as e:
            logger.error(f"Failed to apply bonus days for promo code {code} for user {user_id}: {e}", exc_info=True)
            await message.answer("❌ Произошла ошибка при начислении бонусных дней. Обратитесь в поддержку.")

    elif promo.discount_percent > 0:
        try:
            await promo_service.apply(user_id, promo)
        except PromoClaimError:
            await message.answer("❌ Этот промокод уже использован или закончился.")
            return

        # Сохраняем скидку в состояние и показываем тарифы
        await state.set_state(None) # Выходим из состояния ввода промокода
        await state.update_data(discount=promo.discount_percent, promo_code=code)
        await show_tariffs_logic(message, state)

# =============================================================================
# --- БЛОК 3: ВЫБОР ТАРИФА И СОЗДАНИЕ ПЛАТЕЖА ---
# =============================================================================

async def _show_pending_invoice(call: CallbackQuery, pending) -> None:
    """Показывает пользователю уже существующий неоплаченный счёт."""
    # Счёт на докупку устройств выписывается без тарифа (kind='devices') —
    # дёргать tariff_repo с None нельзя, да и показывать нечего.
    is_devices_invoice = getattr(pending, 'kind', 'subscription') == 'devices'
    pending_tariff = None
    if pending.tariff_id:
        pending_tariff = await tariff_repo.get_by_id(pending.tariff_id)

    if is_devices_invoice:
        tariff_name = f"Доп. устройства ({pending.extra_devices} шт.)"
        tariff_days = "—"
    else:
        tariff_name = pending_tariff.name if pending_tariff else "Неизвестный"
        tariff_days = pending_tariff.duration_days if pending_tariff else "?"

    existing_url = pending.payment_url

    price_text = f"<b>{pending.final_amount:.2f} ₽</b>"
    if pending.discount_percent:
        price_text = (
            f"<s>{pending.original_amount:.2f} ₽</s>\n"
            f"Скидка {pending.discount_percent}% ({escape(pending.promo_code or 'промокод')}): <b>{pending.final_amount:.2f} ₽</b>"
        )

    kb = InlineKeyboardBuilder()
    if existing_url:
        kb.button(text=f"💳 Оплатить {pending.final_amount:.2f} ₽", url=existing_url)
    kb.button(text="📄 Документы и тарифы", callback_data="documents")
    kb.button(text="❌ Отменить платёж", callback_data="cancel_pending_payment")
    kb.button(text="⬅️ Назад", callback_data="buy_subscription")
    kb.adjust(1)

    lines = ["⏳ У вас уже есть неоплаченный счёт:\n\n", f"Тариф: <b>{escape(tariff_name)}</b>\n"]
    if not is_devices_invoice:
        lines.append(f"Срок: <b>{tariff_days} дней</b>\n")
    lines.append(f"Сумма: {price_text}\n\n")
    lines.append("Завершите оплату или отмените счёт кнопкой ниже, чтобы выбрать тариф заново.")

    await call.message.edit_text("".join(lines), reply_markup=kb.as_markup())


def _checkout_price_text(checkout, discount_percent: int | None, promo_code: str | None) -> str:
    """Описание суммы из того же расчёта, который отправляется в Platega."""
    if discount_percent:
        return (
            f"<s>{checkout.tariff_original:.2f} ₽</s>\n"
            f"Скидка {discount_percent}% ({escape(promo_code or 'промокод')}): "
            f"<b>{checkout.tariff_final:.2f} ₽</b>"
        )
    return f"<b>{checkout.tariff_final:.2f} ₽</b>"


async def _create_and_send_payment(
    call: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    tariff,
    slots: int = 0,
) -> None:
    """Создаёт платёж в Platega, сохраняет его в БД и отправляет ссылку на оплату.

    `slots` — сколько доп. устройств оплачивается вместе с тарифом. Скидка на них
    не распространяется, в чек они уходят отдельной позицией (54-ФЗ).
    """
    user_id = call.from_user.id

    fsm_data = await state.get_data()
    discount_percent = fsm_data.get("discount")
    promo_code = fsm_data.get("promo_code")
    user_has_active_sub = await _user_has_active_sub(user_id)

    original_price = effective_price(tariff, user_has_active_sub)

    device_settings = await device_slot_service.settings()
    slots = max(0, min(device_settings.max_extra, slots))
    checkout = build_checkout(
        device_settings, original_price, tariff.duration_days, slots, discount_percent or 0
    )
    price_text = _checkout_price_text(checkout, discount_percent, promo_code)
    if checkout.has_slots:
        price_text += (
            f"\n+ {checkout.slots} доп. "
            f"{'устройство' if checkout.slots == 1 else 'устройства'} "
            f"({device_settings.price} ₽ × {checkout.months} мес): "
            f"<b>{checkout.slots_cost:.2f} ₽</b>\n"
            f"Итого: <b>{checkout.total:.2f} ₽</b>"
        )

    try:
        payment_url, provider_payment_id = await payment_service.create_checkout(
            user_id=user_id, tariff_id=tariff.id,
            original_amount=checkout.original_total, final_amount=checkout.total,
            description=f"Подписка HamzAVPN: {tariff.name}",
            return_url=f"https://t.me/{(await bot.get_me()).username}",
            source='bot', promo_code=promo_code, discount_percent=discount_percent or 0,
            extra_devices=slots, metadata={'tariffId': str(tariff.id), 'slots': str(slots)},
        )
    except Exception as e:
        logger.error(
            f"Ошибка создания платежа Platega: user={user_id}, tariff={tariff.name}, "
            "Ошибка платежного шлюза",
            exc_info=True,
        )
        await call.message.edit_text(
            "❌ Оплата временно недоступна. Попробуйте позже или обратитесь в поддержку. "
            "Документы доступны кнопкой ниже.",
            reply_markup=tariff_slots_keyboard(
                tariff.id, slots, device_settings.max_extra, device_settings.base_limit,
                total_amount=checkout.total,
            ),
        )
        return

    logger.info(
        f"Payment created: user={user_id}, tariff={tariff.name}, "
        f"amount={checkout.total}, slots={slots}, original={original_price}, "
        f"discount={discount_percent or 0}%, promo={promo_code or 'none'}, "
        f"provider_id={provider_payment_id}"
    )

    payment_kb = InlineKeyboardBuilder()
    payment_kb.button(text=f"💳 Оплатить {checkout.total:.2f} ₽", url=payment_url)
    payment_kb.button(text="📄 Документы и тарифы", callback_data="documents")
    payment_kb.button(text="❌ Отменить платёж", callback_data="cancel_pending_payment")
    payment_kb.button(text="⬅️ Назад к выбору тарифа", callback_data="buy_subscription")
    payment_kb.adjust(1)

    sent_message = await call.message.edit_text(
        f"Вы выбрали тариф: <b>{escape(tariff.name)}</b>\n"
        f"Срок: <b>{tariff.duration_days} дней</b>\n"
        f"Трафик: <b>{format_quota(tariff.data_limit_gb)}</b>\n"
        f"Устройств: <b>{device_settings.base_limit + slots}</b> "
        f"({device_settings.base_limit} входят в тариф, {slots} дополнительных)\n\n"
        f"Сумма к оплате: {price_text}\n\n"
        "Оплачивая, вы принимаете Пользовательское соглашение и ознакомлены "
        "с Политикой конфиденциальности. Документы доступны в меню.\n\n"
        "Нажмите на кнопку ниже, чтобы перейти к оплате.",
        reply_markup=payment_kb.as_markup()
    )
    await state.update_data(payment_message_id=sent_message.message_id)


async def _show_slots_step(call: CallbackQuery, state: FSMContext, tariff, slots: int) -> None:
    """Шаг чекаута «сколько устройств»: сумма тарифа + слотов и степпер."""
    user_id = call.from_user.id
    fsm_data = await state.get_data()
    discount_percent = fsm_data.get("discount")
    promo_code = fsm_data.get("promo_code")
    user_has_active_sub = await _user_has_active_sub(user_id)

    original_price = effective_price(tariff, user_has_active_sub)

    device_settings = await device_slot_service.settings()
    slots = max(0, min(device_settings.max_extra, slots))
    checkout = build_checkout(
        device_settings, original_price, tariff.duration_days, slots, discount_percent or 0
    )
    price_text = _checkout_price_text(checkout, discount_percent, promo_code)

    devices_line = f"Устройств: <b>{device_settings.base_limit}</b> (входит в тариф)"
    total_line = ""
    if checkout.has_slots:
        devices_line = (
            f"Устройств: <b>{device_settings.base_limit + slots}</b> "
            f"({device_settings.base_limit} + {slots} доп.)"
        )
        total_line = (
            f"\n+ Доп. устройства: {slots} × {device_settings.price} ₽ × "
            f"{checkout.months} мес = <b>{checkout.slots_cost:.2f} ₽</b>\n"
            f"<b>Итого: {checkout.total:.2f} ₽</b>"
        )

    await call.message.edit_text(
        f"Вы выбрали тариф: <b>{escape(tariff.name)}</b>\n"
        f"Срок: <b>{tariff.duration_days} дней</b>\n"
        f"Трафик: <b>{format_quota(tariff.data_limit_gb)}</b>\n"
        f"{devices_line}\n\n"
        f"Сумма к оплате: {price_text}{total_line}\n\n"
        f"📱 Нужно больше устройств? Каждое дополнительное — "
        f"{device_settings.price} ₽ в месяц, оплачивается вместе с подпиской.\n"
        f"<i>Трафик общий на аккаунт и от количества устройств не меняется.</i>",
        reply_markup=tariff_slots_keyboard(
            tariff.id, slots, device_settings.max_extra, device_settings.base_limit,
            total_amount=checkout.total,
        ),
    )


@payment_router.callback_query(F.data.startswith("select_tariff_"))
async def select_tariff_handler(call: CallbackQuery, state: FSMContext, bot: Bot):
    """Обрабатывает выбор тарифа — показывает шаг выбора количества устройств."""
    await call.answer()

    user_id = call.from_user.id
    tariff_id = int(call.data.split("_")[2])
    tariff = await tariff_repo.get_by_id(tariff_id)
    if not tariff or not tariff.is_active:
        await call.message.edit_text("Ошибка! Тариф не найден.", reply_markup=back_to_main_menu_keyboard())
        return

    # Проверка на дубликат pending-платежа
    pending = await payment_service.get_pending_payment(user_id)
    if pending:
        await _show_pending_invoice(call, pending)
        return

    # Дефолт — то, за сколько слотов человек уже платит: при продлении они
    # должны сохраниться сами, без лишних нажатий.
    user = await user_repo.get(user_id)
    await _show_slots_step(call, state, tariff, (user.extra_devices or 0) if user else 0)


@payment_router.callback_query(F.data.startswith("tslots_"))
async def change_tariff_slots_handler(call: CallbackQuery, state: FSMContext):
    """Степпер количества доп. устройств на чекауте."""
    await call.answer()

    parts = call.data.split("_")  # ['tslots', '<tariff_id>', '<slots>']
    try:
        tariff_id, slots = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return

    tariff = await tariff_repo.get_by_id(tariff_id)
    if not tariff or not tariff.is_active:
        await call.message.edit_text("Ошибка! Тариф не найден.", reply_markup=back_to_main_menu_keyboard())
        return

    await _show_slots_step(call, state, tariff, slots)


@payment_router.callback_query(F.data.startswith("tpay_"))
async def tariff_to_payment_handler(call: CallbackQuery, state: FSMContext, bot: Bot):
    """Переход от выбора устройств к оплате."""
    await call.answer()

    user_id = call.from_user.id
    parts = call.data.split("_")  # ['tpay', '<tariff_id>', '<slots>']
    try:
        tariff_id, slots = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return

    tariff = await tariff_repo.get_by_id(tariff_id)
    if not tariff or not tariff.is_active:
        await call.message.edit_text("Ошибка! Тариф не найден.", reply_markup=back_to_main_menu_keyboard())
        return

    pending = await payment_service.get_pending_payment(user_id)
    if pending:
        await _show_pending_invoice(call, pending)
        return

    await _create_and_send_payment(call, state, bot, tariff, slots=slots)


@payment_router.callback_query(F.data.startswith("paymethod_"))
async def select_payment_method_handler(call: CallbackQuery, state: FSMContext, bot: Bot):
    """Обрабатывает кнопки выбора метода из предыдущих сообщений."""
    await call.answer()

    user_id = call.from_user.id
    parts = call.data.split("_")  # ['paymethod', 'card'|'sbp', '<tariff_id>', '<slots>']
    tariff_id = int(parts[2])
    # Старые сообщения (до появления доп. устройств) приходят без слотов.
    slots = int(parts[3]) if len(parts) > 3 else 0

    tariff = await tariff_repo.get_by_id(tariff_id)
    if not tariff or not tariff.is_active:
        await call.message.edit_text("Ошибка! Тариф не найден.", reply_markup=back_to_main_menu_keyboard())
        return

    # Повторная защита от дубликата (могли создать счёт в другой вкладке)
    pending = await payment_service.get_pending_payment(user_id)
    if pending:
        await _show_pending_invoice(call, pending)
        return

    await _create_and_send_payment(call, state, bot, tariff, slots=slots)


@payment_router.callback_query(F.data == "cancel_pending_payment")
async def cancel_pending_payment_handler(call: CallbackQuery, state: FSMContext):
    """Отменяет неоплаченный счёт по кнопке — не дожидаясь автоотмены по таймауту."""
    user_id = call.from_user.id

    canceled = await payment_service.cancel_pending_payment(user_id)

    if canceled:
        await call.answer("✅ Счёт отменён. Можете выбрать тариф заново.", show_alert=True)
    else:
        await call.answer("Неоплаченный счёт не найден — возможно, он уже оплачен или отменён.", show_alert=True)

    await show_tariffs_logic(call, state)
