from sqlalchemy import select, update, delete

from db import Tariff


class TariffRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def get_active(self) -> list[Tariff]:
        async with self._session_maker() as session:
            stmt = select(Tariff).where(Tariff.is_active == True).order_by(Tariff.price.asc())
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_all(self) -> list[Tariff]:
        async with self._session_maker() as session:
            result = await session.execute(select(Tariff))
            return result.scalars().all()

    async def get_by_id(self, tariff_id: int) -> Tariff | None:
        async with self._session_maker() as session:
            return await session.get(Tariff, tariff_id)

    async def get_by_name_and_price(self, name: str, price: float) -> Tariff | None:
        async with self._session_maker() as session:
            stmt = select(Tariff).where(Tariff.name == name, Tariff.price == price)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def add(
        self,
        name: str,
        price: float,
        duration_days: int,
        data_limit_gb: int | None = None,
        loyalty_price: float | None = None,
        is_highlighted: bool = False,
    ) -> Tariff:
        async with self._session_maker() as session:
            new_tariff = Tariff(
                name=name,
                price=price,
                duration_days=duration_days,
                is_active=True,
                data_limit_gb=data_limit_gb,
                loyalty_price=loyalty_price,
                is_highlighted=is_highlighted,
            )
            session.add(new_tariff)
            await session.commit()
            return new_tariff

    async def update_field(self, tariff_id: int, field: str, value):
        async with self._session_maker() as session:
            stmt = update(Tariff).where(Tariff.id == tariff_id).values({field: value})
            await session.execute(stmt)
            await session.commit()

    async def delete_by_id(self, tariff_id: int):
        async with self._session_maker() as session:
            stmt = delete(Tariff).where(Tariff.id == tariff_id)
            await session.execute(stmt)
            await session.commit()
