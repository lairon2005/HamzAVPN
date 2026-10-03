from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from database.repositories.user import UserRepository
from remnawave.client import RemnawaveClient
from loader import logger


DEFAULT_TRAFFIC_LIMIT_BYTES = 1_000 * 1024 ** 3  # 1000 ГБ
MONTHLY_TRAFFIC_LIMIT_STRATEGY = "MONTH"


@dataclass
class ExtensionResult:
    is_new_user: bool
    username: str


def _days_to_expire_at(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class SubscriptionService:
    def __init__(self, user_repo: UserRepository, remnawave: RemnawaveClient):
        self._user_repo = user_repo
        self._remnawave = remnawave

    async def extend(self, user_id: int, days: int, data_limit_gb: int | None = None) -> ExtensionResult:
        """
        Продлевает подписку в БД и Remnawave. Создает Remnawave-пользователя если нужно.

        data_limit_gb: квота трафика тарифа в ГБ/месяц. None → квота Remnawave не
        трогается (бонусные/реферальные/промо/админские продления не затирают уже
        купленную квоту). 0 → безлимит.
        """
        user = await self._user_repo.get(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found in DB")

        username = self._resolve_username(user)
        data_limit_bytes = None if data_limit_gb is None else data_limit_gb * 1024 ** 3
        is_new, rw_uuid = await self._ensure_remnawave_user(username, user.remnawave_uuid, days, data_limit_bytes)

        if not user.vpn_username:
            await self._user_repo.update_vpn_username(user_id, username)

        # UUID может измениться, если Remnawave-пользователь был удалён вручную и пересоздан
        if user.remnawave_uuid != rw_uuid:
            await self._user_repo.update_remnawave_uuid(user_id, rw_uuid)

        await self._user_repo.extend_subscription(user_id, days)

        logger.info(f"Subscription for user {user_id} extended by {days} days (remnawave: {username}, new={is_new})")
        return ExtensionResult(is_new_user=is_new, username=username)

    async def activate_trial(self, user_id: int, days: int = 7) -> ExtensionResult:
        """Активирует пробный период: extend (без квоты — дефолт Remnawave) + пометить trial_received."""
        result = await self.extend(user_id, days)
        await self._user_repo.set_trial_received(user_id)
        return result

    def _resolve_username(self, user) -> str:
        if user.vpn_username:
            return user.vpn_username.lower()
        if user.user_id > 0:
            return f"user_{user.user_id}"
        return f"web_{abs(user.user_id)}"

    async def _ensure_remnawave_user(
        self, username: str, rw_uuid: str | None, days: int, data_limit_bytes: int | None
    ) -> tuple[bool, str]:
        """
        Создает или продлевает пользователя в Remnawave.
        Возвращает (is_new, remnawave_uuid).
        """
        # Стратегия сброса квоты: платный тариф с лимитом → помесячный сброс (как было
        # используется trafficLimitStrategy="MONTH"); безлимит → без сброса.
        # data_limit_bytes is None (бонус/промо/реферал) → квоту не трогаем вообще.
        if data_limit_bytes is None:
            strategy = None
        elif data_limit_bytes > 0:
            strategy = "MONTH"
        else:
            strategy = "NO_RESET"

        if rw_uuid:
            existing = await self._remnawave.get_user_by_username(username)
            if existing:
                # Продлеваем от текущей даты истечения (не от "сейчас"), чтобы не терять
                # уже оплаченные дни при повторном продлении до истечения срока.
                base = datetime.now(timezone.utc)
                current_expire_str = existing.get("expireAt")
                if current_expire_str:
                    try:
                        current_expire = datetime.fromisoformat(current_expire_str.replace("Z", "+00:00"))
                        base = max(current_expire, base)
                    except (ValueError, AttributeError):
                        pass
                new_expire = (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                await self._remnawave.update_user(
                    rw_uuid, expire_at=new_expire,
                    traffic_limit_bytes=data_limit_bytes, traffic_limit_strategy=strategy,
                )
                # Платный тариф — обнуляем накопленный трафик, даём свежую квоту
                if data_limit_bytes is not None:
                    await self._remnawave.reset_user_traffic(rw_uuid)
                return False, rw_uuid
            # Пользователь был удалён из Remnawave вручную (панель/скрипт) — пересоздаём

        # Для нового пользователя data_limit_bytes=None означает не «безлимит», а
        # прежний дефолт сервиса: 1000 ГБ с ежемесячным сбросом. Значение None
        # используется как «не менять квоту» только при продлении уже существующего
        # пользователя. Явный 0 по-прежнему означает настоящий безлимит.
        create_limit_bytes = (
            DEFAULT_TRAFFIC_LIMIT_BYTES
            if data_limit_bytes is None
            else data_limit_bytes
        )
        create_strategy = (
            MONTHLY_TRAFFIC_LIMIT_STRATEGY
            if create_limit_bytes > 0
            else "NO_RESET"
        )

        created = await self._remnawave.create_user(
            username=username,
            expire_at=_days_to_expire_at(days),
            traffic_limit_bytes=create_limit_bytes,
            traffic_limit_strategy=create_strategy,
        )
        return True, created["uuid"]
