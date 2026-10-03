# webapp/routers/dashboard.py
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from db import User
from webapp.dependencies import get_current_user
from loader import logger, remnawave_client
from database import tariff_repo, stats_repo, payment_repo
from tgbot.services import (
    subscription_service,
    payment_service,
    device_service,
    device_slot_service,
    key_service,
)
from tgbot.services.key_service import CODE_OK, REVOKE_NOTICES
from tgbot.services.pricing import effective_price

router = APIRouter(prefix="/profile")
templates = Jinja2Templates(directory="webapp/templates")

# Фильтр даты
def timestamp_to_date(value):
    if value:
        try:
            return datetime.fromtimestamp(float(value)).strftime('%Y-%m-%d %H:%M')
        except Exception:
            return "Неизвестно"
    return "Неограниченно"

templates.env.filters['timestamp_to_date'] = timestamp_to_date

# --- Роуты ---

@router.get("/", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    user: User = Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login")

    # Получаем тарифы для модального окна
    tariffs_list = await tariff_repo.get_active()

    vpn_user_data = None
    subscription_link = None
    error_message = None

    if user.vpn_username:
        try:
            rw_user = await remnawave_client.get_user_by_username(user.vpn_username)
            if rw_user:
                # Ссылка подписки — нативный subscriptionUrl из Remnawave, используем как есть:
                # по нему открывается sub-страница панели с импортом в приложения.
                subscription_link = rw_user.get("subscriptionUrl") or None

                # Маппинг полей Remnawave -> формат, ожидаемый шаблоном dashboard.html
                # (см. tgbot/services/profile_service.py::_adapt_remnawave_user — тот же паттерн)
                expire_ts = None
                expire_str = rw_user.get("expireAt")
                if expire_str:
                    try:
                        expire_ts = datetime.fromisoformat(expire_str.replace("Z", "+00:00")).timestamp()
                    except (ValueError, AttributeError):
                        expire_ts = None

                # Статусы Remnawave: ACTIVE / DISABLED / LIMITED / EXPIRED — активен только ACTIVE
                is_active = str(rw_user.get("status", "")).upper() == "ACTIVE"

                # usedTrafficBytes вложен в userTraffic (fallback на верхний уровень)
                used_traffic = (rw_user.get("userTraffic") or {}).get(
                    "usedTrafficBytes", rw_user.get("usedTrafficBytes", 0)
                )

                vpn_user_data = {
                    "status": "active" if is_active else "disabled",
                    "expire": expire_ts,
                    "used_traffic": used_traffic,
                    "data_limit": rw_user.get("trafficLimitBytes", 0),
                }
            else:
                error_message = "Аккаунт не найден на сервере VPN."
        except Exception as e:
            logger.error(f"Ошибка получения данных из Remnawave: {e}")
            error_message = "Не удалось загрузить статус VPN."

    # Привязанные устройства — показываем только при живой подписке, чтобы
    # не ходить в панель ради пустого списка.
    devices = None
    if user.vpn_username and vpn_user_data:
        devices = await device_service.list_devices(user.user_id)

    # Докупка доп. устройств: quote решает, можно ли (активная оплаченная
    # подписка, не выбран потолок) и сколько это стоит по остатку срока.
    slot_quote = await device_slot_service.quote(user.user_id, slots=1)
    device_settings = await device_slot_service.settings()

    # Referral info
    referral_count = await stats_repo.count_user_referrals(user.user_id)
    referral_bonus = user.referral_bonus_days or 0

    # Payment history
    payments_list = await payment_service.get_user_payments(user.user_id, limit=10)

    # Итог перевыпуска ключа приезжает кодом в query-строке (см. revoke_key):
    # текст берём из фиксированного словаря, само значение параметра в разметку
    # не попадает.
    key_code = request.query_params.get("key")
    key_notice = REVOKE_NOTICES.get(key_code)

    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "request": request,
        "user": user,
        "key_notice": key_notice,
        "key_notice_ok": key_code == CODE_OK,
        "vpn_data": vpn_user_data,
        "sub_link": subscription_link,
        "error": error_message,
        "tariffs": tariffs_list,
        "tariff_prices": {tariff.id: effective_price(tariff, bool(user.subscription_end_date and user.subscription_end_date > datetime.now())) for tariff in tariffs_list},
        "referral_count": referral_count,
        "referral_bonus": referral_bonus,
        "payments": payments_list,
        "devices": devices,
        "slot_quote": slot_quote,
        "device_settings": device_settings,
        "title": "Личный кабинет"
    })


@router.post("/activate_trial")
async def activate_trial(
    request: Request,
    user: User = Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login")

    if user.has_received_trial:
         return templates.TemplateResponse(request=request, name="dashboard.html", context={
            "request": request,
            "user": user,
            "error": "Вы уже использовали пробный период!",
            "title": "Личный кабинет",
            "tariffs": await tariff_repo.get_active()
        })

    try:
        logger.info(f"Создание триала для {user.email} -> user_{abs(user.user_id)}")
        await subscription_service.activate_trial(user.user_id)
        logger.info(f"Триал успешно выдан: {user.email}")

    except Exception as e:
        logger.error(f"Ошибка при выдаче триала: {e}")
        return templates.TemplateResponse(request=request, name="dashboard.html", context={
            "request": request,
            "user": user,
            "error": f"Ошибка создания VPN: {str(e)}",
            "title": "Личный кабинет",
            "tariffs": await tariff_repo.get_active()
        })

    return RedirectResponse(url="/profile/", status_code=303)


@router.post("/devices/delete")
async def delete_device(
    request: Request,
    user: User = Depends(get_current_user),
):
    """Отвязывает устройство. Принадлежность проверяет device_service."""
    if not user:
        return RedirectResponse(url="/login")

    form = await request.form()
    key = (form.get("key") or "").strip()
    if key:
        await device_service.delete_device(user.user_id, key)

    return RedirectResponse(url="/profile/", status_code=303)


@router.post("/key/revoke")
async def revoke_key(
    request: Request,
    user: User = Depends(get_current_user),
):
    """
    Перевыпускает ключ подписки: старая ссылка умирает, устройства отвязываются.

    Отвечаем редиректом, а не рендером: после POST-рендера обновление страницы
    по F5 перевыпустило бы ключ ещё раз — и человек второй раз остался бы без
    доступа на всех устройствах.
    """
    if not user:
        return RedirectResponse(url="/login")

    result = await key_service.revoke(user.user_id)
    code = CODE_OK if result.ok else (result.code or "generic")
    return RedirectResponse(url=f"/profile/?key={code}", status_code=303)
