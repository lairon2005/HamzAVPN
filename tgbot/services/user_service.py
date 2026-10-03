from database.repositories.user import UserRepository
from database.repositories.stats import StatsRepository
from remnawave.client import RemnawaveClient
from loader import logger


class UserService:
    def __init__(self, user_repo: UserRepository, stats_repo: StatsRepository):
        self._user_repo = user_repo
        self._stats_repo = stats_repo

    async def register_or_get(self, user_id: int, full_name: str, username: str | None = None):
        return await self._user_repo.get_or_create(user_id, full_name, username)

    async def get_user(self, user_id: int):
        return await self._user_repo.get(user_id)

    async def find_user(self, query: str):
        """Ищет пользователя по ID (положительный или отрицательный), email или username."""
        stripped = query.strip()
        if stripped.lstrip("-").isdigit() and stripped != "-":
            return await self._user_repo.get(int(stripped))
        if "@" in stripped and "." in stripped:
            return await self._user_repo.get_by_email(stripped.lower())
        return await self._user_repo.get_by_username(stripped.replace("@", ""))

    async def delete_user(self, user_id: int, remnawave: RemnawaveClient) -> bool:
        """Удаляет пользователя из Remnawave (по UUID) и БД."""
        user = await self._user_repo.get(user_id)
        if not user:
            return False

        try:
            if user.remnawave_uuid:
                await remnawave.delete_user(user.remnawave_uuid)
            elif user.vpn_username:
                # UUID неизвестен (старые записи / рассинхрон) — ищем по имени
                rw_user = await remnawave.get_user_by_username(user.vpn_username)
                if rw_user:
                    await remnawave.delete_user(rw_user["uuid"])
        except Exception as e:
            logger.error(f"Failed to delete remnawave user for {user_id}: {e}", exc_info=True)
            return False

        return await self._user_repo.delete(user_id)

    async def get_referral_info(self, user_id: int) -> dict:
        """Возвращает данные рефералки для отображения."""
        user = await self._user_repo.get(user_id)
        count = await self._stats_repo.count_user_referrals(user_id)
        return {
            "referral_count": count,
            "bonus_days": user.referral_bonus_days if user else 0
        }
