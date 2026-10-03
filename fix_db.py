"""Идемпотентное создание текущей схемы HamzAVPN в новой собственной БД."""
import asyncio
from sqlalchemy import text
from db import Base, async_engine


async def fix_database():
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Добавочные поля текущей версии; никаких переименований чужих таблиц.
        for ddl in (
            'ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_data_limit_gb INTEGER',
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS currency VARCHAR(3) NOT NULL DEFAULT 'RUB'",
            'ALTER TABLE payments ADD COLUMN IF NOT EXISTS payment_url VARCHAR',
            'ALTER TABLE payments ADD COLUMN IF NOT EXISTS duration_days INTEGER NOT NULL DEFAULT 0',
            'ALTER TABLE payments ADD COLUMN IF NOT EXISTS data_limit_gb INTEGER',
            'ALTER TABLE payments ADD COLUMN IF NOT EXISTS sync_pending BOOLEAN NOT NULL DEFAULT FALSE',
            'ALTER TABLE payments ADD COLUMN IF NOT EXISTS referrer_id BIGINT',
            'ALTER TABLE payments ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMP WITHOUT TIME ZONE',
            'ALTER TABLE payments ADD COLUMN IF NOT EXISTS traffic_reset_done BOOLEAN NOT NULL DEFAULT FALSE',
            'ALTER TABLE payments ADD COLUMN IF NOT EXISTS order_id VARCHAR(36) UNIQUE',
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_user_pending ON payments(user_id) WHERE status = 'pending' AND source != 'stars'",
        ):
            await conn.execute(text(ddl))
    print('Схема HamzAVPN готова.')
    await async_engine.dispose()


if __name__ == '__main__':
    asyncio.run(fix_database())
