from datetime import datetime, timedelta

from sqlalchemy import select, func

from db import User, Payment

# Грейс-окно для когорты продлений: продление засчитывается, если платёж прошёл
# не раньше, чем за RENEWAL_WINDOW_BEFORE_DAYS до истечения подписки (досрочная
# оплата), и не позже, чем через RENEWAL_WINDOW_AFTER_DAYS после истечения
# (просрочка, но пользователь ещё вернулся).
RENEWAL_WINDOW_BEFORE_DAYS = 3
RENEWAL_WINDOW_AFTER_DAYS = 14


class StatsRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def count_all_users(self) -> int:
        async with self._session_maker() as session:
            stmt = select(func.count()).select_from(User)
            result = await session.execute(stmt)
            return result.scalar_one()

    async def count_new_users_for_period(self, days: int) -> int:
        async with self._session_maker() as session:
            start_date = datetime.now() - timedelta(days=days)
            stmt = select(func.count()).select_from(User).where(User.reg_date >= start_date)
            result = await session.execute(stmt)
            return result.scalar_one()

    async def count_active_subscriptions(self) -> int:
        async with self._session_maker() as session:
            stmt = select(func.count()).select_from(User).where(
                User.subscription_end_date.is_not(None),
                User.subscription_end_date > datetime.now()
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def count_user_referrals(self, user_id: int) -> int:
        async with self._session_maker() as session:
            stmt = select(func.count()).select_from(User).where(User.referrer_id == user_id)
            result = await session.execute(stmt)
            return result.scalar_one()

    @staticmethod
    def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
        start = datetime(year, month, 1)
        end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
        return start, end

    async def get_monthly_referral_leaderboard(self, year: int, month: int, limit: int = 10) -> list[tuple[int, int]]:
        """
        Лидерборд реферальной программы за календарный месяц (§7.5): топ рефереров
        по числу приглашённых, зарегистрированных в указанном месяце.
        Возвращает [(referrer_id, count), ...] по убыванию count.
        """
        start, end = self._month_bounds(year, month)
        async with self._session_maker() as session:
            stmt = (
                select(User.referrer_id, func.count().label("cnt"))
                .where(
                    User.referrer_id.is_not(None),
                    User.reg_date >= start,
                    User.reg_date < end,
                )
                .group_by(User.referrer_id)
                .order_by(func.count().desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [(row[0], row[1]) for row in result.all()]

    async def count_user_referrals_in_month(self, user_id: int, year: int, month: int) -> int:
        """Число приглашённых конкретным пользователем за указанный календарный месяц."""
        start, end = self._month_bounds(year, month)
        async with self._session_maker() as session:
            stmt = select(func.count()).select_from(User).where(
                User.referrer_id == user_id,
                User.reg_date >= start,
                User.reg_date < end,
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def get_user_referrals(self, user_id: int) -> list[User]:
        async with self._session_maker() as session:
            stmt = select(User).where(User.referrer_id == user_id)
            result = await session.execute(stmt)
            return result.scalars().all()

    async def count_users_with_first_payment(self) -> int:
        async with self._session_maker() as session:
            stmt = select(func.count()).select_from(User).where(User.is_first_payment_made == True)
            result = await session.execute(stmt)
            return result.scalar_one()

    async def count_referral_starts_for_period(self, days: int) -> int:
        """
        Метрика №5 (числитель K-фактора): «старты по реф-ссылкам» —
        пользователи с заполненным referrer_id, зарегистрированные за период.
        """
        async with self._session_maker() as session:
            start_date = datetime.now() - timedelta(days=days)
            stmt = select(func.count()).select_from(User).where(
                User.reg_date >= start_date,
                User.referrer_id.is_not(None),
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def get_weekly_renewal_cohort(self) -> dict:
        """
        Метрика №2 — когорта продлений за прошлую неделю.

        «Истёкшие за неделю» — пользователи, у которых subscription_end_date
        попал в окно [сегодня-14д; сегодня-7д), т.е. подписка закончилась
        ровно на прошлой неделе (когортным способом, а не агрегатом).

        «Продлившие» — из этой когорты те, кто сделал успешный ПОВТОРНЫЙ
        платёж (не первый в жизни платёж пользователя) в грейс-окне вокруг
        даты истечения: [subscription_end_date - 3д; subscription_end_date + 14д].
        Платёж считается повторным, если у пользователя уже был более ранний
        успешный платёж (сравнение по completed_at).
        """
        async with self._session_maker() as session:
            now = datetime.now()
            week_start = now - timedelta(days=14)
            week_end = now - timedelta(days=7)

            expired_stmt = select(func.count()).select_from(User).where(
                User.subscription_end_date.is_not(None),
                User.subscription_end_date >= week_start,
                User.subscription_end_date < week_end,
            )
            expired_result = await session.execute(expired_stmt)
            expired_count = expired_result.scalar_one()

            # Дата первого успешного платежа каждого пользователя (для отсечения "первых" оплат)
            first_payment_subq = (
                select(
                    Payment.user_id.label("user_id"),
                    func.min(Payment.completed_at).label("first_completed_at"),
                )
                .where(Payment.status == "succeeded", Payment.completed_at.is_not(None))
                .group_by(Payment.user_id)
                .subquery()
            )

            renewed_stmt = (
                select(func.count(func.distinct(User.user_id)))
                .select_from(User)
                .join(Payment, Payment.user_id == User.user_id)
                .join(first_payment_subq, first_payment_subq.c.user_id == User.user_id)
                .where(
                    User.subscription_end_date.is_not(None),
                    User.subscription_end_date >= week_start,
                    User.subscription_end_date < week_end,
                    Payment.status == "succeeded",
                    Payment.completed_at.is_not(None),
                    Payment.completed_at > first_payment_subq.c.first_completed_at,
                    Payment.completed_at >= User.subscription_end_date - timedelta(days=RENEWAL_WINDOW_BEFORE_DAYS),
                    Payment.completed_at <= User.subscription_end_date + timedelta(days=RENEWAL_WINDOW_AFTER_DAYS),
                )
            )
            renewed_result = await session.execute(renewed_stmt)
            renewed_count = renewed_result.scalar_one()

            rate = (renewed_count / expired_count) if expired_count else 0.0

            return {
                "expired": expired_count,
                "renewed": renewed_count,
                "rate": rate,
                "cohort_week_start": week_start,
                "cohort_week_end": week_end,
            }
