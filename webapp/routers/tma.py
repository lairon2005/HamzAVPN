# webapp/routers/tma.py
"""
Telegram Mini App: авторизация через initData (README.md фаза 1) +
SSR-экраны (фаза 2).

Навигация — обычные GET-запросы с cookie (как на сайте), а не SPA: каждый экран
сам проверяет `get_current_user` и рендерит auth-сплэш, если сессии ещё нет
(пользователь мог открыть Mini App по прямому deeplink, минуя /tma/auth).
"""
import logging
from datetime import datetime, timedelta

from aiogram.types import LabeledPrice
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from config import load_config
from database import tariff_repo
from db import User
from loader import bot as tg_bot
from tgbot.services import (
    device_service,
    device_slot_service, traffic_service,
    key_service,
    payment_service,
    profile_service,
    referral_service,
    user_service,
)
from tgbot.services import support_service
from tgbot.services.key_service import CODE_OK, REVOKE_NOTICES
from tgbot.services.referral_service import REFERRAL_TRIAL_DAYS
from tgbot.services.pricing import effective_price
from tgbot.services.traffic_pricing import gb_to_packs
from webapp.core.security import ACCESS_TOKEN_EXPIRE_MINUTES, create_access_token
from webapp.core.support import (
    SupportValidationError,
    check_rate_limit,
    format_new_ticket_message,
    format_support_message,
    validate_support_text,
)
from webapp.core.tma_auth import InvalidInitDataError, validate_init_data
from webapp.dependencies import get_current_user

router = APIRouter(prefix="/tma")
templates = Jinja2Templates(directory="webapp/templates")
logger = logging.getLogger(__name__)
config = load_config()


def _auth_splash(request: Request) -> HTMLResponse:
    """Общий сплэш для любого экрана, если валидной сессии ещё нет (см. tma/_auth_splash.html)."""
    return templates.TemplateResponse(request=request, name="tma/_auth_splash.html", context={"request": request})


async def _load_vpn_context(user_id: int) -> dict:
    """
    Данные VPN-статуса для главной/import экранов — переиспользуем
    tgbot/services/profile_service.py::ProfileService.get_profile (тот же сервис,
    что и /profile-хендлеры бота), вместо дублирования Remnawave-похода из
    webapp/routers/dashboard.py.
    """
    profile = await profile_service.get_profile(user_id)
    vpn_user = profile.vpn_user
    sub_link = (vpn_user or {}).get("subscription_url") or None

    expire_display = None
    if vpn_user and vpn_user.get("expire"):
        try:
            expire_display = datetime.fromtimestamp(vpn_user["expire"]).strftime("%d.%m.%Y %H:%M")
        except (ValueError, OSError, OverflowError):
            expire_display = None

    return {
        "vpn_data": vpn_user,
        "expire_display": expire_display,
        "sub_link": sub_link,
        "vpn_error": profile.error,
    }


# --- Экраны ---

@router.get("/", response_class=HTMLResponse)
async def tma_home(request: Request, user: User | None = Depends(get_current_user)):
    if not user:
        return _auth_splash(request)

    ctx = await _load_vpn_context(user.user_id)
    return templates.TemplateResponse(request=request, name="tma/index.html", context={
        "request": request,
        "user": user,
        "title": "HamzAVPN",
        "tma_root": True,
        "traffic_quote": await traffic_service.quote(user.user_id),
        "traffic_settings": await traffic_service.settings(),
        "active_tab": "home",
        **ctx,
    })


@router.get("/import", response_class=HTMLResponse)
async def tma_import(request: Request, user: User | None = Depends(get_current_user)):
    if not user:
        return _auth_splash(request)

    ctx = await _load_vpn_context(user.user_id)
    # Итог перевыпуска приезжает кодом в query-строке (см. tma_key_revoke):
    # текст берём из фиксированного словаря, значение параметра в разметку
    # не попадает.
    key_code = request.query_params.get("key")
    return templates.TemplateResponse(request=request, name="tma/import.html", context={
        "request": request,
        "user": user,
        "title": "Подключение",
        "active_tab": "import",
        "key_notice": REVOKE_NOTICES.get(key_code),
        "key_notice_ok": key_code == CODE_OK,
        **ctx,
    })


