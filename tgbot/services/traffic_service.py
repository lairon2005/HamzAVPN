"""Настройки и серверный расчёт докупки трафика.

Квота — из каталога HamzAVPN или панели для старых пользователей. Денежная
выдача, возвраты и синхронизация выполняются транзакционно PaymentRepository.
"""
from dataclasses import dataclass
from datetime import datetime

from database.repositories.settings import SettingsRepository
from database.repositories.user import UserRepository
from loader import logger
from remnawave.client import RemnawaveClient, RemnawaveTransportError
from tgbot.services.device_pricing import days_left
from tgbot.services.traffic_pricing import (
    DEFAULT_BASE_TRAFFIC_GB,
    DEFAULT_MAX_PACKS,
    DEFAULT_PACK_GB,
    DEFAULT_PACK_PRICE,
    GIB,
    SETTING_BASE_TRAFFIC,
    SETTING_MAX_PACKS,
    SETTING_PACK_GB,
    SETTING_PACK_PRICE,
    TrafficSettings,
    gb_to_packs,
    prorated_pack_cost,
    prorated_packs_cost,
    tariff_quota_gb,
)

CODE_NO_SUBSCRIPTION = "no_subscription"
CODE_TRIAL = "trial"
CODE_UNLIMITED = "unlimited"
CODE_MAX_REACHED = "max_reached"
CODE_BAD_QUANTITY = "bad_quantity"
CODE_PANEL = "panel"
CODE_GENERIC = "generic"
CODE_DISABLED = "disabled"

TRAFFIC_NOTICES = {
    CODE_DISABLED: "Пакеты трафика временно недоступны.",
    CODE_NO_SUBSCRIPTION: (
        "Докупить трафик можно только при активной подписке. "
        "Оформите подписку — и сможете добавить гигабайты."
    ),
    CODE_TRIAL: (
        "На пробном периоде доступна только базовая квота. "
        "Оплатите любой тариф — и сможете докупить трафик."
    ),
    CODE_UNLIMITED: "У вашего тарифа безлимитный трафик — докупать нечего.",
    CODE_MAX_REACHED: "Вы уже докупили максимальный объём трафика.",
    CODE_BAD_QUANTITY: "Некорректное количество пакетов.",
    CODE_PANEL: (
        "VPN-панель временно недоступна. "
        "Пожалуйста, повторите попытку через несколько секунд."
    ),
    CODE_GENERIC: "Не удалось выполнить операцию. Обратитесь в поддержку.",
}


@dataclass
class SyncResult:
    """Итог сверки: какой лимит (ГБ, 0 — безлимит) выставлен в панели."""
    limit_gb: int
    changed: bool = False


@dataclass
class TrafficQuote:
    """Расчёт докупки: сколько пакетов, за сколько и до какой даты."""
    packs: int = 0
    price: float = 0.0
    price_per_pack: int = 0
    pack_gb: int = DEFAULT_PACK_GB
    remaining_days: int = 0
    valid_until: datetime | None = None
    quota_gb: int = DEFAULT_BASE_TRAFFIC_GB
    current_extra_gb: int = 0
    current_packs: int = 0
    max_packs: int = DEFAULT_MAX_PACKS
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None

    @property
    def error(self) -> str | None:
        return TRAFFIC_NOTICES.get(self.error_code) if self.error_code else None

    @property
    def available(self) -> int:
        """Сколько пакетов ещё можно докупить."""
        return max(0, self.max_packs - self.current_packs)

    @property
    def added_gb(self) -> int:
        return self.packs * self.pack_gb

    @property
    def current_limit_gb(self) -> int:
        return self.quota_gb + self.current_extra_gb

    @property
    def total_limit_gb(self) -> int:
        """Каким станет лимит после оплаты."""
        return self.quota_gb + self.current_extra_gb + self.added_gb


