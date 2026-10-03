# tgbot/handlers/webhook_handlers.py

import json
from datetime import datetime
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiohttp import web
from aiogram import Bot, Dispatcher

from tgbot.services import payment_service
from platega.client import money, transaction_id, PlategaError
from database import user_repo, payment_repo
from loader import logger, config, platega_client
from remnawave.client import RemnawaveClient
from tgbot.handlers.user.profile import show_profile_logic


async def _notify_tg_user(user_id: int, tariff, remnawave: RemnawaveClient, bot: Bot, request: web.Request,
                          is_auto: bool = False):
    """Уведомление ТОЛЬКО для Telegram пользователей."""
    if user_id < 0:
        return

    try:
        dp: Dispatcher = request.app['dp']
        storage = dp.storage
        state = FSMContext(storage=storage, key=StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id))

        fsm_data = await state.get_data()
        msg_id = fsm_data.get("payment_message_id")
        if msg_id:
            await bot.edit_message_text(chat_id=user_id, message_id=msg_id, text="✅ <i>Счет оплачен.</i>", reply_markup=None)
        await state.clear()
    except Exception:
        pass

    try:
        if is_auto:
            success_text = (
                f"🔄 Подписка автоматически продлена! Тариф '<b>{tariff.name}</b>' — "
                f"{tariff.duration_days} дней."
            )
        else:
            success_text = f"✅ Оплата успешна! Тариф '<b>{tariff.name}</b>' активирован."
        await bot.send_message(user_id, success_text)

        from aiogram.types import User, Chat, Message
        fake_msg = Message(
            message_id=0, date=datetime.now(),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="User")
        )
        # show_referral_cta=True — момент счастья (§7.5): сразу после успешной
        # оплаты/продления показываем кнопку "Поделиться и получить дни".
        await show_profile_logic(fake_msg, remnawave, bot, show_referral_cta=True)
    except Exception as e:
        logger.error(f"Failed to notify TG user {user_id}: {e}")


async def _log_transaction(bot: Bot, user_id: int, tariff_name: str, price: float,
                           is_new: bool, payment=None):
    """Логирование в админ-чат с информацией о скидке."""
    user = await user_repo.get(user_id)
    if not user:
        return

    source_icon = "🌐 WEB" if user_id < 0 else "🤖 BOT"
    action = "💎 Новая подписка" if is_new else "🔄 Продление"
    username_text = f"@{user.username}" if user.username else "Нет"

    # Формируем строку суммы с учётом скидки
    if payment and payment.discount_percent > 0:
        amount_text = (
            f"{payment.final_amount:.2f} RUB "
            f"(скидка {payment.discount_percent}%, промокод {payment.promo_code}, "
        )
    else:
        amount_text = f"{price:.2f} RUB"

    text = (
        f"{source_icon} | {action}\n\n"
        f"👤 <b>User:</b> {user.full_name} (ID: <code>{user.user_id}</code>)\n"
        f"🏷 <b>Username:</b> {username_text}\n\n"
        f"💳 <b>Тариф:</b> {tariff_name}\n"
        f"💰 <b>Сумма:</b> {amount_text}"
    )

    try:
        await bot.send_message(
            chat_id=config.tg_bot.support_chat_id,
            message_thread_id=config.tg_bot.transaction_log_topic_id,
            text=text
        )
    except Exception as e:
        logger.error(f"Failed to send transaction log: {e}")


async def _log_refund(bot: Bot, payment):
    """Логирование возврата в админ-чат."""
    user = await user_repo.get(payment.user_id)
    if not user:
        return

    username_text = f"@{user.username}" if user.username else "Нет"

    text = (
        f"💸 <b>ВОЗВРАТ</b>\n\n"
        f"👤 <b>User:</b> {user.full_name} (ID: <code>{user.user_id}</code>)\n"
        f"🏷 <b>Username:</b> {username_text}\n\n"
        f"💰 <b>Сумма возврата:</b> {payment.final_amount:.2f} RUB\n"
        f"🆔 <b>Platega ID:</b> <code>{payment.provider_payment_id}</code>"
    )

    try:
        await bot.send_message(
            chat_id=config.tg_bot.support_chat_id,
            message_thread_id=config.tg_bot.transaction_log_topic_id,
            text=text
        )
    except Exception as e:
        logger.error(f"Failed to send refund log: {e}")


async def _notify_slots_purchased(bot: Bot, result) -> None:
    """Слоты начислены — сообщаем новый лимит и ведём на экран устройств."""
    from tgbot.keyboards.inline import back_to_main_menu_keyboard

    bought = result.payment.extra_devices or 0
    try:
        await bot.send_message(
            result.payment.user_id,
            f"✅ <b>Оплата прошла</b>\n\n"
            f"Добавлено доп. устройств: <b>{bought}</b>.\n"
            f"Всего оплачено дополнительных слотов: <b>{result.extra_devices}</b>.\n\n"
            "Новый лимит уже действует — подключайте устройство, "
            "перевыпускать ключ не нужно.",
            reply_markup=back_to_main_menu_keyboard(),
        )
    except Exception as e:
        logger.error(f"Failed to notify user {result.payment.user_id} about slots: {e}")


