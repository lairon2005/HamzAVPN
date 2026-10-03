"""
Перевыпуск ключа подписки.

Нужен ровно в одной ситуации: ссылка утекла (переслали другу, вытащили из
скриншота, слили в общий чат). Отобрать доступ у того, кто её скопировал,
иначе нельзя — ссылка и есть единственный секрет.

Перевыпуск в панели меняет shortUuid и секреты протоколов, но оставляет
привязанные HWID-устройства. Записи утёкших устройств подключиться уже не
смогут, а слоты hwidDeviceLimit продолжат занимать — поэтому сервис чистит
их сам, иначе после перевыпуска человек упёрся бы в лимит на пустом месте.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil

from database.repositories.user import UserRepository
from loader import logger
from remnawave.client import RemnawaveClient, RemnawaveTransportError

PANEL_UNAVAILABLE = (
    "VPN-панель временно недоступна. "
    "Пожалуйста, повторите попытку через несколько секунд."
)
NO_SUBSCRIPTION = "У вас ещё нет подписки — перевыпускать нечего."
GENERIC_ERROR = "Не удалось перевыпустить ключ. Обратитесь в поддержку."

# Пауза между перевыпусками. Защищает не панель, а самого пользователя:
# каждый перевыпуск заставляет заново добавить подписку на ВСЕХ устройствах,
# и случайный второй тап означал бы повторную настройку.
REVOKE_COOLDOWN = timedelta(minutes=10)


def _parse_dt(value: str | None) -> datetime | None:
    """ISO-строка панели → aware datetime в UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    # Панель отдаёт время с таймзоной, но подстраховываемся: naive datetime
    # в сравнении с aware уронил бы перевыпуск целиком.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _cooldown_message(left: timedelta) -> str:
    minutes = max(1, ceil(left.total_seconds() / 60))
    return (
        f"Ключ уже перевыпускался только что. "
        f"Следующий перевыпуск будет доступен через {minutes} мин."
    )


# Машиночитаемая причина отказа. Нужна вебу: там ответ уезжает редиректом
# (POST/Redirect/GET, иначе F5 перевыпускал бы ключ повторно), а через
# query-строку готовый текст ошибки не протащить — только короткий код.
CODE_COOLDOWN = "cooldown"
CODE_NO_SUBSCRIPTION = "no_subscription"
CODE_PANEL = "panel"
CODE_GENERIC = "generic"


# Успех в той же системе кодов, что и отказы — вебу удобнее один параметр.
CODE_OK = "revoked"

# Готовые тексты для веба. Страница берёт сообщение ТОЛЬКО из этого словаря по
# коду из query-строки: подставлять в разметку произвольное значение параметра
# нельзя, а неизвестный код просто не найдётся и уведомления не будет.
REVOKE_NOTICES = {
    CODE_OK: (
        "Ключ перевыпущен — старая ссылка больше не работает. "
        "Добавьте новую подписку на каждом своём устройстве."
    ),
    CODE_COOLDOWN: (
        "Ключ уже перевыпускался только что. "
        "Повторить можно через несколько минут."
    ),
    CODE_NO_SUBSCRIPTION: NO_SUBSCRIPTION,
    CODE_PANEL: PANEL_UNAVAILABLE,
    CODE_GENERIC: GENERIC_ERROR,
}


@dataclass
class RevokeResult:
    ok: bool
    subscription_url: str = ""
    devices_released: int = 0
    error: str | None = None
    code: str | None = None


class KeyService:
    def __init__(self, user_repo: UserRepository, remnawave: RemnawaveClient):
        self._user_repo = user_repo
        self._remnawave = remnawave

    async def revoke(self, user_id: int) -> RevokeResult:
        """
        Выдаёт пользователю новую ссылку подписки и отвязывает все устройства.

        Порядок действий важен: сначала перевыпуск, потом чистка устройств.
        В обратном порядке остался бы промежуток, в котором утёкший клиент
        ходит по ещё живой ссылке и успевает зарегистрироваться заново —
        то есть слот занял бы ровно тот, у кого доступ и отбираем.
        """
        try:
            user = await self._user_repo.get(user_id)
            if not user or not user.vpn_username:
                return RevokeResult(ok=False, error=NO_SUBSCRIPTION, code=CODE_NO_SUBSCRIPTION)

            rw_user = await self._remnawave.get_user_by_username(user.vpn_username)
            if not rw_user:
                return RevokeResult(ok=False, error=NO_SUBSCRIPTION, code=CODE_NO_SUBSCRIPTION)

            uuid = rw_user.get("uuid") or user.remnawave_uuid
            if not uuid:
                return RevokeResult(ok=False, error=GENERIC_ERROR, code=CODE_GENERIC)

            left = self._cooldown_left(rw_user.get("subRevokedAt"))
            if left:
                return RevokeResult(ok=False, error=_cooldown_message(left), code=CODE_COOLDOWN)

            # Считаем устройства до перевыпуска: после чистки их уже не
            # увидеть, а пользователю важно понимать, сколько слотов
            # освободилось и почему его собственные телефоны отвалились.
            devices_released = await self._count_devices(uuid)

            updated = await self._remnawave.revoke_user_subscription(uuid)
        except RemnawaveTransportError as e:
            logger.warning("[key] transport error on revoke for %s: %s", user_id, e.category)
            return RevokeResult(ok=False, error=PANEL_UNAVAILABLE, code=CODE_PANEL)
        except Exception:
            logger.error("[key] failed to revoke subscription for %s", user_id, exc_info=True)
            return RevokeResult(ok=False, error=GENERIC_ERROR, code=CODE_GENERIC)

        # Ссылка уже сменилась — с этого момента ошибки чистки не должны
        # превращаться в "не удалось перевыпустить": это было бы враньё,
        # старый ключ уже мёртв, и повторный тап только сбил бы человека.
        try:
            await self._remnawave.delete_all_user_devices(uuid)
        except Exception:
            logger.error(
                "[key] subscription revoked for %s, but devices cleanup failed",
                user_id, exc_info=True,
            )
            devices_released = 0

        logger.info(
            "[key] user %s revoked subscription, %s device(s) released",
            user_id, devices_released,
        )
        return RevokeResult(
            ok=True,
            subscription_url=(updated or {}).get("subscriptionUrl") or "",
            devices_released=devices_released,
        )

    async def _count_devices(self, uuid: str) -> int:
        """Сколько устройств привязано сейчас. Ошибку глушим — это справка, а не условие."""
        try:
            return len(await self._remnawave.get_user_devices(uuid))
        except Exception:
            logger.warning("[key] can't count devices for %s before revoke", uuid, exc_info=True)
            return 0

    @staticmethod
    def _cooldown_left(sub_revoked_at: str | None) -> timedelta | None:
        """Сколько ещё ждать до следующего перевыпуска (None — можно сейчас)."""
        revoked_at = _parse_dt(sub_revoked_at)
        if not revoked_at:
            return None

        passed = datetime.now(timezone.utc) - revoked_at
        # Отрицательное passed (часы панели убежали вперёд) трактуем как
        # "ждать весь кулдаун" — это безопаснее, чем пустить перевыпуск.
        if passed >= REVOKE_COOLDOWN:
            return None
        return REVOKE_COOLDOWN - passed