class TrafficService:
    def __init__(self, user_repo: UserRepository, settings_repo: SettingsRepository,
                 remnawave: RemnawaveClient):
        self._user_repo = user_repo
        self._settings_repo = settings_repo
        self._remnawave = remnawave

    # --- настройки -----------------------------------------------------------

    async def settings(self) -> TrafficSettings:
        return TrafficSettings(
            base_gb=await self._settings_repo.get_int(SETTING_BASE_TRAFFIC, DEFAULT_BASE_TRAFFIC_GB),
            pack_gb=await self._settings_repo.get_int(SETTING_PACK_GB, DEFAULT_PACK_GB),
            pack_price=await self._settings_repo.get_int(SETTING_PACK_PRICE, DEFAULT_PACK_PRICE),
            max_packs=await self._settings_repo.get_int(SETTING_MAX_PACKS, DEFAULT_MAX_PACKS),
        )

    async def base_gb(self) -> int:
        """Базовая квота, ГБ/мес — для новых пользователей и триала."""
        return await self._settings_repo.get_int(SETTING_BASE_TRAFFIC, DEFAULT_BASE_TRAFFIC_GB)

    async def tariff_quota(self, data_limit_gb: int | None) -> int:
        """Квота тарифа в ГБ/мес (NULL или 0 → безлимит)."""
        return tariff_quota_gb(await self.settings(), data_limit_gb)

    # --- квота пользователя --------------------------------------------------

    async def _resolve_quota(self, user) -> int | None:
        """
        Квота подписки, ГБ. Если в БД её ещё нет (подписка выдана до появления
        докупки) — берём лимит из панели и запоминаем. None — панель не ответила.
        """
        if user.subscription_data_limit_gb is not None:
            return user.subscription_data_limit_gb
        if not user.vpn_username:
            return None

        rw_user = await self._remnawave.get_user_by_username(user.vpn_username)
        if not rw_user:
            return None
        limit_bytes = int(rw_user.get("trafficLimitBytes") or 0)
        quota = max(0, limit_bytes // GIB - (user.extra_traffic_gb or 0))
        await self._user_repo.set_traffic_quota(user.user_id, quota)
        return quota

    # --- расчёт --------------------------------------------------------------

    async def quote(self, user_id: int, packs: int = 1) -> TrafficQuote:
        """Стоимость докупки `packs` пакетов до конца текущей подписки."""
        settings = await self.settings()
        user = await self._user_repo.get(user_id)
        base = dict(pack_gb=settings.pack_gb, max_packs=settings.max_packs,
                    price_per_pack=settings.pack_price)
        if not user:
            return TrafficQuote(error_code=CODE_GENERIC, **base)

        current_extra = user.extra_traffic_gb or 0
        quote = TrafficQuote(
            current_extra_gb=current_extra,
            current_packs=gb_to_packs(settings, current_extra),
            valid_until=user.subscription_end_date,
            **base,
        )

        remaining = days_left(user.subscription_end_date)
        quote.remaining_days = remaining
        if remaining <= 0:
            quote.error_code = CODE_NO_SUBSCRIPTION
            return quote

        # Триал — подписка без единого платежа: докупку не продаём.
        if not user.is_first_payment_made:
            quote.error_code = CODE_TRIAL
            return quote

        try:
            quota = await self._resolve_quota(user)
        except RemnawaveTransportError:
            quote.error_code = CODE_PANEL
            return quote
        except Exception:
            logger.error("[traffic] failed to resolve quota for %s", user_id, exc_info=True)
            quote.error_code = CODE_GENERIC
            return quote
        if quota is None:
            quote.error_code = CODE_GENERIC
            return quote
        if quota <= 0:
            quote.error_code = CODE_UNLIMITED
            return quote
        quote.quota_gb = quota

        if packs <= 0:
            quote.error_code = CODE_BAD_QUANTITY
            return quote
        if quote.current_packs >= settings.max_packs:
            quote.error_code = CODE_MAX_REACHED
            return quote
        if packs > quote.available:
            quote.error_code = CODE_BAD_QUANTITY
            return quote

        if settings.pack_gb <= 0 or settings.pack_price <= 0:
            quote.error_code = CODE_DISABLED
            return quote

        quote.packs = packs
        quote.price_per_pack = prorated_pack_cost(settings, remaining)
        quote.price = prorated_packs_cost(settings, remaining, packs)
        return quote

