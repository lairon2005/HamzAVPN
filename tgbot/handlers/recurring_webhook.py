"""Callback'и Platega по рекуррентным подпискам (автопродление).

Приходят на тот же `/platega`, что и разовые платежи; разбирает их `platega_webhook_handler`.

* Списание (`SubscriptionId` + CONFIRMED/CANCELED/CHARGEBACKED) — подтверждается секретом из
  заголовков и сверкой подписки через API, дни выдаёт `recurring_service.on_charge_confirmed`.
* Статус подписки (`SUBSCRIPTION_*`) — заголовки Platega в документации необязательны, поэтому
  телу callback не доверяем вовсе: берём реальный статус из `GET /subscription/{id}`.

Ответ 200 — «принято», 503 — «повторите» (Platega делает до 3 повторов), остальное — отказ.
"""
from datetime import datetime

from aiohttp import web
from aiogram import Bot

from database import user_repo, recurring_repo
from loader import logger, platega_client
from platega.client import PlategaError, money, parse_charge, transaction_id, whole_rubles
from tgbot.services import payment_service, recurring_service

CHARGE_STATUSES = ('CONFIRMED', 'CANCELED', 'CHARGEBACKED')


def _fmt_date(value: datetime | None) -> str:
    return value.strftime('%d.%m.%Y') if value else '—'


async def _send(bot: Bot, user_id: int, text: str, markup=None) -> None:
    if user_id < 0:  # пользователи сайта: писать некуда
        return
    try:
        await bot.send_message(user_id, text, reply_markup=markup)
    except Exception as exc:
        logger.warning('Не удалось уведомить %s об автопродлении: %s', user_id, exc)


async def notify_status_change(bot: Bot, sub, old: str, new: str) -> None:
    from tgbot.keyboards.inline import autorenew_manage_keyboard, autorenew_prompt_keyboard
    user = await user_repo.get(sub.user_id)
    until = _fmt_date(user.subscription_end_date) if user else '—'
    if new == 'active' and old == 'pending':
        await _send(bot, sub.user_id,
                    f'✅ <b>Автопродление подключено</b>\n\nСписание {sub.amount} ₽ по расписанию тарифа. '
                    'Дни добавятся сразу после списания. Отключить можно в любой момент.',
                    autorenew_manage_keyboard())
    elif new == 'cancelled':
        await _send(bot, sub.user_id,
                    f'🔁 <b>Автопродление отключено.</b> Деньги больше списываться не будут. '
                    f'Подписка действует до {until}.', autorenew_prompt_keyboard())
    elif new == 'failed':
        await _send(bot, sub.user_id,
                    '❌ <b>Не удалось подключить автопродление</b> — время на привязку счёта истекло '
                    'или банк не подтвердил согласие. Деньги не списаны. Попробуйте ещё раз.',
                    autorenew_prompt_keyboard())
    elif new == 'past_due':
        await _notify_charge_failed(bot, sub)


async def _notify_charge_failed(bot: Bot, sub) -> None:
    from tgbot.keyboards.inline import autorenew_prompt_keyboard
    user = await user_repo.get(sub.user_id)
    until = _fmt_date(user.subscription_end_date) if user else '—'
    await _send(bot, sub.user_id,
                f'⚠️ <b>Не удалось списать {sub.amount} ₽ за автопродление.</b>\n\n'
                'Банк отклонил операцию, повторных попыток не будет, автопродление остановлено. '
                f'Подписка действует до {until}. Продлите её вручную или подключите автопродление заново.',
                autorenew_prompt_keyboard())


async def _handle_status(request: web.Request, data: dict) -> web.Response:
    try:
        sub_id = transaction_id(data.get('subscriptionid') or data.get('id'))
    except ValueError:
        return web.Response(status=400)
    sub = await recurring_repo.get_by_platega_id(sub_id)
    if sub is None:
        # Чужая или тестовая подписка: повторять доставку бессмысленно.
        return web.Response(status=200)
    try:
        remote = await platega_client.get_subscription(sub_id)
    except PlategaError:
        return web.Response(status=503)
    old, new = await recurring_service.apply_remote_state(sub, remote)
    if new != old:
        await notify_status_change(request.app['bot'], sub, old, new)
    return web.Response(status=200)


async def _soft_verify_charge(charge_id: str, sub) -> bool:
    """Сверка самого списания через GET /transaction. Недоступность API — не повод терять деньги."""
    try:
        verified = await platega_client.get_payment(charge_id)
    except PlategaError:
        logger.warning('Списание %s не удалось сверить через API транзакций', charge_id)
        return True
    details = verified['paymentDetails']
    return verified['status'] == 'CONFIRMED' and money(details['amount']) == money(sub.amount)