@router.get("/tariffs", response_class=HTMLResponse)
async def tma_tariffs(request: Request, user: User | None = Depends(get_current_user)):
    if not user:
        return _auth_splash(request)

    tariffs_list = await tariff_repo.get_active()
    device_settings = await device_slot_service.settings()
    return templates.TemplateResponse(request=request, name="tma/tariffs.html", context={
        "request": request,
        "user": user,
        "tariffs": tariffs_list,
        "tariff_prices": {tariff.id: effective_price(tariff, bool(user.subscription_end_date and user.subscription_end_date > datetime.now())) for tariff in tariffs_list},
        "device_settings": device_settings,
        "traffic_settings": await traffic_service.settings(),
        "current_traffic_packs": gb_to_packs(await traffic_service.settings(), user.extra_traffic_gb or 0),
        # Степпер стартует с уже оплаченных слотов: иначе продление, в котором
        # человек не трогал счётчик, молча снесло бы его доп. устройства.
        # Оплата Stars при активных слотах отклоняется (см. stars_payment.py),
        # поэтому кнопка ⭐ у таких пользователей просто не показывается.
        "current_extra_devices": user.extra_devices or 0,
        "title": "Тарифы",
        "active_tab": "tariffs",
    })


@router.get("/referral", response_class=HTMLResponse)
async def tma_referral(request: Request, user: User | None = Depends(get_current_user)):
    if not user:
        return _auth_splash(request)

    info = await user_service.get_referral_info(user.user_id)

    bot_username = config.tg_bot.tg_bot_username
    ref_link = None
    if bot_username:
        ref_link = f"https://t.me/{bot_username}/{config.tg_bot.tma_app_name}?startapp={user.user_id}"

    return templates.TemplateResponse(request=request, name="tma/referral.html", context={
        "request": request,
        "user": user,
        "referral_count": info["referral_count"],
        "referral_bonus": info["bonus_days"],
        "ref_link": ref_link,
        "title": "Реферальная программа",
        "active_tab": "referral",
    })


@router.get("/history", response_class=HTMLResponse)
async def tma_history(request: Request, user: User | None = Depends(get_current_user)):
    if not user:
        return _auth_splash(request)

    payments_list = await payment_service.get_user_payments(user.user_id, limit=20)
    return templates.TemplateResponse(request=request, name="tma/history.html", context={
        "request": request,
        "user": user,
        "payments": payments_list,
        "title": "История платежей",
        "active_tab": "history",
    })


@router.get("/devices", response_class=HTMLResponse)
async def tma_devices(request: Request, user: User | None = Depends(get_current_user)):
    if not user:
        return _auth_splash(request)

    devices = await device_service.list_devices(user.user_id)
    # quote решает, показывать ли докупку: нужна активная оплаченная подписка
    # и незанятый потолок слотов.
    slot_quote = await device_slot_service.quote(user.user_id, slots=1)
    device_settings = await device_slot_service.settings()
    return templates.TemplateResponse(request=request, name="tma/devices.html", context={
        "request": request,
        "user": user,
        "devices": devices,
        "slot_quote": slot_quote,
        "device_settings": device_settings,
        "traffic_settings": await traffic_service.settings(),
        "current_traffic_packs": gb_to_packs(await traffic_service.settings(), user.extra_traffic_gb or 0),
        "title": "Мои устройства",
        "active_tab": "devices",
    })


@router.post("/devices/delete")
async def tma_devices_delete(request: Request, user: User | None = Depends(get_current_user)):
    """
    Отвязка устройства обычной формой, а не JSON-ручкой: экран SSR-овый,
    и после удаления нужен именно redirect на обновлённый список.
    Принадлежность устройства проверяет device_service.
    """
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    form = await request.form()
    key = (form.get("key") or "").strip()
    if key:
        await device_service.delete_device(user.user_id, key)

    return RedirectResponse(url="/tma/devices", status_code=303)


