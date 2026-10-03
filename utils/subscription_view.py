from dataclasses import dataclass
from datetime import datetime
from html import escape

from utils.formatting import format_traffic, get_user_attribute


@dataclass(frozen=True)
class DbSubscriptionView:
    is_active: bool
    status_icon: str
    status_label: str
    expires_label: str


def build_db_subscription_view(
    subscription_end_date: datetime | None,
    *,
    now: datetime | None = None,
) -> DbSubscriptionView:
    """Формирует статус главного меню исключительно из данных локальной БД."""
    current_time = now or datetime.now()

    if subscription_end_date is None:
        return DbSubscriptionView(
            is_active=False,
            status_icon="⚪️",
            status_label="не оформлена",
            expires_label="—",
        )

    date_label = subscription_end_date.strftime("%d.%m.%Y")
    if subscription_end_date <= current_time:
        return DbSubscriptionView(
            is_active=False,
            status_icon="🔴",
            status_label="истекла",
            expires_label=date_label,
        )

    days_left = max(0, (subscription_end_date - current_time).days)
    return DbSubscriptionView(
        is_active=True,
        status_icon="🟢",
        status_label="активна",
        expires_label=f"{date_label} (осталось {days_left} дн.)",
    )


@dataclass(frozen=True)
class ConnectionView:
    text: str
    subscription_url: str


def build_connection_view(vpn_user: dict) -> ConnectionView:
    """Формирует экран подключения из уже загруженного профиля Remnawave."""
    status = str(get_user_attribute(vpn_user, "status", "unknown"))
    status_labels = {
        "active": "активна",
        "disabled": "неактивна",
        "limited": "лимит исчерпан",
        "expired": "истекла",
        "unknown": "неизвестен",
    }
    status_label = escape(status_labels.get(status.lower(), status))

    expire_ts = get_user_attribute(vpn_user, "expire")
    expire_label = (
        datetime.fromtimestamp(expire_ts).strftime("%d.%m.%Y %H:%M")
        if expire_ts
        else "без ограничения"
    )

    used_traffic = get_user_attribute(vpn_user, "used_traffic", 0)
    data_limit = get_user_attribute(vpn_user, "data_limit")
    used_label = format_traffic(used_traffic)
    limit_label = (
        "Безлимит" if data_limit in (None, 0) else format_traffic(data_limit)
    )
    subscription_url = str(
        get_user_attribute(vpn_user, "subscription_url", "") or ""
    )

    text = (
        "🛜 <b>Подключение VPN</b>\n\n"
        f"🔑 Статус: <b>{status_label}</b>\n"
        f"🗓 Активна до: <code>{expire_label}</code>\n\n"
        "📊 <b>Трафик:</b>\n"
        f"Использовано: <code>{used_label}</code>\n"
        f"Лимит: <code>{limit_label}</code>\n\n"
        "🔗 <b>Ссылка подключения:</b>\n"
        f"<code>{escape(subscription_url)}</code>"
    )
    return ConnectionView(text=text, subscription_url=subscription_url)
