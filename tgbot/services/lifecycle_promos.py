# tgbot/services/lifecycle_promos.py
"""
Идемпотентная инициализация промокодов, на которые ссылаются lifecycle-серии
(§7.2 BACK30, §7.3 COMEBACK, §7.4 START20) и гейт канала (§7.6 CHANNEL15).
Вызывается один раз при старте бота (см. bot.py/on_startup) — если код уже существует
(get_by_code), ничего не меняем, чтобы не затирать ручные правки администратора
(uses_left, discount_percent и т.д.).
"""
from datetime import datetime, timedelta

from database import promo_repo, tariff_repo
from loader import logger

DEFAULT_TTL_HOURS = 72

# Указанные в документе ориентиры на случай, если в БД ещё нет ни одного тарифа
FALLBACK_BASE_MONTHLY_PRICE = 149.0
COMEBACK_TARGET_PRICE = 99.0


async def _resolve_base_monthly_price() -> float:
    """
    Ищет тариф с длительностью, ближайшей к 30 дням — он считается "базовым месячным"
    для расчёта скидки COMEBACK (99 ₽ вместо цены этого тарифа). Модель PromoCode не
    поддерживает фиксированную цену — только discount_percent, поэтому "99 вместо 149"
    смоделировано как скидка в %, посчитанная от базового тарифа.
    """
    tariffs = await tariff_repo.get_all()
    if not tariffs:
        return FALLBACK_BASE_MONTHLY_PRICE
    monthly = min(tariffs, key=lambda t: abs(t.duration_days - 30))
    return monthly.price if monthly.price > 0 else FALLBACK_BASE_MONTHLY_PRICE


async def ensure_lifecycle_promo_codes() -> None:
    """Идемпотентно создаёт BACK30 / COMEBACK / START20 / CHANNEL15, если их ещё нет в БД."""
    try:
        if not await promo_repo.get_by_code('BACK30'):
            await promo_repo.create(
                code='BACK30', bonus_days=0, discount_percent=30,
                max_uses=1_000_000, expire_date=datetime.now() + timedelta(hours=DEFAULT_TTL_HOURS),
            )
            logger.info("Lifecycle: создан промокод BACK30 (-30%, §7.2 D+5).")

        if not await promo_repo.get_by_code('COMEBACK'):
            base_price = await _resolve_base_monthly_price()
            discount_percent = max(0, min(95, round((1 - COMEBACK_TARGET_PRICE / base_price) * 100)))
            await promo_repo.create(
                code='COMEBACK', bonus_days=0, discount_percent=discount_percent,
                max_uses=1_000_000, expire_date=datetime.now() + timedelta(hours=DEFAULT_TTL_HOURS),
            )
            logger.info(
                f"Lifecycle: создан промокод COMEBACK (-{discount_percent}%, смоделирован как "
                f"99₽ вместо {base_price}₽ базового месячного тарифа, §7.3 волна 2)."
            )

        if not await promo_repo.get_by_code('START20'):
            await promo_repo.create(
                code='START20', bonus_days=0, discount_percent=20,
                max_uses=1_000_000, expire_date=datetime.now() + timedelta(hours=DEFAULT_TTL_HOURS),
            )
            logger.info("Lifecycle: создан промокод START20 (-20%, §7.4 касание 3).")

        if not await promo_repo.get_by_code('CHANNEL15'):
            # В отличие от BACK30/COMEBACK/START20 (персональные касания с TTL) — CHANNEL15
            # публикуется в промо-постах канала и должен жить долго, поэтому expire_date=None
            # (промокод не истекает; см. promo_code_service.validate — expire_date=None не проверяется).
            await promo_repo.create(
                code='CHANNEL15', bonus_days=0, discount_percent=15,
                max_uses=1_000_000, expire_date=None,
            )
            logger.info("Lifecycle: создан промокод CHANNEL15 (-15%, §7.6 гейт канала, бессрочный).")
    except Exception as e:
        logger.error(f"Lifecycle: ошибка при идемпотентной инициализации промокодов: {e}", exc_info=True)
