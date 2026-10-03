"""
Докупка дополнительных устройств (слотов) и синхронизация лимита с панелью.

Разделение с [device_service.py]: там — показать и отвязать устройства, здесь —
сколько устройств человеку положено и что за это списано.

Главное, что делает сервис помимо арифметики, — доводит панель до состояния БД
(`sync_limit`). Это нужно не только после оплаты: PATCH в панель может не пройти
(панель уже лежала — инцидент 23.07), и тогда деньги списаны, а слот не выдан.
Поэтому `sync_limit` идемпотентен и вызывается ещё и по расписанию из
scheduler.sync_device_limits.

Про авточистку: Remnawave проверяет лимит только в момент регистрации НОВОГО
hwid. Уже записанные устройства продолжают работать, даже если лимит потом
опустили — то есть «купил 5 слотов на месяц, зарегистрировал 10 устройств,
перестал платить» без чистки оставался бы рабочей схемой навсегда. Поэтому при
снижении лимита лишние записи удаляются сами, начиная с самых давно не
заходивших.
"""
from dataclasses import dataclass
from datetime import datetime

from database.repositories.settings import SettingsRepository
from database.repositories.user import UserRepository
from loader import logger
from remnawave.client import RemnawaveClient, RemnawaveTransportError
from tgbot.services.device_pricing import (
    DeviceSettings,
    SETTING_BASE_LIMIT,
    SETTING_MAX_EXTRA,
    SETTING_PRICE,
    DEFAULT_BASE_LIMIT,
    DEFAULT_MAX_EXTRA,
    DEFAULT_PRICE,
    days_left,
    device_limit,
    prorated_slot_cost,
    prorated_slots_cost,
)

# Коды отказов — в той же системе, что и в key_service: веб отвечает редиректом
# и готовый текст через query-строку не протащить, только короткий код.
CODE_OK = "ok"
CODE_NO_SUBSCRIPTION = "no_subscription"
CODE_TRIAL = "trial"
CODE_MAX_REACHED = "max_reached"
CODE_BAD_QUANTITY = "bad_quantity"
CODE_PANEL = "panel"
CODE_GENERIC = "generic"

SLOT_NOTICES = {
    CODE_NO_SUBSCRIPTION: (
        "Докупить устройство можно только при активной подписке. "
        "Оформите подписку — нужное количество устройств выбирается при оплате."
    ),
    CODE_TRIAL: (
        "На пробном периоде доступны только базовые устройства. "
        "Оплатите любой тариф — и сможете докупить дополнительные."
    ),
    CODE_MAX_REACHED: "Вы уже используете максимальное количество устройств.",
    CODE_BAD_QUANTITY: "Некорректное количество устройств.",
    CODE_PANEL: (
        "VPN-панель временно недоступна. "
        "Пожалуйста, повторите попытку через несколько секунд."
    ),
    CODE_GENERIC: "Не удалось выполнить операцию. Обратитесь в поддержку.",
}


@dataclass
class SyncResult:
    """Итог сверки лимита с панелью: какой лимит выставлен и сколько устройств пришлось отключить."""
    limit: int
    removed: int = 0


@dataclass
class SlotQuote:
    """Расчёт докупки: сколько слотов, за сколько и до какой даты."""
    slots: int = 0
    price: float = 0.0
    price_per_slot: int = 0
    remaining_days: int = 0
    valid_until: datetime | None = None
    current_extra: int = 0
    base_limit: int = DEFAULT_BASE_LIMIT
    max_extra: int = DEFAULT_MAX_EXTRA
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None

    @property
    def error(self) -> str | None:
        return SLOT_NOTICES.get(self.error_code) if self.error_code else None

    @property
    def available(self) -> int:
        """Сколько слотов ещё можно докупить."""
        return max(0, self.max_extra - self.current_extra)

    @property
    def total_limit(self) -> int:
        """Каким станет лимит устройств после оплаты."""
        return self.base_limit + self.current_extra + self.slots


