"""Пакеты в чекауте: серверная цена, без скидки на дополнительные ГБ."""
from .traffic_pricing import packs_cost_for_tariff, packs_to_gb, tariff_quota_gb


async def add_traffic(checkout, tariff, packs, service):
    settings = await service.settings()
    quota = tariff_quota_gb(settings, getattr(tariff, 'data_limit_gb', None))
    if not quota or settings.pack_price <= 0 or settings.pack_gb <= 0:
        packs = 0
    packs = max(0, min(settings.max_packs, packs))
    return checkout.with_traffic(packs, packs_cost_for_tariff(settings, tariff.duration_days, packs),
                                 packs_to_gb(settings, packs))
