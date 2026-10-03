"""
Репозиторий изменяемых из админки настроек (таблица app_settings).

Значения хранятся строками, типизация — на стороне вызывающего кода
(`get_int`). Читаются они на каждом показе тарифов и на каждом экране
устройств, поэтому поверх лежит кэш с коротким TTL.

Почему TTL, а не сброс кэша при записи: процессов два (vpn_bot и vpn_site),
у каждого свой кэш в памяти, и админ правит цену только в боте — без TTL
сайт продолжал бы считать по старой цене до перезапуска контейнера.
"""
import time

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from db import AppSetting

_CACHE_TTL_SECONDS = 60


class SettingsRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker
        self._cache: dict[str, str] = {}
        self._cache_expires_at: float = 0.0

    async def _load(self) -> dict[str, str]:
        if time.monotonic() < self._cache_expires_at:
            return self._cache

        async with self._session_maker() as session:
            result = await session.execute(select(AppSetting))
            self._cache = {row.key: row.value for row in result.scalars()}

        self._cache_expires_at = time.monotonic() + _CACHE_TTL_SECONDS
        return self._cache

    async def get(self, key: str, default: str | None = None) -> str | None:
        return (await self._load()).get(key, default)

    async def get_int(self, key: str, default: int) -> int:
        """Значение как int. Мусор в БД не должен ронять оплату — падаем на дефолт."""
        raw = await self.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    async def get_all(self) -> dict[str, str]:
        return dict(await self._load())

    async def set(self, key: str, value) -> None:
        async with self._session_maker() as session:
            stmt = insert(AppSetting).values(key=key, value=str(value))
            stmt = stmt.on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
            )
            await session.execute(stmt)
            await session.commit()

        # Свой кэш обновляем сразу, чужой догонит по TTL.
        self._cache[key] = str(value)

    def invalidate(self) -> None:
        self._cache_expires_at = 0.0