class DeviceSlotService:
    def __init__(self, user_repo: UserRepository, settings_repo: SettingsRepository,
                 remnawave: RemnawaveClient):
        self._user_repo = user_repo
        self._settings_repo = settings_repo
        self._remnawave = remnawave

    # --- настройки -----------------------------------------------------------

    async def settings(self) -> DeviceSettings:
        return DeviceSettings(
            price=await self._settings_repo.get_int(SETTING_PRICE, DEFAULT_PRICE),
            base_limit=await self._settings_repo.get_int(SETTING_BASE_LIMIT, DEFAULT_BASE_LIMIT),
            max_extra=await self._settings_repo.get_int(SETTING_MAX_EXTRA, DEFAULT_MAX_EXTRA),
        )

    # --- расчёт --------------------------------------------------------------

    async def quote(self, user_id: int, slots: int = 1) -> SlotQuote:
        """
        Считает стоимость докупки `slots` слотов до конца текущей подписки.

        Возвращает SlotQuote с error_code, если докупать нельзя — вызывающий код
        показывает текст из SLOT_NOTICES.
        """
        settings = await self.settings()
        user = await self._user_repo.get(user_id)
        if not user:
            return SlotQuote(error_code=CODE_GENERIC, base_limit=settings.base_limit,
                             max_extra=settings.max_extra)

        current_extra = user.extra_devices or 0
        quote = SlotQuote(
            current_extra=current_extra,
            base_limit=settings.base_limit,
            max_extra=settings.max_extra,
            price_per_slot=settings.price,
            valid_until=user.subscription_end_date,
        )

        remaining = days_left(user.subscription_end_date)
        quote.remaining_days = remaining
        if remaining <= 0:
            quote.error_code = CODE_NO_SUBSCRIPTION
            return quote

        # Триал — это подписка без единого платежа. Слоты продаём только тем,
        # кто уже платил: иначе 49 ₽ пришлось бы считать за семь пробных дней.
        if not user.is_first_payment_made:
            quote.error_code = CODE_TRIAL
            return quote

        if slots <= 0:
            quote.error_code = CODE_BAD_QUANTITY
            return quote

        if current_extra >= settings.max_extra:
            quote.error_code = CODE_MAX_REACHED
            return quote

        if slots > quote.available:
            quote.error_code = CODE_BAD_QUANTITY
            return quote

        quote.slots = slots
        quote.price_per_slot = prorated_slot_cost(settings, remaining)
        quote.price = prorated_slots_cost(settings, remaining, slots)
        return quote

    # --- применение ----------------------------------------------------------

    async def add_slots(self, user_id: int, slots: int) -> int:
        """
        Начисляет оплаченные слоты и доводит лимит в панели. Возвращает новый extra_devices.

        Отрицательный `slots` снимает слоты — так оформляется возврат за докупку.

        Потолок соблюдается и здесь: между выставлением счёта и вебхуком человек
        мог докупить слоты другим платежом.
        """
        settings = await self.settings()
        user = await self._user_repo.get(user_id)
        current = (user.extra_devices or 0) if user else 0
        target = max(0, min(settings.max_extra, current + slots))

        if target != current:
            await self._user_repo.set_extra_devices(user_id, target)

        await self.sync_limit(user_id, extra_devices=target)
        logger.info("[slots] user %s: extra_devices %s → %s (+%s оплачено)",
                    user_id, current, target, slots)
        return target

    async def set_slots(self, user_id: int, slots: int) -> int:
        """
        Выставляет количество слотов абсолютным значением (покупка/продление тарифа).

        Количество человек выбирает на чекауте, поэтому уменьшение применяется
        сразу — это его собственное осознанное действие, а не истечение срока.
        """
        settings = await self.settings()
        target = max(0, min(settings.max_extra, slots))
        await self._user_repo.set_extra_devices(user_id, target)
        await self.sync_limit(user_id, extra_devices=target)
        return target

    async def sync_limit(self, user_id: int, extra_devices: int | None = None) -> SyncResult | None:
        """
        Приводит hwidDeviceLimit в панели к состоянию БД и чистит лишние устройства.

        Возвращает SyncResult либо None, если синхронизировать не удалось
        (тогда это подхватит следующий прогон джоба).
        """
        user = await self._user_repo.get(user_id)
        if not user or not user.vpn_username:
            return None

        settings = await self.settings()
        extra = user.extra_devices if extra_devices is None else extra_devices
        subscription_active = days_left(user.subscription_end_date) > 0
        limit = device_limit(settings, extra or 0, subscription_active)

        try:
            rw_user = await self._remnawave.get_user_by_username(user.vpn_username)
            if not rw_user:
                return None

            uuid = rw_user.get("uuid") or user.remnawave_uuid
            if not uuid:
                return None

            if rw_user.get("hwidDeviceLimit") != limit:
                await self._remnawave.update_user(uuid, hwid_device_limit=limit)
                logger.info("[slots] user %s: hwidDeviceLimit → %s", user_id, limit)

            removed = await self._enforce_limit(uuid, limit, user_id)
        except RemnawaveTransportError as e:
            logger.warning("[slots] panel unavailable for user %s: %s", user_id, e.category)
            return None
        except Exception:
            logger.error("[slots] failed to sync limit for user %s", user_id, exc_info=True)
            return None

        return SyncResult(limit=limit, removed=removed)

    async def expire_slots(self, user_id: int) -> SyncResult | None:
        """Подписка истекла — слоты сгорают, лимит возвращается к базовому."""
        user = await self._user_repo.get(user_id)
        if not user or not (user.extra_devices or 0):
            return None

        await self._user_repo.set_extra_devices(user_id, 0)
        result = await self.sync_limit(user_id, extra_devices=0)
        logger.info("[slots] user %s: слоты сгорели вместе с подпиской", user_id)
        return result

    async def _enforce_limit(self, uuid: str, limit: int, user_id: int) -> int:
        """
        Удаляет устройства сверх лимита, начиная с самых давно не заходивших.

        Возвращает количество удалённых. Панель сама этого не делает: её проверка
        срабатывает только при регистрации нового hwid.
        """
        devices = await self._remnawave.get_user_devices(uuid)
        if len(devices) <= limit:
            return 0

        def last_seen(device: dict) -> str:
            # Строки ISO сравниваются лексикографически в том же порядке, что и даты.
            # Устройство без отметки считаем самым старым — оно и уйдёт первым.
            return device.get("updatedAt") or device.get("createdAt") or ""

        ordered = sorted(devices, key=last_seen, reverse=True)
        excess = ordered[limit:]

        removed = 0
        for device in excess:
            hwid = device.get("hwid")
            if not hwid:
                continue
            await self._remnawave.delete_user_device(uuid, hwid)
            removed += 1

        if removed:
            logger.info("[slots] user %s: удалено %s устройств сверх лимита %s",
                        user_id, removed, limit)
        return removed
