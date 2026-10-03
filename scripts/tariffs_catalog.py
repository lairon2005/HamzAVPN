"""Экспорт/импорт только каталога тарифов; не переносит аккаунты и платежи."""
import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import dotenv_values

FIELDS = ("name", "price", "duration_days", "is_active", "data_limit_gb",
          "loyalty_price", "is_highlighted", "price_stars")


def validate(rows):
    if not isinstance(rows, list):
        raise ValueError("Ожидается массив тарифов")
    clean = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise ValueError("Неизвестные или недостающие поля тарифа")
        if not isinstance(row["name"], str) or not row["name"].strip():
            raise ValueError("Пустое название тарифа")
        for field in ("price", "loyalty_price"):
            value = row[field]
            if value is None and field == "loyalty_price":
                continue
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                raise ValueError("Некорректная цена тарифа")
        for field in ("duration_days", "data_limit_gb", "price_stars"):
            value = row[field]
            if value is None and field != "duration_days":
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < (1 if field == "duration_days" else 0):
                raise ValueError("Некорректный срок или квота")
        for field in ("is_active", "is_highlighted"):
            if not isinstance(row[field], bool):
                raise ValueError("Некорректный флаг тарифа")
        clean.append(tuple(row[field] for field in FIELDS))
    return clean


async def run(mode, path):
    rows = None
    if mode == "import":
        raw = sys.stdin.read() if path == "-" else Path(path).read_text()
        rows = validate(json.loads(raw))
    env = {**dotenv_values(".env"), **os.environ}
    conn = await asyncpg.connect(host=env["DB_HOST"], port=int(env.get("DB_PORT", 5432)),
                                 user=env["DB_USER"], password=env["DB_PASSWORD"],
                                 database=env["DB_NAME"], timeout=10)
    try:
        if mode == "export":
            async with conn.transaction(readonly=True):
                records = await conn.fetch("SELECT " + ",".join(FIELDS) + " FROM tariffs ORDER BY id")
            output = json.dumps([dict(r) for r in records], ensure_ascii=False, indent=2) + "\n"
            if path == "-":
                sys.stdout.write(output)
            else:
                Path(path).write_text(output)
        else:
            async with conn.transaction():
                await conn.execute("LOCK TABLE tariffs IN SHARE ROW EXCLUSIVE MODE")
                if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM tariffs)"):
                    raise ValueError("Импорт разрешён только в пустой каталог")
                await conn.executemany("INSERT INTO tariffs (" + ",".join(FIELDS) + ") VALUES (" +
                                       ",".join(f"${i}" for i in range(1, len(FIELDS)+1)) + ")", rows)
            print(f"Импортировано тарифов: {len(rows)}")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("export", "import"))
    parser.add_argument("path", nargs="?", default="-")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.mode, args.path))
    except ValueError as exc:
        sys.exit(str(exc))
    except Exception as exc:
        sys.exit(f"Операция не выполнена ({type(exc).__name__}). Проверьте настройки базы локально.")
