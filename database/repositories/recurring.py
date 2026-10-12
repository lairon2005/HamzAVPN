"""Рекуррентные подписки Platega (автопродление): хранение состояния и переходы статусов."""
from datetime import datetime, timedelta

from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError

from db import RecurringSubscription

LIVE = ('pending', 'active')


class RecurringRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def create(self, platega_subscription_id: str, user_id: int, tariff_id: int,
                     extra_devices: int, amount: int, payment_url: str | None = None,
                     source: str = 'bot') -> RecurringSubscription:
        async with self._session_maker() as session:
            sub = RecurringSubscription(
                platega_subscription_id=platega_subscription_id, user_id=user_id,
                tariff_id=tariff_id, extra_devices=extra_devices, amount=amount,
                payment_url=payment_url, source=source,
            )
            session.add(sub)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(sub)
            return sub

    async def get_by_platega_id(self, platega_subscription_id: str) -> RecurringSubscription | None:
        async with self._session_maker() as session:
            return await session.scalar(select(RecurringSubscription).where(
                RecurringSubscription.platega_subscription_id == platega_subscription_id))

    async def get_live_for_user(self, user_id: int) -> RecurringSubscription | None:
        async with self._session_maker() as session:
            return await session.scalar(select(RecurringSubscription).where(
                RecurringSubscription.user_id == user_id,
                RecurringSubscription.status.in_(LIVE)))

    async def get_last_for_user(self, user_id: int) -> RecurringSubscription | None:
        async with self._session_maker() as session:
            return await session.scalar(select(RecurringSubscription).where(
                RecurringSubscription.user_id == user_id
            ).order_by(RecurringSubscription.id.desc()).limit(1))

    async def set_status(self, platega_subscription_id: str, status: str,
                         next_charge_at: datetime | None = None) -> bool:
        """Переводит подписку в новый статус. True — статус действительно изменился.

        Возврат из cancelled/failed в другие статусы запрещён: отмена окончательна, а
        запоздавший callback о старой активации не должен её воскрешать. past_due
        «перманентный» — из него выходят только в cancelled (или повторным успешным
        списанием, которое идёт через record_charge).
        """
        async with self._session_maker() as session, session.begin():
            sub = await session.scalar(select(RecurringSubscription).where(
                RecurringSubscription.platega_subscription_id == platega_subscription_id
            ).with_for_update())
            if sub is None or sub.status == status:
                return False
            if sub.status in ('cancelled', 'failed'):
                return False
            if sub.status == 'past_due' and status not in ('cancelled', 'active'):
                return False
            now = datetime.now()
            sub.status = status
            if status == 'active':
                sub.activated_at = sub.activated_at or now
                if next_charge_at:
                    sub.next_charge_at = next_charge_at
            elif status in ('cancelled', 'failed', 'past_due'):
                sub.next_charge_at = None
                if status == 'cancelled':
                    sub.cancelled_at = now
            return True

    async def record_charge(self, platega_subscription_id: str,
                            next_charge_at: datetime | None) -> RecurringSubscription | None:
        """Успешное списание: подписка активна, счётчик растёт, ближайшее списание обновлено."""
        async with self._session_maker() as session, session.begin():
            sub = await session.scalar(select(RecurringSubscription).where(
                RecurringSubscription.platega_subscription_id == platega_subscription_id
            ).with_for_update())
            if sub is None:
                return None
            now = datetime.now()
            if sub.status in ('pending', 'past_due'):
                sub.status = 'active'
            sub.activated_at = sub.activated_at or now
            sub.last_charge_at = now
            sub.next_charge_at = next_charge_at
            sub.charges_ok = (sub.charges_ok or 0) + 1
            return sub

    async def touch_checked(self, platega_subscription_id: str, next_charge_at: datetime | None = None):
        async with self._session_maker() as session:
            values = {'last_checked_at': datetime.now()}
            if next_charge_at is not None:
                values['next_charge_at'] = next_charge_at
            await session.execute(update(RecurringSubscription).where(
                RecurringSubscription.platega_subscription_id == platega_subscription_id
            ).values(**values))
            await session.commit()

    async def get_stale_pending(self, older_than_minutes: int) -> list[RecurringSubscription]:
        async with self._session_maker() as session:
            cutoff = datetime.now() - timedelta(minutes=older_than_minutes)
            return (await session.scalars(select(RecurringSubscription).where(
                RecurringSubscription.status == 'pending',
                RecurringSubscription.created_at < cutoff
            ).order_by(RecurringSubscription.id).limit(100))).all()

    async def get_active_to_check(self, older_than_hours: int) -> list[RecurringSubscription]:
        async with self._session_maker() as session:
            cutoff = datetime.now() - timedelta(hours=older_than_hours)
            return (await session.scalars(select(RecurringSubscription).where(
                RecurringSubscription.status == 'active',
                (RecurringSubscription.last_checked_at.is_(None))
                | (RecurringSubscription.last_checked_at < cutoff)
            ).order_by(RecurringSubscription.last_checked_at.asc().nullsfirst()).limit(100))).all()

    async def get_active_user_ids(self) -> set[int]:
        """Пользователи с действующим автопродлением — им не нужны напоминания «продлите»."""
        async with self._session_maker() as session:
            return set((await session.scalars(select(RecurringSubscription.user_id).where(
                RecurringSubscription.status == 'active'))).all())

    async def count_by_status(self) -> dict[str, int]:
        async with self._session_maker() as session:
            rows = await session.execute(select(
                RecurringSubscription.status, func.count(RecurringSubscription.id)
            ).group_by(RecurringSubscription.status))
            return {status: count for status, count in rows.all()}