async def _handle_charge(request: web.Request, data: dict, authenticated: bool) -> web.Response:
    if not authenticated:
        return web.Response(status=401)
    charge = parse_charge(data)
    try:
        charge_id = transaction_id(charge['id'])
        sub_id = transaction_id(charge['subscription_id'])
        amount = money(charge['amount'])
        status = charge['status']
        currency = charge['currency']
        if status not in CHARGE_STATUSES or currency != 'RUB':
            raise ValueError
    except (ValueError, TypeError):
        return web.Response(status=400)

    sub = await recurring_repo.get_by_platega_id(sub_id)
    if sub is None:
        logger.error('Списание %s по неизвестной подписке %s', charge_id, sub_id)
        return web.Response(status=200)
    try:
        remote = await platega_client.get_subscription(sub_id)
        if whole_rubles(remote['amount']) != sub.amount:
            logger.error('Сумма подписки %s в Platega (%s) не совпадает с нашей (%s)',
                         sub_id, remote['amount'], sub.amount)
            return web.Response(status=409)
    except PlategaError:
        return web.Response(status=503)

    bot: Bot = request.app['bot']
    if status == 'CONFIRMED':
        if amount != money(sub.amount) or not await _soft_verify_charge(charge_id, sub):
            logger.error('Отклонено списание %s: данные не совпадают с подпиской %s', charge_id, sub_id)
            return web.Response(status=409)
        first_charge = (sub.charges_ok or 0) == 0
        try:
            result = await recurring_service.on_charge_confirmed(
                sub, charge_id, amount, currency, charge['next_charge_at'])
        except ValueError:
            return web.Response(status=409)
        if result is None:  # повтор уже обработанного callback
            return web.Response(status=200)
        await _after_charge(request, bot, sub, result, first_charge, charge['next_charge_at'])
    elif status == 'CANCELED':
        if await recurring_service.on_charge_failed(sub):
            await _notify_charge_failed(bot, sub)
    else:
        try:
            refunded = await payment_service.process_refund(charge_id)
        except LookupError:
            logger.error('Возврат по неизвестному списанию %s', charge_id)
            return web.Response(status=200)
        if refunded:
            from tgbot.handlers.webhook_handlers import _log_refund
            await _log_refund(bot, refunded)
            await _send(bot, refunded.user_id,
                        f'💸 Возврат: <b>{refunded.final_amount:.2f} RUB</b>. '
                        'Оплаченная услуга скорректирована.')
    return web.Response(status=200)


async def _after_charge(request: web.Request, bot: Bot, sub, result, first_charge: bool,
                        next_charge_at: datetime | None) -> None:
    from tgbot.handlers.webhook_handlers import _log_transaction, _notify_tg_user, notify_referrer
    from tgbot.keyboards.inline import autorenew_manage_keyboard
    await notify_referrer(bot, result)
    await _log_transaction(bot, sub.user_id, f'{result.tariff.name} (автопродление)',
                           result.payment.final_amount, result.extension.is_new_user,
                           payment=result.payment)
    if sub.user_id < 0:
        return
    if first_charge:
        # Первое списание = первая оплата: показываем профиль с ключом, как после обычной оплаты.
        await _notify_tg_user(sub.user_id, result.tariff, request.app['remnawave'], bot, request)
    user = await user_repo.get(sub.user_id)
    until = _fmt_date(user.subscription_end_date) if user else '—'
    head = '🔁 <b>Автопродление подключено</b>' if first_charge else '🔄 <b>Подписка продлена автоматически</b>'
    await _send(bot, sub.user_id,
                f'{head}\n\nТариф «{result.tariff.name}», списано {sub.amount} ₽. '
                f'Подписка действует до {until}.\n'
                f'Следующее списание: {_fmt_date(next_charge_at)}.\n'
                'Отключить автопродление можно в любой момент.',
                autorenew_manage_keyboard())


async def handle_subscription_callback(request: web.Request, data: dict, kind: str,
                                       authenticated: bool) -> web.Response:
    try:
        if kind == 'subscription_status':
            return await _handle_status(request, data)
        return await _handle_charge(request, data, authenticated)
    except Exception:
        # БД, шлюз или панель временно недоступны: 503 вызовет повтор доставки.
        logger.exception('Обработка callback автопродления отложена')
        return web.Response(status=503)
