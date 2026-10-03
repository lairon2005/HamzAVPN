import datetime

from sqlalchemy import select, delete, update, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db import PromoCode, UsedPromoCode


class PromoCodeRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def create(self, code: str, bonus_days=0, discount_percent=0, max_uses=1, expire_date=None) -> PromoCode:
        async with self._session_maker() as session:
            new_promo = PromoCode(
                code=code.upper(), bonus_days=bonus_days, discount_percent=discount_percent,
                max_uses=max_uses, uses_left=max_uses, expire_date=expire_date
            )
            session.add(new_promo)
            await session.commit()
            return new_promo

    async def get_all(self) -> list[PromoCode]:
        async with self._session_maker() as session:
            result = await session.execute(select(PromoCode))
            return result.scalars().all()

    async def get_by_code(self, code: str) -> PromoCode | None:
        async with self._session_maker() as session:
            stmt = select(PromoCode).where(func.lower(PromoCode.code) == code.lower())
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def has_user_used(self, user_id: int, promo_id: int) -> bool:
        async with self._session_maker() as session:
            stmt = select(UsedPromoCode).where(
                UsedPromoCode.user_id == user_id,
                UsedPromoCode.promo_code_id == promo_id
            )
            result = await session.execute(select(stmt.exists()))
            return result.scalar()

    async def try_claim(self, user_id: int, promo: PromoCode) -> bool:
        """
        Атомарно захватывает промокод для пользователя: декремент uses_left и
        вставка строки использования — одной транзакцией, с guard'ами прямо в
        SQL. Закрывает TOCTOU-гонку между PromoCodeService.validate() (читает
        has_user_used/uses_left) и фактической записью использования: два
        конкурентных апдейта от Telegram (throttling L1=0.5s окно не спасает,
        aiogram обрабатывает их отдельными тасками) могут оба пройти validate()
        до того, как первый закоммитит — см. security review 2026-07-26.

        Возвращает False, если:
          - промокод уже исчерпан к моменту UPDATE — WHERE uses_left > 0 не даёт
            декременту уйти в минус (uses_left изначально мог быть в памяти
            свежим по validate(), но за сетевую задержку кто-то другой мог его
            исчерпать);
          - пользователь уже погасил именно этот промокод — уникальный индекс
            uq_used_promo_user_code (db.py::UsedPromoCode) ловит гонку через
            ON CONFLICT DO NOTHING.

        В обоих случаях транзакция откатывается целиком: если конфликт
        обнаружился на INSERT, уже выполненный UPDATE (декремент) в этой же,
        ещё не закоммиченной сессии откатывается вместе с ним — uses_left не
        "утекает" при проигранной гонке.
        """
        async with self._session_maker() as session:
            update_stmt = (
                update(PromoCode)
                .where(PromoCode.id == promo.id, PromoCode.uses_left > 0)
                .values(uses_left=PromoCode.uses_left - 1)
                .returning(PromoCode.id)
            )
            update_result = await session.execute(update_stmt)
            if update_result.first() is None:
                # Промокод уже исчерпан — ничего не меняли, но на всякий случай
                # закрываем транзакцию явно, а не полагаемся на __aexit__.
                await session.rollback()
                return False

            insert_stmt = (
                pg_insert(UsedPromoCode)
                .values(user_id=user_id, promo_code_id=promo.id)
                .on_conflict_do_nothing(constraint="uq_used_promo_user_code")
                .returning(UsedPromoCode.id)
            )
            insert_result = await session.execute(insert_stmt)
            if insert_result.first() is None:
                # Пользователь уже погасил этот промокод в параллельном запросе,
                # который выиграл гонку. Откатываем ВЕСЬ блок, включая декремент
                # выше — иначе uses_left спишется без соответствующей записи об
                # использовании.
                await session.rollback()
                return False

            await session.commit()
            return True

    async def release_claim(self, user_id: int, promo: PromoCode) -> None:
        """
        Компенсирующий откат try_claim(): вызывается, когда сам захват прошёл
        успешно, но последующее начисление (subscription_service.extend, поход
        в Remnawave) упало — например, ConnectTimeout панели (инцидент
        2026-07-23, LOVEHamzAVPN). Возвращает попытку пользователю: удаляет
        отметку использования и возвращает uses_left, одной транзакцией.
        """
        async with self._session_maker() as session:
            await session.execute(
                delete(UsedPromoCode).where(
                    UsedPromoCode.user_id == user_id,
                    UsedPromoCode.promo_code_id == promo.id,
                )
            )
            await session.execute(
                update(PromoCode)
                .where(PromoCode.id == promo.id)
                .values(uses_left=PromoCode.uses_left + 1)
            )
            await session.commit()

    async def use(self, user_id: int, promo: PromoCode) -> bool:
        """
        Тонкая обёртка над try_claim() для обратной совместимости вызывающих,
        которым не нужен явный сигнал о проигранной гонке (сам
        PromoCodeService теперь всегда идёт через try_claim/release_claim
        напрямую — см. apply()/apply_bonus_days()).
        """
        return await self.try_claim(user_id, promo)

    async def update_expire(self, promo_id: int, expire_date: datetime.datetime) -> None:
        """
        Продлевает срок действия промокода (используется lifecycle-рассылками, чтобы
        компенсировать единый общий expire_date на все касания серии — см.
        tgbot/services/scheduler.py::_ensure_promo_ttl).
        """
        async with self._session_maker() as session:
            stmt = update(PromoCode).where(PromoCode.id == promo_id).values(expire_date=expire_date)
            await session.execute(stmt)
            await session.commit()

    async def delete_by_id(self, promo_id: int) -> bool:
        async with self._session_maker() as session:
            promo = await session.get(PromoCode, promo_id)
            if promo:
                stmt = delete(UsedPromoCode).where(UsedPromoCode.promo_code_id == promo_id)
                await session.execute(stmt)
                await session.delete(promo)
                await session.commit()
                return True
            return False