@router.post("/key/revoke")
async def tma_key_revoke(request: Request, user: User | None = Depends(get_current_user)):
    """
    Перевыпуск ключа подписки: старая ссылка умирает, устройства отвязываются.

    Как и на сайте — редирект, а не рендер: после POST-рендера обновление
    экрана перевыпустило бы ключ повторно.
    """
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await key_service.revoke(user.user_id)
    code = CODE_OK if result.ok else (result.code or "generic")
    return RedirectResponse(url=f"/tma/import?key={code}", status_code=303)


@router.get("/support", response_class=HTMLResponse)
async def tma_support(request: Request, user: User | None = Depends(get_current_user)):
    if not user:
        return _auth_splash(request)

    return templates.TemplateResponse(request=request, name="tma/support.html", context={
        "request": request,
        "user": user,
        "title": "Поддержка",
        "active_tab": "support",
    })


# --- Поддержка (README.md фаза 4): форма обращения в TMA форвардит
# сообщение в тот же чат/топик поддержки, что и tgbot/handlers/support.py, переиспользуя
# support_service (топики хранятся в User.support_topic_id) — админы отвечают как обычно,
# ответ бот отправляет пользователю в личку (см. admin_reply_to_user_from_topic).
# Валидация и rate-limit вынесены в webapp/core/support.py (stdlib-only, unit-тесты
# см. tests/test_tma_support.py) — по тому же принципу, что и tma_auth.py для initData. ---

# In-memory rate-limit (простейший достаточный вариант для одного процесса vpn_site,
# см. README.md фаза 4, пункт 2) — не переживает рестарт, но это ОК для
# анти-спам защиты от повторных сабмитов формы.
_support_last_sent_at: dict[int, datetime] = {}


class SupportMessageRequest(BaseModel):
    text: str


async def _get_or_create_support_topic(user: User) -> int:
    """Возвращает ID топика поддержки пользователя, создавая его при необходимости.

    Та же логика, что и tgbot/handlers/support.py::start_support_chat_confirmed —
    переиспользуем support_service, чтобы админы видели/отвечали в ОДНОМ и том же
    топике независимо от того, откуда пришло сообщение (бот или Mini App).
    """
    topic_id = await support_service.get_topic_id(user.user_id)
    if topic_id:
        return topic_id

    topic = await tg_bot.create_forum_topic(
        chat_id=config.tg_bot.support_chat_id,
        name=f"Тикет #{user.user_id} | @{user.username or 'NoUsername'}",
    )
    await support_service.save_topic(user.user_id, topic.message_thread_id)
    await tg_bot.send_message(
        chat_id=config.tg_bot.support_chat_id,
        message_thread_id=topic.message_thread_id,
        text=format_new_ticket_message(user.full_name, user.user_id),
    )
    return topic.message_thread_id


