"""
Управление HWID-устройствами пользователя.

Панель считает устройства сама (заголовки Happ/клиента → таблица
hwid_user_devices), а лимит задаётся полем hwidDeviceLimit у пользователя.
Как только лимит выставлен, человеку нужен способ освободить слот —
этим и занимается сервис: общий для бота, сайта и Mini App.
"""
from dataclasses import dataclass
from datetime import datetime
from hashlib import blake2s

from database.repositories.user import UserRepository
from loader import logger
from remnawave.client import RemnawaveClient, RemnawaveTransportError

PANEL_UNAVAILABLE = (
    "VPN-панель временно недоступна. "
    "Пожалуйста, повторите попытку через несколько секунд."
)
NO_SUBSCRIPTION = "У вас ещё нет активной подписки."
GENERIC_ERROR = "Не удалось получить список устройств. Обратитесь в поддержку."
DEVICE_NOT_FOUND = "Устройство не найдено — возможно, вы уже его удалили."


def device_key(hwid: str) -> str:
    """
    Короткий стабильный идентификатор устройства.

    Сырой hwid в callback_data класть нельзя: Telegram ограничивает её 64
    байтами, а длина hwid зависит от клиента. Хэш решает и вторую задачу —
    по нему нельзя подделать чужой hwid, потому что удаление всё равно
    ищет устройство только среди своих (см. delete_device).
    """
    return blake2s(hwid.encode(), digest_size=6).hexdigest()


def _parse_app(user_agent: str | None) -> str:
    """"Happ/3.26.3/Android/1783945..." → "Happ 3.26.3"."""
    if not user_agent:
        return "—"
    parts = user_agent.split("/")
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    return parts[0]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


@dataclass
class Device:
    key: str
    hwid: str
    title: str
    platform: str
    app: str
    ip: str
    added_at: datetime | None
    last_seen: datetime | None

    @property
    def added_display(self) -> str:
        return self.added_at.strftime("%d.%m.%Y") if self.added_at else "—"

    @property
    def last_seen_display(self) -> str:
        return self.last_seen.strftime("%d.%m.%Y %H:%M") if self.last_seen else "—"


@dataclass
class DeviceList:
    devices: list[Device]
    limit: int | None = None
    error: str | None = None

    @property
    def used(self) -> int:
        return len(self.devices)

    @property
    def limit_display(self) -> str:
        return str(self.limit) if self.limit else "без ограничения"

    @property
    def is_full(self) -> bool:
        return bool(self.limit) and self.used >= self.limit


def _to_device(raw: dict) -> Device:
    hwid = raw.get("hwid") or ""
    model = (raw.get("deviceModel") or "").strip()
    platform = (raw.get("platform") or "").strip() or "—"
    os_version = (raw.get("osVersion") or "").strip()

    title = model or platform
    if os_version and platform != "—":
        platform = f"{platform} {os_version}"

    return Device(
        key=device_key(hwid),
        hwid=hwid,
        title=title or "Неизвестное устройство",
        platform=platform,
        app=_parse_app(raw.get("userAgent")),
        ip=raw.get("requestIp") or "—",
        added_at=_parse_dt(raw.get("createdAt")),
        last_seen=_parse_dt(raw.get("updatedAt")),
    )


class DeviceService:
    def __init__(self, user_repo: UserRepository, remnawave: RemnawaveClient):
        self._user_repo = user_repo
        self._remnawave = remnawave

    async def _resolve(self, user_id: int) -> tuple[str | None, int | None, str | None]:
        """Возвращает (remnawave_uuid, hwid_device_limit, error)."""
        user = await self._user_repo.get(user_id)
        if not user or not user.vpn_username:
            return None, None, NO_SUBSCRIPTION

        rw_user = await self._remnawave.get_user_by_username(user.vpn_username)
        if not rw_user:
            return None, None, NO_SUBSCRIPTION

        uuid = rw_user.get("uuid") or user.remnawave_uuid
        if not uuid:
            return None, None, GENERIC_ERROR

        return uuid, rw_user.get("hwidDeviceLimit"), None

    async def list_devices(self, user_id: int) -> DeviceList:
        """Устройства пользователя, свежие сверху."""
        try:
            uuid, limit, error = await self._resolve(user_id)
            if error:
                return DeviceList(devices=[], error=error)

            raw_devices = await self._remnawave.get_user_devices(uuid)
        except RemnawaveTransportError as e:
            logger.warning("[devices] transport error for user %s: %s", user_id, e.category)
            return DeviceList(devices=[], error=PANEL_UNAVAILABLE)
        except Exception:
            logger.error("[devices] failed to list devices for %s", user_id, exc_info=True)
            return DeviceList(devices=[], error=GENERIC_ERROR)

        devices = [_to_device(d) for d in raw_devices]
        devices.sort(key=lambda d: d.last_seen or datetime.min.replace(tzinfo=None), reverse=True)
        return DeviceList(devices=devices, limit=limit)

    async def delete_device(self, user_id: int, key: str) -> tuple[bool, str | None]:
        """
        Удаляет устройство пользователя по короткому ключу.

        hwid берётся не из запроса, а из списка устройств этого пользователя —
        так чужое устройство удалить нельзя, даже подобрав ключ.
        """
        try:
            uuid, _, error = await self._resolve(user_id)
            if error:
                return False, error

            raw_devices = await self._remnawave.get_user_devices(uuid)
            target = next((d for d in raw_devices if device_key(d.get("hwid") or "") == key), None)
            if not target:
                return False, DEVICE_NOT_FOUND

            await self._remnawave.delete_user_device(uuid, target["hwid"])
        except RemnawaveTransportError as e:
            logger.warning("[devices] transport error on delete for %s: %s", user_id, e.category)
            return False, PANEL_UNAVAILABLE
        except Exception:
            logger.error("[devices] failed to delete device for %s", user_id, exc_info=True)
            return False, GENERIC_ERROR

        logger.info("[devices] user %s removed device %s", user_id, key)
        return True, None
