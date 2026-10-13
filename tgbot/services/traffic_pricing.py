"""
Квота трафика и расчёт стоимости докупленных пакетов ГБ.

Схема повторяет доп. устройства (см. device_pricing.py):

  * квота берётся из активного каталога тарифов HamzAVPN;
    `data_limit_gb` = NULL или 0 означает безлимит, N — N ГБ/мес;
  * сверху докупаются пакеты (по умолчанию +100 ГБ/мес за 49 ₽/мес);
  * вместе с тарифом пакет стоит `цена × месяцев × пакетов`, в середине периода —
    по остатку дней, но не дешевле месячной цены (пакет всё равно сгорит вместе
    с подпиской, а платёж на копейки съедается комиссией эквайринга).

Докупленные ГБ живут до `subscription_end_date`: это прибавка к ежемесячной
квоте, а не разовый остаток. Безлимитному тарифу докупать нечего.

Все функции чистые — их проверяют тесты без БД и панели.
"""
from dataclasses import dataclass
from math import ceil

from tgbot.services.device_pricing import billing_months

# Ключи в таблице app_settings
SETTING_BASE_TRAFFIC = "base_traffic_gb"
SETTING_PACK_GB = "traffic_pack_gb"
SETTING_PACK_PRICE = "traffic_pack_price"
SETTING_MAX_PACKS = "max_traffic_packs"

DEFAULT_BASE_TRAFFIC_GB = 500
DEFAULT_PACK_GB = 100
DEFAULT_PACK_PRICE = 49
DEFAULT_MAX_PACKS = 10

# Расчётный месяц тот же, что у тарифов и слотов — 30 дней.
DAYS_IN_MONTH = 30

# Панель считает трафик в байтах; ГБ в этом проекте — гибибайты (как в extend()).
GIB = 1024 ** 3


@dataclass(frozen=True)
class TrafficSettings:
    """Снимок настроек на момент расчёта — цена не должна поменяться между экраном и счётом."""
    base_gb: int = DEFAULT_BASE_TRAFFIC_GB
    pack_gb: int = DEFAULT_PACK_GB
    pack_price: int = DEFAULT_PACK_PRICE
    max_packs: int = DEFAULT_MAX_PACKS

    @property
    def max_extra_gb(self) -> int:
        return self.pack_gb * self.max_packs


def tariff_quota_gb(settings: TrafficSettings, data_limit_gb: int | None) -> int:
    """
    Квота тарифа в ГБ/мес: NULL или 0 → безлимит (0), N → N.

    Сохраняем действующие квоты каталога HamzAVPN.
    """
    if data_limit_gb is None:
        return 0
    return max(0, int(data_limit_gb))


def is_unlimited(quota_gb: int | None) -> bool:
    return quota_gb == 0


def packs_to_gb(settings: TrafficSettings, packs: int) -> int:
    return max(0, packs) * settings.pack_gb


def gb_to_packs(settings: TrafficSettings, extra_gb: int) -> int:
    """Сколько пакетов соответствует докупленным ГБ (с округлением вверх)."""
    if extra_gb <= 0 or settings.pack_gb <= 0:
        return 0
    return ceil(extra_gb / settings.pack_gb)


def total_limit_gb(quota_gb: int, extra_gb: int, subscription_active: bool = True) -> int:
    """
    Лимит для записи в панель, ГБ. 0 — безлимит.

    Докупленное живёт по дате подписки: подписка кончилась — остаётся квота.
    """
    if quota_gb <= 0:
        return 0
    if not subscription_active:
        return quota_gb
    return quota_gb + max(0, extra_gb)


def packs_cost_for_tariff(settings: TrafficSettings, duration_days: int, packs: int) -> float:
    """Плата за пакеты в составе покупки/продления тарифа."""
    if packs <= 0:
        return 0.0
    return float(settings.pack_price * billing_months(duration_days) * packs)


def prorated_pack_cost(settings: TrafficSettings, remaining_days: int) -> int:
    """Цена ОДНОГО пакета до конца текущего периода, не меньше месячной."""
    prorated = ceil(settings.pack_price * remaining_days / DAYS_IN_MONTH)
    return max(settings.pack_price, prorated)


def prorated_packs_cost(settings: TrafficSettings, remaining_days: int, packs: int) -> float:
    if packs <= 0:
        return 0.0
    return float(prorated_pack_cost(settings, remaining_days) * packs)


def format_gb(gb: int) -> str:
    """500 → '500 ГБ', 1500 → '1500 ГБ'."""
    return f"{gb} ГБ"


def format_packs_line(settings: TrafficSettings, duration_days: int, packs: int) -> str:
    """Строка для карточки счёта: «Доп. трафик: 2 × 100 ГБ × 49 ₽ × 3 мес = 294 ₽»."""
    months = billing_months(duration_days)
    total = packs_cost_for_tariff(settings, duration_days, packs)
    return (
        f"Доп. трафик: {packs} × {settings.pack_gb} ГБ × {settings.pack_price} ₽ "
        f"× {months} мес = {total:.0f} ₽"
    )