@router.post("/support")
async def tma_support_send(payload: SupportMessageRequest, user: User | None = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        text = validate_support_text(payload.text)
        check_rate_limit(_support_last_sent_at, user.user_id)
    except SupportValidationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    try:
        topic_id = await _get_or_create_support_topic(user)
        # Формат сообщения — как заголовок топика в support.py (имя + ID), плюс
        # пометка источника, чтобы админ понимал, что ответить нужно как обычно
        # (админ отвечает в топик — admin_reply_to_user_from_topic сам найдёт юзера).
        # Экранирование обязательно: бот работает с parse_mode=HTML, а text и
        # full_name задаёт пользователь (см. webapp/core/support.py).
        await tg_bot.send_message(
            chat_id=config.tg_bot.support_chat_id,
            message_thread_id=topic_id,
            text=format_support_message(user.full_name, user.user_id, text),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"TMA support: failed to forward message from user {user.user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Не удалось отправить сообщение. Попробуйте позже.")

    return {"ok": True}


# --- Оплата Telegram Stars (README.md: оплата Telegram Stars) ---

class StarsInvoiceRequest(BaseModel):
    tariff_id: int


@router.post("/payment/stars-invoice")
async def create_stars_invoice(
    payload: StarsInvoiceRequest,
    user: User | None = Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    tariff = await tariff_repo.get_by_id(payload.tariff_id)
    if not tariff or not tariff.is_active:
        raise HTTPException(status_code=404, detail="Тариф не найден")
    if not tariff.price_stars:
        raise HTTPException(status_code=400, detail="Оплата Stars недоступна для этого тарифа")

    # payload инвойса: "stars:{tariff_id}:{user_id}:{order_id}" — распознаётся обработчиками
    # tgbot/handlers/user/stars_payment.py (pre_checkout_query + successful_payment).
    # Currency XTR — provider_token НЕ передаётся (см. Bot API docs по Stars-платежам).
    # Используем тот же инстанс aiogram Bot, что и остальной бот/вебхуки (loader.bot),
    # а не создаём новый — единый rate-limit и сессия HTTP-клиента.
    try:
        invoice = await payment_service.prepare_stars_invoice(user.user_id, tariff.id)
        invoice_url = await tg_bot.create_invoice_link(
            title=f"Подписка «{tariff.name}»",
            description=f"HamzAVPN: {tariff.name} на {tariff.duration_days} дней",
            payload=f"stars:{tariff.id}:{user.user_id}:{invoice.order_id}",
            currency="XTR",
            prices=[LabeledPrice(label=tariff.name, amount=tariff.price_stars)],
        )
    except Exception as e:
        logger.error(f"Failed to create Stars invoice: user={user.user_id}, tariff={tariff.id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Не удалось создать счёт")

    return {"invoice_url": invoice_url}


# --- Авторизация ---

class TmaAuthRequest(BaseModel):
    init_data: str


def _full_name_from_tg_user(tg_user: dict) -> str:
    """Собирает full_name так же, как это делает aiogram User.full_name."""
    first = (tg_user.get("first_name") or "").strip()
    last = (tg_user.get("last_name") or "").strip()
    full = f"{first} {last}".strip()
    return full or tg_user.get("username") or str(tg_user.get("id", ""))


@router.post("/auth")
async def tma_auth(payload: TmaAuthRequest):
    try:
        data = validate_init_data(payload.init_data, config.tg_bot.token)
    except InvalidInitDataError as e:
        raise HTTPException(status_code=401, detail=f"Invalid initData: {e}")

    tg_user = data["user"]
    try:
        user_id = int(tg_user["id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="initData.user.id is missing")

    full_name = _full_name_from_tg_user(tg_user)
    username = tg_user.get("username")

    # Find-or-create — та же логика, что и /start в боте (tgbot/handlers/user/start.py,
    # user_service.register_or_get -> UserRepository.get_or_create). Юзер мог открыть
    # Mini App, ни разу не нажав /start, поэтому find-or-create обязателен.
    user, created = await user_service.register_or_get(user_id, full_name, username)

    # Рефералка при первом входе: ?startapp={ref_id} прилетает в initDataUnsafe.start_param
    # БЕЗ префикса "ref" (в отличие от /start?start=ref123 в самом боте — Telegram Mini App
    # деeplink-параметр это просто значение). См. README.md фаза 1.4.
    # Ключевые слова экранов (tariffs/import/support/history) обрабатываются в tma.js
    # на клиенте (routeStartParam) — здесь пытаемся распарсить start_param как int и
    # молча игнорируем нечисловые значения.
    if created and data.get("start_param"):
        try:
            referrer_id = int(data["start_param"])
        except (TypeError, ValueError):
            referrer_id = None

        if referrer_id and referrer_id != user_id and await user_service.get_user(referrer_id):
            await referral_service.activate_new_user_referral(user_id, referrer_id, REFERRAL_TRIAL_DAYS)
        elif referrer_id:
            logger.info(f"TMA: referrer {referrer_id} not found or self-referral, skip for user {user_id}")

    # Тот же формат payload и cookie, что и в webapp/routers/auth.py — все существующие
    # API-роуты (dashboard, payment) продолжают работать без изменений.
    access_token = create_access_token(
        data={"sub": str(user.user_id), "email": user.email},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )

    response = JSONResponse({
        "ok": True,
        "token": access_token,
        "user": {
            "id": user.user_id,
            "full_name": user.full_name,
            "username": user.username,
        },
    })
    # httponly cookie как на сайте + токен в теле ответа — фолбэк для webview
    # Telegram Desktop, где cookie иногда режутся (риск из roadmap, фаза 1.3).
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)
    return response
