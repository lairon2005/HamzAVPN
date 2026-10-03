import datetime

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from db import LifecycleMessage, User


class LifecycleRepository:
    """
    Единый механизм трекинга «касаний» lifecycle-серий (напоминания о продлении,
    win-back, дрип активации). Позволяет не слать одно и то же касание дважды и
    держать последовательность шагов внутри серии.
    """

    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def was_sent(self, user_id: int, series: str, step: str) -> bool:
        """Было ли уже отправлено конкретное касание (series+step) этому пользователю."""
        async with self._session_maker() as session:
            stmt = select(LifecycleMessage.id).where(
                LifecycleMessage.user_id == user_id,
                LifecycleMessage.series == series,
                LifecycleMessage.step == step,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None

    async def mark_sent(self, user_id: int, series: str, step: str) -> bool:
        """
        Помечает касание отправленным. Возвращает False, если запись уже существует
        (защита от гонки/повторного вызова) — UNIQUE(user_id, series, step) в БД.
        """
        async with self._session_maker() as session:
            record = LifecycleMessage(user_id=user_id, series=series, step=step)
            session.add(record)
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def get_last_promo_touch_at(self, user_id: int) -> datetime.datetime | None:
        """
        Дата последнего ЛЮБОГО промо-касания (шаги с префиксом 'promo:' — win-back волны,
        дрип активации, D+5 скидка renewal-серии). Используется для анти-спам лимита
        «не более 1 промо-касания в неделю на сегмент».
        """
        async with self._session_maker() as session:
            stmt = select(func.max(LifecycleMessage.sent_at)).where(
                LifecycleMessage.user_id == user_id,
                LifecycleMessage.step.like('promo:%'),
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def count_returned_after_winback(self) -> int:
        """
        Метрика «вернувшиеся win-back»: сколько уникальных пользователей, получивших
        хотя бы одно касание серии 'winback', сейчас снова активны (подписка в будущем).
        Используется дашбордом (метрика №7).
        """
        async with self._session_maker() as session:
            now = datetime.datetime.now()
            stmt = (
                select(func.count(func.distinct(LifecycleMessage.user_id)))
                .select_from(LifecycleMessage)
                .join(User, User.user_id == LifecycleMessage.user_id)
                .where(
                    LifecycleMessage.series == 'winback',
                    User.subscription_end_date.is_not(None),
                    User.subscription_end_date > now,
                )
            )
            result = await session.execute(stmt)
            return result.scalar_one() or 0

    async def get_returned_winback_users(self) -> list[User]:
        """Как count_returned_after_winback, но возвращает самих пользователей (для деталей в дашборде)."""
        async with self._session_maker() as session:
            now = datetime.datetime.now()
            stmt = (
                select(User)
                .join(LifecycleMessage, LifecycleMessage.user_id == User.user_id)
                .where(
                    LifecycleMessage.series == 'winback',
                    User.subscription_end_date.is_not(None),
                    User.subscription_end_date > now,
                )
                .distinct()
            )
            result = await session.execute(stmt)
            return result.scalars().all()
