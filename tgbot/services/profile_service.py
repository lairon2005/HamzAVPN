from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Any

from db import User
from database.repositories.user import UserRepository
from remnawave.client import RemnawaveClient, RemnawaveTransportError
from loader import logger


@dataclass
class ProfileData:
    db_user: User | None
    vpn_user: Dict[str, Any] | None
    error: str | None = None


def _adapt_remnawave_user(rw_user: dict) -> dict:
    """
    Маппинг полей Remnawave-пользователя → форму, ожидаемую хендлерами.

    subscription_url берётся из нативного поля subscriptionUrl объекта Remnawave —
    это уже полный URL, используется как есть, без префикса домена.
    """
    expire_ts = None
    expire_str = rw_user.get("expireAt")
    if expire_str:
        try:
            expire_ts = int(datetime.fromisoformat(expire_str.replace("Z", "+00:00")).timestamp())
        except (ValueError, AttributeError):
            pass

    # Статусы Remnawave: ACTIVE / DISABLED / LIMITED / EXPIRED — активен только ACTIVE
    is_active = str(rw_user.get("status", "")).upper() == "ACTIVE"

    # usedTrafficBytes лежит внутри объекта userTraffic (fallback на верхний уровень
    # для совместимости со старыми версиями панели, где поле было плоским)
    used_traffic = (rw_user.get("userTraffic") or {}).get(
        "usedTrafficBytes", rw_user.get("usedTrafficBytes", 0)
    )

    return {
        "status": "active" if is_active else "disabled",
        "expire": expire_ts,
        "used_traffic": used_traffic,
        "data_limit": rw_user.get("trafficLimitBytes", 0),
        "subscription_url": rw_user.get("subscriptionUrl") or "",
    }


class ProfileService:
    def __init__(self, user_repo: UserRepository, remnawave: RemnawaveClient):
        self._user_repo = user_repo
        self._remnawave = remnawave

    async def get_profile(self, user_id: int) -> ProfileData:
        """Возвращает данные профиля без побочных эффектов."""
        user = await self._user_repo.get(user_id)

        if not user or not user.vpn_username:
            return ProfileData(
                db_user=user, vpn_user=None,
                error="У вас еще нет активной подписки. Пожалуйста, оплатите тариф, чтобы получить доступ."
            )

        try:
            rw_user = await self._remnawave.get_user_by_username(user.vpn_username)
            if not rw_user:
                return ProfileData(
                    db_user=user, vpn_user=None,
                    error="Не удалось получить данные о вашей подписке. Пожалуйста, обратитесь в поддержку."
                )
            return ProfileData(db_user=user, vpn_user=_adapt_remnawave_user(rw_user))
        except RemnawaveTransportError as e:
            logger.warning(
                f"Temporary Remnawave transport error while loading "
                f"{user.vpn_username}: {e.category}"
            )
            return ProfileData(
                db_user=user, vpn_user=None,
                error=(
                    "VPN-панель временно недоступна. "
                    "Пожалуйста, повторите попытку через несколько секунд."
                )
            )
        except Exception as e:
            logger.error(f"Failed to get user {user.vpn_username} from Remnawave: {e}", exc_info=True)
            return ProfileData(
                db_user=user, vpn_user=None,
                error="Не удалось получить данные о вашей подписке. Пожалуйста, обратитесь в поддержку."
            )
