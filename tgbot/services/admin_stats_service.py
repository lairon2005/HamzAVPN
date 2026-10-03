import asyncio

from database.repositories.stats import StatsRepository
from database.repositories.payment import PaymentRepository
from database.repositories.lifecycle import LifecycleRepository
from remnawave.client import RemnawaveClient


async def _safe(coro, default):
    """Оборачивает корутину: сетевая/панельная ошибка не роняет весь dashboard."""
    try:
        return await coro
    except Exception:
        return default


class AdminStatsService:
    def __init__(self, stats_repo: StatsRepository, remnawave: RemnawaveClient,
                 payment_repo: PaymentRepository,
                 lifecycle_repo: LifecycleRepository | None = None):
        self._stats_repo = stats_repo
        self._remnawave = remnawave
        self._payment_repo = payment_repo
        self._lifecycle_repo = lifecycle_repo

    async def get_dashboard_stats(self) -> dict:
        """Агрегирует статистику из БД и Remnawave для админ-панели.

        Вызовы к панели Remnawave (онлайн, ноды, метрики нод, список VPN-
        аккаунтов) выполняются конкурентно через asyncio.gather — хендлер
        должен уложиться в окно ответа Telegram (~15с), а падение одного
        запроса (например, панель не отдаёт nodes-metrics) не должно ронять
        остальную статистику — для каждого используется мягкая деградация.
        """
        system_stats, nodes, vpn_accounts = await asyncio.gather(
            _safe(self._remnawave.get_system_stats(), {
                "online_now": 0, "users_total": 0, "status_counts": {}, "nodes_online": 0,
            }),
            _safe(self._remnawave.get_nodes(), []),
            _safe(self._remnawave.get_all_users(), []),
        )
        vpn_accounts_total = len(vpn_accounts)
        # Ноды уже содержат cpu_usage/ram_usage (get_nodes читает их из node.system).

        return {
            "total_users": await self._stats_repo.count_all_users(),
            "active_subs": await self._stats_repo.count_active_subscriptions(),
            "first_payments": await self._stats_repo.count_users_with_first_payment(),
            "users_today": await self._stats_repo.count_new_users_for_period(1),
            "users_week": await self._stats_repo.count_new_users_for_period(7),
            "users_month": await self._stats_repo.count_new_users_for_period(30),
            # Реальное кол-во VPN-аккаунтов в панели Remnawave (get_all_users()).
            "vpn_accounts_total": vpn_accounts_total,
            # Реальный онлайн из Remnawave (GET /api/system/stats).
            "online_now": system_stats["online_now"],
            # Ноды панели с смёрженными CPU/RAM метриками (best-effort).
            "nodes": nodes,
            # Revenue stats — календарные периоды по МСК.
            "revenue_today": await self._payment_repo.get_revenue_for_period("day"),
            "revenue_week": await self._payment_repo.get_revenue_for_period("week"),
            "revenue_month": await self._payment_repo.get_revenue_for_period("month"),
            "revenue_year": await self._payment_repo.get_revenue_for_period("year"),
            "revenue_total": await self._payment_repo.get_total_revenue(),
            # Доход Stars (XTR) — отдельно от рублёвого, не смешивается в суммах выше.
            "stars_revenue_total": await self._payment_repo.get_stars_revenue_total(),
        }

    async def get_cohort_metrics(self) -> dict:
        """
        7 еженедельных метрик когорт продлений из стратегии монетизации (M3).
        Первая задача после автопродления: агрегатная статистика прячет утечку
        пользователей, поэтому здесь считаются именно когортные показатели.

        Возвращает dict вида {metric_key: {value, target, achieved, ...}}.
        """
        mrr = await self._payment_repo.get_mrr()
        renewal_cohort = await self._stats_repo.get_weekly_renewal_cohort()
        first_payments_month = await self._payment_repo.count_first_payments_for_period(30)
        users_month = await self._stats_repo.count_new_users_for_period(30)
        referral_starts_month = await self._stats_repo.count_referral_starts_for_period(30)
        active_subs = await self._stats_repo.count_active_subscriptions()
        long_tariff = await self._payment_repo.get_long_tariff_share(30)
        winback_returns = await self._get_winback_returns()

        # K-фактор = сколько новых реф-стартов приходится на одного активного
        # пользователя за месяц (виральность реферальной программы).
        k_factor = (referral_starts_month / active_subs) if active_subs else 0.0

        return {
            "mrr": {
                "label": "MRR (за календарный месяц)",
                "value": mrr["revenue"],
                "target": 95_000,
                "achieved": mrr["revenue"] >= 95_000,
            },
            "renewal_rate": {
                "label": "Доля продлений (когорта прошлой недели)",
                "value": renewal_cohort["rate"],
                "expired": renewal_cohort["expired"],
                "renewed": renewal_cohort["renewed"],
                "target": 0.40,
                "achieved": renewal_cohort["rate"] >= 0.40,
            },
            "first_payments": {
                "label": "Первые оплаты (новые платящие за месяц)",
                "value": first_payments_month,
                "target": 250,
                "achieved": first_payments_month >= 250,
            },
            "inflow": {
                "label": "Приток (старты бота за месяц)",
                "value": users_month,
                "target": 200,
                "achieved": users_month >= 200,
            },
            "k_factor": {
                "label": "K-фактор рефералки",
                "value": k_factor,
                "referral_starts": referral_starts_month,
                "active_subs": active_subs,
                "target": 0.25,
                "achieved": k_factor >= 0.25,
            },
            "long_tariff_share": {
                "label": "Доля длинных тарифов (3 мес+, за месяц)",
                "value": long_tariff["share"],
                "long": long_tariff["long"],
                "total": long_tariff["total"],
                "target": 0.35,
                "achieved": long_tariff["share"] >= 0.35,
            },
            "winback_returns": {
                "label": "Возвраты win-back",
                "value": winback_returns,
                "target": 40,
                "achieved": (winback_returns or 0) >= 40,
            },
        }

    async def _get_winback_returns(self) -> int | None:
        # Метрика №7 (возвраты win-back): вернувшиеся из ушедших — уникальные
        # пользователи, получившие касание серии 'winback' и снова активные.
        # Источник — lifecycle-подсистема (LifecycleRepository). Если репозиторий
        # не подключён (напр. в тестах) — возвращаем None, экран покажет "н/д".
        if self._lifecycle_repo is None:
            return None
        return await self._lifecycle_repo.count_returned_after_winback()
