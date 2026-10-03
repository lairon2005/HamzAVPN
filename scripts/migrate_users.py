"""
Скрипт для массового обновления пользователей Remnawave:
 - Устанавливает лимит трафика 1000 ГБ с помесячным сбросом (trafficLimitStrategy="MONTH")

Привязку локальных пользователей к UUID панели выполняет
scripts/sync_remnawave_uuids.py. Сквады назначайте средствами Remnawave.

Функции:
 - Пагинация: RemnawaveClient.get_all_users() загружает все страницы
   через size/start; --batch оставлен только для обратной совместимости CLI
   и сейчас ни на что не влияет.
 - Сохранение прогресса: при прерывании продолжает с места остановки
 - Возобновление: повторный запуск автоматически пропускает уже обработанных

Запуск внутри контейнера:
    docker compose exec vpn_bot python3 scripts/migrate_users.py

Параметры:
    --delay   задержка между запросами обновления (сек, default=0.5)
    --batch   не используется в Remnawave-версии (см. TODO выше)
    --reset   сбросить прогресс и начать заново
"""

import asyncio
import sys
import argparse
import json
import os

sys.path.insert(0, ".")

from loader import remnawave_client

DATA_LIMIT_BYTES = 1_000 * 1024 ** 3  # 1000 ГБ в байтах

PROGRESS_FILE = "/tmp/migrate_users_progress.json"


def load_progress() -> set:
    """Загружает список уже обработанных пользователей."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE) as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def save_progress(done: set):
    """Сохраняет прогресс на диск."""
    with open(PROGRESS_FILE, "w") as f:
        json.dump(list(done), f)


async def migrate(delay: float, batch_size: int, reset: bool):
    if reset and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
        print("Прогресс сброшен.")

    done = load_progress()
    if done:
        print(f"Найден прогресс: уже обработано {len(done)} пользователей. Продолжаем...")

    print(f"Запуск миграции (задержка={delay}с)...")

    # Клиент сам загрузит все страницы Remnawave через size/start.
    print("Получение списка пользователей...")
    users = await remnawave_client.get_all_users()
    total = len(users)
    print(f"Всего пользователей: {total}")

    updated = skipped = failed = already_done = 0

    for i, user in enumerate(users, 1):
        username = user.get("username", "")
        user_uuid = user.get("uuid", "")
        if not username or not user_uuid:
            continue

        # Пропускаем уже обработанных (resume после разрыва)
        if username in done:
            already_done += 1
            continue

        current_limit = user.get("trafficLimitBytes") or 0
        current_strategy = user.get("trafficLimitStrategy")
        needs_limit = (
            current_limit != DATA_LIMIT_BYTES
            or current_strategy != "MONTH"
        )

        if not needs_limit:
            print(f"[{i}/{total}] {username} — пропущен (всё актуально)", flush=True)
            skipped += 1
            done.add(username)
            continue

        try:
            await remnawave_client.update_user(
                user_uuid, traffic_limit_bytes=DATA_LIMIT_BYTES, traffic_limit_strategy="MONTH"
            )
            print(f"[{i}/{total}] {username} — обновлён (лимит трафика)", flush=True)
            updated += 1
            done.add(username)
        except Exception as e:
            print(f"[{i}/{total}] {username} — ОШИБКА: {e}", flush=True)
            failed += 1

        # Сохраняем прогресс каждые 10 пользователей
        if len(done) % 10 == 0:
            save_progress(done)

        await asyncio.sleep(delay)

    save_progress(done)
    print(f"\nГотово: обновлено={updated}, пропущено={skipped}, уже было={already_done}, ошибок={failed}")

    # Если всё успешно — удаляем файл прогресса
    if failed == 0 and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
        print("Файл прогресса удалён.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=0.5, help="Задержка между запросами в секундах")
    parser.add_argument("--batch", type=int, default=100, help="Не используется в Remnawave-версии (оставлен для совместимости CLI)")
    parser.add_argument("--reset", action="store_true", help="Сбросить прогресс и начать заново")
    args = parser.parse_args()

    asyncio.run(migrate(delay=args.delay, batch_size=args.batch, reset=args.reset))
