#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Обновляет только сервисы данного Compose-проекта.
docker compose build vpn_bot
docker compose up -d --wait postgres
docker compose run --rm --no-deps vpn_bot python3 fix_db.py
docker compose up -d --no-deps --force-recreate vpn_bot vpn_site
docker compose run --rm --no-deps -T nginx nginx -t
docker compose up -d --no-deps --force-recreate nginx
