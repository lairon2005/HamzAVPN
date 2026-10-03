"""
Расчёт стоимости дополнительных устройств.

Модель простая: базовый лимит входит в тариф, каждое устройство сверх него
стоит фиксированную сумму В МЕСЯЦ. Отсюда два способа посчитать цену:

  * вместе с тарифом (покупка/продление/автосписание) —
    `slots_cost_for_tariff`: цена слота × месяцев тарифа × количество слотов.
    Тариф на 3 месяца за 399 ₽ с одним доп. устройством = 399 + 49×3.

  * докупка в середине оплаченного периода — `prorated_slots_cost`:
    платим только за остаток срока, потому что слот всё равно сгорит вместе
    с подпиской. Без этого слот на годовом тарифе стоил бы 49 ₽ за 12 месяцев.

Минимальный чек за слот — полная месячная цена: платёж на 9 ₽ съедается
комиссией эквайринга и фискализацией, а «докупить за 9 ₽ в последний день,
чтобы потом продлеваться уже со слотом» — тоже лазейка.

Настройки (цена, база, потолок) лежат в app_settings и правятся из админки,
дефолты ниже — то, что было зашито в панели на момент запуска фичи.
"""
from dataclasses import dataclass
from datetime import datetime
from math import ceil

# Ключи в таблице app_settings
SETTING_PRICE = "extra_device_price"
SETTING_BASE_LIMIT = "base_device_limit"
SETTING_MAX_EXTRA = "max_extra_devices"

# Дефолты: 5 устройств в тарифе (== fallbackDeviceLimit в панели),
# 49 ₽/мес за слот, не больше +5 слотов на аккаунт.
DEFAULT_PRICE = 49
DEFAULT_BASE_LIMIT = 5
DEFAULT_MAX_EXTRA = 5

# Расчётный месяц. Тот же, что у тарифов: 30 дней.
DAYS_IN_MONTH = 30


@dataclass(frozen=True)
class DeviceSettings:
    """Снимок настроек на момент расчёта — чтобы цена не поменялась между экраном и счётом."""
    price: int = DEFAULT_PRICE
    base_limit: int = DEFAULT_BASE_LIMIT
    max_extra: int = DEFAULT_MAX_EXTRA


def billing_months(duration_days: int) -> int:
    """
    Сколько месяцев в тарифе для расчёта платы за устройства.

    Округляем к ближайшему целому (365 дней → 12 месяцев, а не 12.17),
    минимум — один месяц: тариф короче месяца всё равно оплачивает слот целиком.
    """
    return max(1, round((duration_days or 0) / DAYS_IN_MONTH))


def slots_cost_for_tariff(settings: DeviceSettings, duration_days: int, slots: int) -> float:
    """Плата за слоты в составе покупки/продления тарифа."""
    if slots <= 0:
        return 0.0
    return float(settings.price * billing_months(duration_days) * slots)


def days_left(subscription_end_date: datetime | None, now: datetime | None = None) -> int:
    """
    Сколько оплаченных дней осталось. 0 — подписки нет или она истекла.

    Округляем вверх: начатый день считается оплаченным.
    """
    if not subscription_end_date:
        return 0
    base = now or datetime.now()
    delta = subscription_end_date - base
    if delta.total_seconds() <= 0:
        return 0
    return max(1, ceil(delta.total_seconds() / 86400))


def prorated_slot_cost(settings: DeviceSettings, remaining_days: int) -> int:
    """Цена ОДНОГО слота до конца текущего периода, не меньше месячной."""
    prorated = ceil(settings.price * remaining_days / DAYS_IN_MONTH)
    return max(settings.price, prorated)


def prorated_slots_cost(settings: DeviceSettings, remaining_days: int, slots: int) -> float:
    """Цена докупки `slots` слотов до конца текущего периода."""
    if slots <= 0:
        return 0.0
    return float(prorated_slot_cost(settings, remaining_days) * slots)


def device_limit(settings: DeviceSettings, extra_devices: int, subscription_active: bool) -> int:
    """
    Абсолютный лимит устройств для записи в панель.

    Слоты живут по дате подписки: подписка кончилась — остаётся база.
    """
    if not subscription_active:
        return settings.base_limit
    return settings.base_limit + max(0, extra_devices)


@dataclass(frozen=True)
class Checkout:
    """Итог чекаута: тариф со скидкой + слоты устройств по полной цене."""
    tariff_original: float
    tariff_final: float
    slots: int
    slots_cost: float
    months: int

    @property
    def total(self) -> float:
        return round(self.tariff_final + self.slots_cost, 2)

    @property
    def original_total(self) -> float:
        return round(self.tariff_original + self.slots_cost, 2)

    @property
    def has_slots(self) -> bool:
        return self.slots > 0 and self.slots_cost > 0


def build_checkout(settings: DeviceSettings, tariff_price: float, duration_days: int,
                   slots: int, discount_percent: int = 0) -> Checkout:
    """
    Считает сумму счёта за тариф вместе с доп. устройствами.

    Промо-скидка применяется ТОЛЬКО к тарифу: слоты — отдельная услуга с
    фиксированной ценой, иначе «-50%» на годовом тарифе с пятью слотами резал бы
    и её тоже. `tariff_price` уже должен быть эффективным (лоялти-цена, §7.1).
    """
    tariff_final = tariff_price
    if discount_percent > 0:
        tariff_final = round(tariff_price * (1 - discount_percent / 100), 2)

    slots = max(0, slots)
    return Checkout(
        tariff_original=round(tariff_price, 2),
        tariff_final=tariff_final,
        slots=slots,
        slots_cost=slots_cost_for_tariff(settings, duration_days, slots),
        months=billing_months(duration_days),
    )


def receipt_items(tariff_name: str, checkout: Checkout, duration_days: int) -> list[dict]:
    """Позиции заказа: тариф и дополнительные устройства показываются раздельно."""
    items = [{
        "description": f"Подписка VPN: {tariff_name}",
        "quantity": 1,
        "amount": checkout.tariff_final,
    }]
    if checkout.has_slots:
        items.append({
            "description": f"Дополнительные устройства ({duration_days} дн.)",
            "quantity": checkout.slots,
            "amount": checkout.slots_cost / checkout.slots,
        })
    return items


def format_slots_line(settings: DeviceSettings, duration_days: int, slots: int) -> str:
    """Строка для чека и карточки счёта: «Доп. устройства: 2 × 49 ₽ × 3 мес = 294 ₽»."""
    months = billing_months(duration_days)
    total = slots_cost_for_tariff(settings, duration_days, slots)
    return (
        f"Доп. устройства: {slots} × {settings.price} ₽ × {months} мес = {total:.0f} ₽"
    )
