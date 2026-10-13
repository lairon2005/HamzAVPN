"""Автопродление в боте: подключение, статус, отключение.

Деньги списывает Platega по СБП-подписке (см. tgbot/services/recurring_service.py).
Кнопки «Моя карта» из прежних сообщений (`manage_card`, `pm_*`) ведут на тот же экран.
"""
from html import escape

from aiogram import Router, F
from aiogram.types import CallbackQuery

from database import tariff_repo
from loader import logger
from platega.client import PlategaError
from tgbot.keyboards.inline import (
    autorenew_screen_keyboard, autorenew_confirm_off_keyboard,
    back_to_main_menu_keyboard,
)
from tgbot.services import recurring_service
from tgbot.services.payment_service import PendingPaymentError
from tgbot.services.recurring_service import RecurringError
from utils.telegram_ui import replace_message_text

payment_methods_router = Router()

STATUS_TITLES = {
    'pending': '⏳ Ждём привязку счёта',
    'active': '✅ Автопродление включено',
    'past_due': '⚠️ Автопродление остановлено: списание не прошло',
    'cancelled': 'Автопродление отключено',
    'failed': '❌ Не удалось подключить автопродление',
}

CONSENT = (
    'Нажимая «Привязать счёт», вы соглашаетесь на регулярные списания <b>{amount} ₽</b> '
    'каждые <b>{days} дн.</b> через СБП. Платёж проводит Platega; подписка будет продлеваться, '
    'пока вы её не отключите. Отключить можно в любой момент: в боте, в личном кабинете или '
    'по ссылке из письма Platega — деньги перестанут списываться сразу.'
)


def _date(value) -> str:
    return value.strftime('%d.%m.%Y') if value else '—'


async def _screen_text(user_id: int) -> tuple[str, str | None, str | None]:
    sub = await recurring_service.get_view(user_id)
    if sub is None:
        return ('🔁 <b>Автопродление</b>\n\nПодписка продлевается сама, пока вы её не отключите: '
                'ничего не нужно помнить и нажимать.\n\nЧтобы включить, выберите тариф и нажмите '
                '«🔁 С автопродлением». Отключить можно в любой момент.'), None, None
    tariff = await tariff_repo.get_by_id(sub.tariff_id)
    lines = [f'🔁 <b>Автопродление</b>\n\n{STATUS_TITLES.get(sub.status, sub.status)}\n']
    if tariff:
        lines.append(f'Тариф: <b>{escape(tariff.name)}</b> ({tariff.duration_days} дн.)')
    lines.append(f'Сумма списания: <b>{sub.amount} ₽</b>')
    if sub.extra_devices:
        lines.append(f'Доп. устройств: <b>{sub.extra_devices}</b>')
    if sub.status == 'active':
        lines.append(f'Следующее списание: <b>{_date(sub.next_charge_at)}</b>')
        lines.append('\nЧтобы изменить число устройств или тариф, отключите автопродление и '
                     'подключите его заново.')
    elif sub.status == 'pending':
        lines.append('\nПривяжите счёт в банке в течение 30 минут, иначе подписка не активируется.')
    elif sub.status == 'past_due':
        lines.append('\nПовторных попыток списания не будет. Продлите подписку вручную или '
                     'подключите автопродление заново.')
    return '\n'.join(lines), sub.status, sub.payment_url


async def show_autorenew_screen(call: CallbackQuery) -> None:
    text, status, url = await _screen_text(call.from_user.id)
    await replace_message_text(call.message, text, reply_markup=autorenew_screen_keyboard(status, url))


@payment_methods_router.callback_query(
    (F.data == 'autorenew') | (F.data == 'manage_card') | F.data.startswith('pm_'))
async def autorenew_screen_handler(call: CallbackQuery):
    await call.answer()
    await show_autorenew_screen(call)


@payment_methods_router.callback_query(F.data == 'autorenew_off')
async def autorenew_off_handler(call: CallbackQuery):
    await call.answer()
    sub = await recurring_service.get_view(call.from_user.id)
    if sub is None or sub.status not in ('pending', 'active'):
        await show_autorenew_screen(call)
        return
    await replace_message_text(
        call.message,
        '❓ <b>Отключить автопродление?</b>\n\nДеньги списываться перестанут. '
        'Уже оплаченный срок подписки сохранится.',
        reply_markup=autorenew_confirm_off_keyboard())


@payment_methods_router.callback_query(F.data == 'autorenew_off_yes')
async def autorenew_off_confirmed_handler(call: CallbackQuery):
    try:
        await recurring_service.cancel(call.from_user.id)
    except PlategaError:
        await call.answer('Не удалось отключить: платёжный сервис недоступен. '
                          'Повторите через минуту.', show_alert=True)
        return
    await call.answer('Автопродление отключено', show_alert=True)
    await show_autorenew_screen(call)


@payment_methods_router.callback_query(F.data.startswith('tauto_'))
async def start_autorenew_handler(call: CallbackQuery):
    """Оформление подписки: тариф и число устройств приходят из callback_data шага чекаута."""
    await call.answer()
    user_id = call.from_user.id
    try:
        parts = call.data.split('_')
        _, tariff_id, slots = parts[:3]
        packs = int(parts[3]) if len(parts) > 3 else 0
        tariff_id, slots = int(tariff_id), int(slots)
    except ValueError:
        return
    try:
        sub, url = await recurring_service.start(user_id, tariff_id, slots, source='bot', packs=packs)
    except RecurringError as exc:
        await replace_message_text(call.message, f'❌ {escape(str(exc))}',
                                   reply_markup=back_to_main_menu_keyboard())
        return
    except PendingPaymentError:
        from tgbot.handlers.user.payment import _show_pending_invoice
        from tgbot.services import payment_service
        pending = await payment_service.get_pending_payment(user_id)
        if pending:
            await _show_pending_invoice(call, pending)
        return
    except Exception:
        logger.error('Не удалось создать подписку автопродления user=%s', user_id, exc_info=True)
        await replace_message_text(
            call.message, '❌ Автопродление временно недоступно. Оплатите тариф разово '
            'или повторите позже.', reply_markup=back_to_main_menu_keyboard())
        return
    tariff = await tariff_repo.get_by_id(sub.tariff_id)
    text = (
        f'🔁 <b>Автопродление</b>\n\nТариф: <b>{escape(tariff.name)}</b>\n'
        f'Списание: <b>{sub.amount} ₽</b> каждые <b>{tariff.duration_days} дн.</b>\n'
        f'Устройств: доп. <b>{sub.extra_devices}</b>\n\n'
        + CONSENT.format(amount=sub.amount, days=tariff.duration_days)
        + '\n\nНа привязку счёта даётся 30 минут.'
    )
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text='💳 Привязать счёт', url=url)
    kb.button(text='📄 Документы и тарифы', callback_data='documents')
    kb.button(text='❌ Отменить', callback_data='autorenew_off')
    kb.adjust(1)
    await replace_message_text(call.message, text, reply_markup=kb.as_markup())
