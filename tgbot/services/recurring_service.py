"""Автопродление: рекуррентные СБП-подписки Platega.

Platega сама списывает деньги по расписанию и присылает callback на каждое списание.
Мы ничего не инициируем: создаём подписку, отправляем человека привязать счёт, а
каждое подтверждённое списание проводим тем же конвейером, что и разовый платёж
(`Payment` с source='auto' → `process_successful_payment`) — дни, слоты, рефералка,
синхронизация с панелью и возвраты работают без отдельной ветки.

Состав подписки (тариф, слоты, сумма) фиксируется при создании: Platega не умеет менять
цену действующей подписки. Поэтому изменение числа устройств посреди периода
отключает автопродление (`on_devices_changed`) — иначе следующее списание сбросило бы
докупленные слоты.
"""
from dataclasses import dataclass
from datetime import datetime

from loader import logger
from platega.client import PlategaError, parse_platega_time, subscription_interval, whole_rubles
from tgbot.services.device_pricing import build_checkout
from tgbot.services.payment_service import PendingPaymentError
from tgbot.services.pricing import effective_price

# На привязку счёта в банке Platega даёт 30 минут, после чего подписка становится Failed.
PENDING_TTL_MINUTES = 35
# Как часто сверять активные подписки с Platega (пропущенные callback'и, внешняя отмена).
ACTIVE_RECHECK_HOURS = 6

REMOTE_TO_LOCAL = {
    'PendingAgreement': 'pending', 'Active': 'active', 'PastDue': 'past_due',
    'Cancelled': 'cancelled', 'Failed': 'failed',
}
CALLBACK_TO_LOCAL = {
    'SUBSCRIPTION_ACTIVATED': 'active', 'SUBSCRIPTION_PAST_DUE': 'past_due',
    'SUBSCRIPTION_CANCELLED': 'cancelled', 'SUBSCRIPTION_FAILED': 'failed',
}


class RecurringError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


@dataclass
class AutoRenewOffer:
    amount: int
    interval: int
    interval_count: int
    checkout: object


