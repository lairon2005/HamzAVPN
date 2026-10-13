# tgbot/handlers/webhook_handlers.py

import json
from datetime import datetime
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiohttp import web
from aiogram import Bot, Dispatcher

from tgbot.services import payment_service
from platega.client import money, transaction_id, PlategaError, verified_order_amount, normalize_callback, callback_kind
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


async def notify_referrer(bot: Bot, result) -> None:
    """Реферер узнаёт о первой оплате друга и о начисленных днях."""
    if not (result.referrer_id and result.referrer_id > 0):
        return
    from tgbot.services.referral_service import REFERRER_PAYMENT_BONUS_DAYS
    try:
        await bot.send_message(result.referrer_id,
            f'🎉 Друг оплатил подписку. Вам начислено <b>{REFERRER_PAYMENT_BONUS_DAYS} бонусных дней</b>.')
    except Exception:
        logger.warning('Не удалось уведомить реферера %s', result.referrer_id)


async def _drop_autorenew_after_slots(bot: Bot, user_id: int) -> None:
    """Сумма автопродления фиксирована: после докупки слотов оно отключается, чтобы
    следующее списание не обнулило купленные устройства. Человека об этом предупреждаем."""
    try:
        from tgbot.services import recurring_service
        dropped = await recurring_service.on_devices_changed(user_id)
        if dropped and user_id > 0:
            from tgbot.keyboards.inline import autorenew_prompt_keyboard
            await bot.send_message(
                user_id,
                '🔁 <b>Автопродление отключено</b>, потому что вы изменили дополнительные услуги, '
                'а сумма автопродления фиксирована. Деньги больше списываться не будут. '
                'Подключите его заново — с учётом новых устройств и трафика.',
                reply_markup=autorenew_prompt_keyboard(),
            )
    except Exception:
        logger.exception('Не удалось отключить автопродление после докупки устройств user=%s', user_id)


async def _notify_traffic_purchased(bot: Bot, result) -> None:
    """Трафик начислен — сообщаем новый лимит."""
    from tgbot.keyboards.inline import back_to_main_menu_keyboard

    bought = result.payment.extra_traffic_gb or 0
    try:
        await bot.send_message(
            result.payment.user_id,
            f"✅ <b>Оплата прошла</b>\n\n"
            f"Добавлено трафика: <b>+{bought} ГБ/мес</b>.\n"
            f"Всего докупленного трафика: <b>{result.extra_traffic_gb} ГБ</b>.\n\n"
            "Новый лимит уже действует — перевыпускать ключ не нужно. "
            "Докупленное действует до конца текущей подписки.",
            reply_markup=back_to_main_menu_keyboard(),
        )
    except Exception as e:
        logger.error(f"Failed to notify user {result.payment.user_id} about traffic: {e}")


async def _log_traffic_purchase(bot: Bot, result) -> None:
    """Лог докупки трафика в админ-чат."""
    user = await user_repo.get(result.payment.user_id)
    if not user:
        return

    source_icon = "🌐 WEB" if result.payment.user_id < 0 else "🤖 BOT"
    username_text = f"@{user.username}" if user.username else "Нет"

    text = (
        f"{source_icon} | 📊 Докупка трафика\n\n"
        f"👤 <b>User:</b> {user.full_name} (ID: <code>{user.user_id}</code>)\n"
        f"🏷 <b>Username:</b> {username_text}\n\n"
        f"➕ <b>Куплено:</b> {result.payment.extra_traffic_gb} ГБ/мес\n"
        f"📊 <b>Всего докупленного:</b> {result.extra_traffic_gb} ГБ\n"
        f"💰 <b>Сумма:</b> {result.payment.final_amount:.2f} RUB"
    )

    try:
        await bot.send_message(
            chat_id=config.tg_bot.support_chat_id,
            message_thread_id=config.tg_bot.transaction_log_topic_id,
            text=text
        )
    except Exception as e:
        logger.error(f"Failed to send traffic log: {e}")


# --- ГЛАВНЫЙ ХЕНДЛЕР ---

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
    authenticated = platega_client.authenticate_callback(request.headers)
    credentials_sent = any(name in request.headers for name in ('X-MerchantId', 'X-Secret'))
    if credentials_sent and not authenticated:
        return web.Response(status=401)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
        body = normalize_callback(body)
    except (ValueError, json.JSONDecodeError):
        return web.Response(status=401 if not authenticated else 400)
    kind = callback_kind(body)
    if kind != 'payment':
        # Автопродление: списания и смена статуса подписки. Статус подписки Platega может
        # прислать без заголовков, поэтому такие запросы проверяются через API, а не по секрету.
        from tgbot.handlers.recurring_webhook import handle_subscription_callback
        return await handle_subscription_callback(request, body, kind, authenticated)
    if not authenticated:
        return web.Response(status=401)
    try:
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
                if order is None or str(order.user_id) != str(payload.get('userId')):
                    return web.Response(status=409)
                try:
                    verified_order_amount(verified, order.final_amount, order.currency,
                                          order_id=order_id, user_id=order.user_id)
                except ValueError:
                    logger.warning('Отклонен callback: сумма или привязка резерва; платеж=%s', identifier)
                    return web.Response(status=409)
                payment = await payment_repo.bind_transaction(reservation, identifier)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                return web.Response(status=503)
        try:
            order_amount = verified_order_amount(
                verified, payment.final_amount, payment.currency,
                order_id=getattr(payment, 'order_id', None), user_id=payment.user_id)
        except ValueError:
            logger.warning('Отклонен callback: сумма или привязка заказа; платеж=%s; '
                           'API сумма=%s; сумма заказа=%s',
                           identifier, details['amount'], payment.final_amount)
            return web.Response(status=409)
        # Callback может содержать цену заказа или уже проверенную полную
        # сумму с комиссией. Цена услуги в любом случае берётся из заказа.
        if (verified['status'] != status
                or callback_amount not in (order_amount, money(details['amount']))
                or details['currency'] != currency or payment.currency != currency):
            logger.warning('Отклонен callback: платеж=%s; API статус=%s; callback статус=%s; '
                           'callback сумма=%s; сумма заказа=%s; валюта совпадает=%s',
                           identifier, verified['status'], status, callback_amount, order_amount,
                           details['currency'] == currency == payment.currency)
            return web.Response(status=409)

        bot: Bot = request.app['bot']
        remnawave: RemnawaveClient = request.app['remnawave']
        if status == 'CONFIRMED':
            result = await payment_service.process_successful_payment(identifier, order_amount, currency)
            if result is None:
                return web.Response(status=200)
            if result.kind == 'traffic':
                await _log_traffic_purchase(bot, result)
                if result.payment.user_id > 0:
                    await _notify_traffic_purchased(bot, result)
                await _drop_autorenew_after_slots(bot, result.payment.user_id)
                return web.Response(status=200)
            if result.kind == 'devices':
                await _log_device_slots_purchase(bot, result)
                if result.payment.user_id > 0:
                    await _notify_slots_purchased(bot, result)
                await _drop_autorenew_after_slots(bot, result.payment.user_id)
                return web.Response(status=200)
            await notify_referrer(bot, result)
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
