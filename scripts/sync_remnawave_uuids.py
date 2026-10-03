"""
Синхронизирует remnawave_uuid в нашей БД после официальной миграции Remnawave.
Для каждого пользователя с vpn_username запрашивает его UUID из Remnawave
и сохраняет в поле remnawave_uuid.

Запускать ПОСЛЕ официальной миграции Remnawave из Remnawave.

  python scripts/sync_remnawave_uuids.py [--dry-run]

Обязательные переменные окружения:
  DATABASE_URL          — asyncpg DSN, напр. postgresql://user:pass@host:5432/dbname
  REMNAWAVE_API_URL     — базовый URL Remnawave API, напр. https://panel.example.com
  REMNAWAVE_API_TOKEN   — Bearer API token (Dashboard → API Keys)
Опциональная переменная:
  REMNAWAVE_ACCESS_COOKIE — cookie доступа eGames reverse proxy в формате name=value
  REMNAWAVE_PROXY_URL     — HTTP/SOCKS5-прокси для доступа к панели
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncpg
from remnawave.client import RemnawaveClient


async def main(dry_run: bool = False):
    dsn = os.environ["DATABASE_URL"]
    rw_url = os.environ["REMNAWAVE_API_URL"]
    rw_token = os.environ["REMNAWAVE_API_TOKEN"]
    rw_access_cookie = os.getenv("REMNAWAVE_ACCESS_COOKIE", "")
    rw_proxy_url = os.getenv("REMNAWAVE_PROXY_URL")

    remnawave = RemnawaveClient(
        base_url=rw_url,
        token=rw_token,
        access_cookie=rw_access_cookie,
        proxy_url=rw_proxy_url,
    )
    conn = await asyncpg.connect(dsn)

    rows = await conn.fetch(
        "SELECT user_id, vpn_username FROM users "
        "WHERE vpn_username IS NOT NULL AND remnawave_uuid IS NULL"
    )
    print(f"Пользователей без remnawave_uuid: {len(rows)}")

    ok, not_found, fail = 0, 0, 0

    for row in rows:
        username = row["vpn_username"]
        try:
            rw_user = await remnawave.get_user_by_username(username)
            if not rw_user:
                print(f"  NOT FOUND in Remnawave: {username}")
                not_found += 1
                continue

            rw_uuid = rw_user["uuid"]
            print(f"  {'DRY-RUN: ' if dry_run else ''}OK: {username} → {rw_uuid}")
            ok += 1

            if not dry_run:
                await conn.execute(
                    "UPDATE users SET remnawave_uuid = $1 WHERE user_id = $2",
                    rw_uuid, row["user_id"]
                )
        except Exception as e:
            print(f"  FAIL: {username}: {e}")
            fail += 1

    await conn.close()
    print(f"\nИтого: OK={ok}, not_found={not_found}, fail={fail}")
    if not_found > 0:
        print("⚠️  Пользователи NOT FOUND не перенесены официальной миграцией Remnawave — проверить вручную.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    asyncio.run(main(dry_run=dry_run))