class RecurringService:
    def __init__(self, recurring_repo, payment_repo, payment_service, tariff_repo,
                 user_repo, device_slot_service, platega=None, enabled: bool = True):
        self._platega_client = platega
        self._enabled = enabled
        self._repo = recurring_repo
        self._payment_repo = payment_repo
        self._payments = payment_service
        self._tariffs = tariff_repo
        self._users = user_repo
        self._slots = device_slot_service

    @property
    def _platega(self):
        if self._platega_client is not None:
            return self._platega_client
        from loader import platega_client
        return platega_client

    def is_available(self) -> bool:
        """Автопродление включено в настройках и платёжная система настроена."""
        return bool(self._enabled and self._platega.configured)

    # ------------------------------------------------------------------ оформление

    async def build_offer(self, user, tariff, slots: int) -> AutoRenewOffer:
        """Сумма и период подписки для тарифа и числа слотов либо RecurringError."""
        if tariff is None or not tariff.is_active:
            raise RecurringError('Тариф не найден')
        interval = subscription_interval(tariff.duration_days)
        if interval is None:
            raise RecurringError('Для этого тарифа автопродление недоступно')
        settings = await self._slots.settings()
        slots = max(0, min(settings.max_extra, slots))
        active = bool(user and user.subscription_end_date and user.subscription_end_date > datetime.now())
        checkout = build_checkout(settings, effective_price(tariff, active), tariff.duration_days, slots, 0)
        try:
            amount = whole_rubles(checkout.total)
        except ValueError:
            raise RecurringError(
                'Автопродление доступно только для тарифов с ценой в целых рублях. '
                'Оплатите этот тариф разово.') from None
        return AutoRenewOffer(amount, interval[0], interval[1], checkout)

    async def start(self, user_id: int, tariff_id: int, slots: int, source: str = 'bot'):
        """Создаёт подписку в Platega и локальную запись. Возвращает (подписка, ссылка).

        Живая подписка в статусе pending (человек не дошёл до привязки) заменяется новой:
        денег по ней не списывалось. Активную заменить нельзя — сначала отключение.
        """
        if not self.is_available():
            raise RecurringError('Автопродление пока недоступно. Оплатите тариф разово.')
        user = await self._users.get(user_id)
        tariff = await self._tariffs.get_by_id(tariff_id)
        offer = await self.build_offer(user, tariff, slots)
        async with self._payment_repo.checkout_lock(user_id):
            if await self._payments.has_pending_payment(user_id):
                raise PendingPaymentError('У вас уже есть неоплаченный счет')
            live = await self._repo.get_live_for_user(user_id)
            if live and live.status == 'active':
                raise RecurringError('Автопродление уже подключено')
            if live:
                try:
                    await self._platega.cancel_subscription(live.platega_subscription_id)
                except PlategaError:
                    logger.warning('Не удалось отменить незавершённую подписку %s',
                                   live.platega_subscription_id)
                await self._repo.set_status(live.platega_subscription_id, 'cancelled')
            created = await self._platega.create_subscription(
                offer.amount, f'Автопродление HamzAVPN: {tariff.name}',
                offer.interval, offer.interval_count)
            slots = offer.checkout.slots
            sub = await self._repo.create(
                created.id, user_id, tariff.id, slots, offer.amount, created.url, source)
        logger.info('Автопродление: подписка %s, user=%s, tariff=%s, slots=%s, amount=%s',
                    created.id, user_id, tariff.id, slots, offer.amount)
        return sub, created.url

    # ------------------------------------------------------------------ отключение

    async def cancel(self, user_id: int):
        """Отключает автопродление. Возвращает отключённую подписку или None, если нечего."""
        live = await self._repo.get_live_for_user(user_id)
        if live is None:
            return None
        # Сначала Platega: если отмена не прошла, локально подписку «живой» не бросаем и
        # не обещаем человеку, что списаний не будет.
        await self._platega.cancel_subscription(live.platega_subscription_id)
        await self._repo.set_status(live.platega_subscription_id, 'cancelled')
        return live

    async def on_devices_changed(self, user_id: int):
        """Слоты изменились посреди периода — подписка с прежней суммой больше не годится."""
        live = await self._repo.get_live_for_user(user_id)
        if live is None or live.status != 'active':
            return None
        try:
            return await self.cancel(user_id)
        except PlategaError:
            logger.error('Не удалось отключить автопродление user=%s после смены устройств', user_id)
            return None

    async def get_view(self, user_id: int):
        """Подписка для показа человеку: живая, а при её отсутствии — последняя."""
        return await self._repo.get_live_for_user(user_id) or await self._repo.get_last_for_user(user_id)

    async def has_active(self, user_id: int) -> bool:
        live = await self._repo.get_live_for_user(user_id)
        return bool(live and live.status == 'active')

    # ------------------------------------------------------------------ callback

    async def apply_remote_state(self, sub, remote: dict) -> tuple[str, str]:
        """Приводит локальный статус к состоянию, которое сообщает API Platega.

        Возвращает (старый статус, новый статус); если они равны — ничего не менялось.
        """
        new = REMOTE_TO_LOCAL.get(remote.get('status'))
        if new is None:
            return sub.status, sub.status
        next_at = parse_platega_time(remote.get('nextChargeAt'))
        changed = await self._repo.set_status(sub.platega_subscription_id, new, next_at)
        await self._repo.touch_checked(sub.platega_subscription_id, next_at if new == 'active' else None)
        return (sub.status, new) if changed else (sub.status, sub.status)

    async def on_charge_confirmed(self, sub, charge_id: str, amount, currency: str, next_charge_at):
        """Подтверждённое списание → Payment(source='auto') → выдача дней. Повтор безопасен."""
        from platega.client import money
        if currency != sub.currency or money(amount) != money(sub.amount):
            raise ValueError('Сумма или валюта списания не совпадает с подпиской')
        tariff = await self._tariffs.get_by_id(sub.tariff_id)
        if tariff is None:
            raise LookupError('Тариф подписки не найден')
        await self._payment_repo.create(
            provider_payment_id=charge_id, user_id=sub.user_id, tariff_id=sub.tariff_id,
            original_amount=float(sub.amount), final_amount=float(sub.amount), source='auto',
            kind='subscription', extra_devices=sub.extra_devices,
        )
        result = await self._payments.process_successful_payment(charge_id, sub.amount, sub.currency)
        await self._repo.record_charge(sub.platega_subscription_id, next_charge_at) \
            if result is not None else None
        return result

    async def on_charge_failed(self, sub) -> bool:
        """Списание не прошло, повторов Platega не делает. True — статус сменился (уведомить)."""
        return await self._repo.set_status(sub.platega_subscription_id, 'past_due')

    # ------------------------------------------------------------------ сверка

    async def reconcile(self) -> list[tuple[object, str, str]]:
        """Сверка с Platega: брошенные привязки, внешние отмены, потерянные callback'и.

        Возвращает список (подписка, старый статус, новый) для уведомлений пользователям.
        """
        changes = []
        if not self._platega.configured:
            return changes
        stale = await self._repo.get_stale_pending(PENDING_TTL_MINUTES)
        active = await self._repo.get_active_to_check(ACTIVE_RECHECK_HOURS)
        for sub in [*stale, *active]:
            try:
                remote = await self._platega.get_subscription(sub.platega_subscription_id)
                old, new = await self.apply_remote_state(sub, remote)
                if new != old:
                    changes.append((sub, old, new))
                metrics = remote.get('chargeMetrics') or {}
                paid = int(metrics.get('chargesSuccess') or 0)
                credited = await self._payment_repo.count_auto_since(sub.user_id, sub.created_at)
                if new == 'active' and paid > credited:
                    # Деньги списаны, а callback до нас не дошёл: ID списания из API
                    # подписок не получить — нужна ручная сверка в кабинете Platega.
                    logger.error(
                        'Автопродление %s: Platega списала %s раз(а), у нас зачислено %s '
                        '(user=%s). Проверьте списания в кабинете Platega.',
                        sub.platega_subscription_id, paid, credited, sub.user_id)
            except PlategaError:
                logger.warning('Сверка подписки %s отложена', sub.platega_subscription_id)
            except Exception:
                logger.exception('Сверка подписки %s не удалась', sub.platega_subscription_id)
        return changes