async def _log_device_slots_purchase(bot: Bot, result) -> None:
    """Лог докупки устройств в админ-чат."""
    user = await user_repo.get(result.payment.user_id)
    if not user:
        return

    source_icon = "🌐 WEB" if result.payment.user_id < 0 else "🤖 BOT"
    username_text = f"@{user.username}" if user.username else "Нет"

    text = (
        f"{source_icon} | 📱 Докупка устройств\n\n"
        f"👤 <b>User:</b> {user.full_name} (ID: <code>{user.user_id}</code>)\n"
        f"🏷 <b>Username:</b> {username_text}\n\n"
        f"➕ <b>Слотов куплено:</b> {result.payment.extra_devices}\n"
        f"📱 <b>Всего доп. слотов:</b> {result.extra_devices}\n"
        f"💰 <b>Сумма:</b> {result.payment.final_amount:.2f} RUB"
    )

    try:
        await bot.send_message(
            chat_id=config.tg_bot.support_chat_id,
            message_thread_id=config.tg_bot.transaction_log_topic_id,
            text=text
        )
    except Exception as e:
        logger.error(f"Failed to send device slots log: {e}")


async def platega_webhook_handler(request: web.Request):
    """Аутентификация callback + независимая проверка транзакции через API."""
    if not platega_client.authenticate_callback(request.headers):
        return web.Response(status=401)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
        identifier = transaction_id(body['id'])
        callback_amount = money(body['amount'])
        currency = body['currency']
        status = body['status']
        if currency != 'RUB' or status not in ('CONFIRMED', 'CANCELED', 'CHARGEBACKED'):
            raise ValueError
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return web.Response(status=400)
    try:
        verified = await platega_client.get_payment(identifier)
        details = verified['paymentDetails']
        payment = await payment_repo.get_by_provider_id(identifier)
        if payment is None:
            # Резерв зафиксирован до обращения к API. Восстановление работает даже
            # если ответ создания потерялся или процесс остановился до bind.
            try:
                payload = json.loads(verified.get('payload') or '{}')
                order_id = transaction_id(payload['orderId'])
                reservation = f'order:{order_id}'
                order = await payment_repo.get_by_provider_id(reservation)
                if (order is None or str(order.user_id) != str(payload.get('userId'))
                        or money(details['amount']) != money(order.final_amount)
                        or details['currency'] != order.currency):
                    return web.Response(status=409)
                payment = await payment_repo.bind_transaction(reservation, identifier)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                return web.Response(status=503)
        if (verified['status'] != status or money(details['amount']) != callback_amount
                or callback_amount != money(payment.final_amount)
                or details['currency'] != currency or payment.currency != currency):
            logger.warning('Отклонен callback с несовпадающими данными: %s', identifier)
            return web.Response(status=409)

        bot: Bot = request.app['bot']
        remnawave: RemnawaveClient = request.app['remnawave']
        if status == 'CONFIRMED':
            result = await payment_service.process_successful_payment(identifier, callback_amount, currency)
            if result is None:
                return web.Response(status=200)
            if result.kind == 'devices':
                await _log_device_slots_purchase(bot, result)
                if result.payment.user_id > 0:
                    await _notify_slots_purchased(bot, result)
                return web.Response(status=200)
            if result.referrer_id and result.referrer_id > 0:
                from tgbot.services.referral_service import REFERRER_PAYMENT_BONUS_DAYS
                try:
                    await bot.send_message(result.referrer_id,
                        f'🎉 Друг оплатил подписку. Вам начислено <b>{REFERRER_PAYMENT_BONUS_DAYS} бонусных дней</b>.')
                except Exception:
                    logger.warning('Не удалось уведомить реферера %s', result.referrer_id)
            await _log_transaction(bot, result.payment.user_id, result.tariff.name,
                result.payment.final_amount, result.extension.is_new_user, payment=result.payment)
            if result.payment.user_id > 0:
                await _notify_tg_user(result.payment.user_id, result.tariff, remnawave, bot, request)
        elif status == 'CANCELED':
            await payment_repo.cancel_if_pending(identifier)
        else:
            refunded = await payment_service.process_refund(identifier)
            if refunded:
                await _log_refund(bot, refunded)
                if refunded.user_id > 0:
                    try:
                        await bot.send_message(refunded.user_id,
                            f'💸 Возврат: <b>{refunded.final_amount:.2f} RUB</b>. '
                            'Оплаченная услуга скорректирована.')
                    except Exception:
                        logger.warning('Не удалось уведомить о возврате %s', identifier)
        return web.Response(status=200)
    except Exception:
        # DB/шлюз/панель временно недоступны: 503 вызывает повтор callback.
        # Уже зачисленные дни защищены атомарным маркером статуса в БД.
        logger.exception('Обработка callback Platega отложена: %s', identifier)
        return web.Response(status=503)
