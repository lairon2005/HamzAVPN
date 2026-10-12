# webapp/routers/payment.py
import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from tgbot.services import (
    payment_service, promo_service, subscription_service, device_slot_service,
    recurring_service,
)
from tgbot.services.recurring_service import RecurringError
from platega.client import PlategaError
from tgbot.services.pricing import effective_price
from tgbot.services.device_pricing import build_checkout
from tgbot.services.promo_code_service import PromoClaimError
from db import User
from tgbot.services.payment_service import PendingPaymentError
from database import tariff_repo
from webapp.dependencies import get_current_user
from config import load_config

router = APIRouter(prefix="/payment")
config = load_config()
logger = logging.getLogger(__name__)


class PaymentRequest(BaseModel):
    tariff_name: str
    price: float
    promo_code: str | None = None
    discount_percent: int = 0
    # Источник запроса (README.md фаза 3.1): "web" — сайт (дефолт,
    # обратная совместимость), "tma" — Telegram Mini App. Literal даёт FastAPI
    # автоматическую валидацию (422 на любое другое значение) без ручного validator'а.
    source: Literal["web", "tma"] = "web"
    # Сколько доп. устройств оплачивается вместе с тарифом. Скидка на них не
    # распространяется — см. build_checkout.
    extra_devices: int = 0
    # Подключить автопродление (рекуррентная СБП-подписка Platega). Без промокода:
    # сумма подписки фиксирована.
    autorenew: bool = False


async def _prepare_checkout(payload: PaymentRequest, user):
    tariff = await tariff_repo.get_by_name_and_price(payload.tariff_name, payload.price)
    if not tariff or not tariff.is_active:
        raise HTTPException(status_code=404, detail="Тариф не найден")
    validated_promo = None
    discount = 0
    if payload.promo_code:
        validation = await promo_service.validate(payload.promo_code.strip().upper(), user.user_id)
        if not validation.is_valid or validation.promo.discount_percent <= 0:
            raise HTTPException(status_code=400, detail=validation.error_message or 'Промокод не дает скидку')
        validated_promo = validation.promo
        discount = validated_promo.discount_percent
    # Клиентская discount_percent информационная; доверяем только действующему промокоду.
    active = bool(user.subscription_end_date and user.subscription_end_date > datetime.now())
    original_price = effective_price(tariff, active)
    settings = await device_slot_service.settings()
    slots = max(0, min(settings.max_extra, payload.extra_devices))
    checkout = build_checkout(settings, original_price, tariff.duration_days, slots, discount)
    return tariff, checkout, validated_promo, discount, slots


def _quote_response(checkout, discount, autorenew_amount=None):
    return {'final_amount': checkout.total, 'original_amount': checkout.original_total,
            'tariff_amount': checkout.tariff_final, 'slots_amount': checkout.slots_cost,
            'applied_discount': discount, 'currency': 'RUB', 'extra_devices': checkout.slots,
            # Сумма регулярного списания, если для этого тарифа доступно автопродление.
            'autorenew_amount': autorenew_amount}


async def _autorenew_amount(user, tariff, slots, discount):
    if discount or not recurring_service.is_available():
        return None
    try:
        return (await recurring_service.build_offer(user, tariff, slots)).amount
    except RecurringError:
        return None


@router.post('/quote')
async def quote_payment_route(payload: PaymentRequest, user: User = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail='Unauthorized')
    tariff, checkout, _, discount, slots = await _prepare_checkout(payload, user)
    return _quote_response(checkout, discount, await _autorenew_amount(user, tariff, slots, discount))


@router.post("/create")
async def create_payment_route(payload: PaymentRequest, user: User = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail='Unauthorized')
    if await payment_service.has_pending_payment(user.user_id):
        raise HTTPException(status_code=409, detail='У вас уже есть неоплаченный счёт. Отмените его или завершите оплату.')
    tariff, checkout, validated_promo, discount, slots = await _prepare_checkout(payload, user)
    if payload.autorenew:
        return await _create_autorenew(payload, user, tariff, checkout, validated_promo, slots)
    if payload.source == 'tma' and config.tg_bot.tg_bot_username:
        return_url = f"https://t.me/{config.tg_bot.tg_bot_username}/{config.tg_bot.tma_app_name}?startapp=paid"
    else:
        return_url = f"https://{config.webhook.domain}/profile/"
    claimed = False
    try:
        if validated_promo:
            await promo_service.apply(user.user_id, validated_promo)
            claimed = True
        payment_url, identifier = await payment_service.create_checkout(
            user_id=user.user_id, tariff_id=tariff.id,
            original_amount=checkout.original_total, final_amount=checkout.total,
            description=f'Подписка HamzAVPN: {tariff.name}', return_url=return_url,
            source=payload.source, promo_code=validated_promo.code if validated_promo else None,
            discount_percent=discount, extra_devices=slots,
            metadata={'tariffId': str(tariff.id), 'source': payload.source, 'slots': str(slots)},
        )
        return {'payment_url': payment_url, **_quote_response(checkout, discount)}
    except PendingPaymentError:
        if claimed:
            await promo_service.release(user.user_id, validated_promo)
        raise HTTPException(status_code=409, detail='У вас уже есть неоплаченный счет')
    except PromoClaimError:
        raise HTTPException(status_code=400, detail='Промокод уже использован или закончился')
    except Exception:
        if claimed:
            await promo_service.release(user.user_id, validated_promo)
        logger.exception('Ошибка создания счета Platega')
        raise HTTPException(status_code=503, detail='Платежный сервис временно недоступен')


async def _create_autorenew(payload, user, tariff, checkout, validated_promo, slots):
    """Подписка Platega вместо разового счёта: ссылка ведёт на привязку счёта в банке."""
    if validated_promo:
        raise HTTPException(status_code=400, detail='Автопродление нельзя совмещать с промокодом')
    try:
        sub, url = await recurring_service.start(user.user_id, tariff.id, slots, source=payload.source)
    except RecurringError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PendingPaymentError:
        raise HTTPException(status_code=409, detail='У вас уже есть неоплаченный счет')
    except PlategaError:
        logger.exception('Ошибка создания подписки Platega')
        raise HTTPException(status_code=503, detail='Платежный сервис временно недоступен')
    return {'payment_url': url, 'autorenew': True,
            **_quote_response(checkout, 0, sub.amount), 'final_amount': sub.amount}


def _autorenew_view(sub, tariff):
    if sub is None:
        return {'status': None}
    return {
        'status': sub.status, 'amount': sub.amount, 'extra_devices': sub.extra_devices,
        'tariff_name': tariff.name if tariff else None,
        'period_days': tariff.duration_days if tariff else None,
        'next_charge_at': sub.next_charge_at.strftime('%d.%m.%Y') if sub.next_charge_at else None,
        'payment_url': sub.payment_url if sub.status == 'pending' else None,
    }


@router.get('/autorenew')
async def autorenew_status_route(user: User = Depends(get_current_user)):
    """Состояние автопродления для личного кабинета и Mini App."""
    if not user:
        raise HTTPException(status_code=401, detail='Unauthorized')
    sub = await recurring_service.get_view(user.user_id)
    tariff = await tariff_repo.get_by_id(sub.tariff_id) if sub else None
    return _autorenew_view(sub, tariff)


@router.post('/autorenew/cancel')
async def autorenew_cancel_route(user: User = Depends(get_current_user)):
    """Отключает автопродление: деньги перестают списываться сразу, оплаченный срок остаётся."""
    if not user:
        raise HTTPException(status_code=401, detail='Unauthorized')
    try:
        cancelled = await recurring_service.cancel(user.user_id)
    except PlategaError:
        raise HTTPException(status_code=503, detail='Платежный сервис временно недоступен. Повторите через минуту.')
    if cancelled is None:
        raise HTTPException(status_code=404, detail='Автопродление не подключено')
    return {'cancelled': True}


class PromoValidateRequest(BaseModel):
    code: str


@router.post("/validate-promo")
async def validate_promo_route(
    payload: PromoValidateRequest,
    user: User = Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await promo_service.validate(payload.code.upper(), user.user_id)
    if not result.is_valid:
        return {"valid": False, "error": result.error_message}

    promo = result.promo
    response = {"valid": True, "code": promo.code}

    if promo.discount_percent > 0:
        response["type"] = "discount"
        response["discount_percent"] = promo.discount_percent
    elif promo.bonus_days > 0:
        response["type"] = "bonus_days"
        response["bonus_days"] = promo.bonus_days

    return response


@router.post("/apply-bonus-promo")
async def apply_bonus_promo_route(
    payload: PromoValidateRequest,
    user: User = Depends(get_current_user),
):
    """Apply a bonus-days promo code (no payment needed)."""
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await promo_service.validate(payload.code.upper(), user.user_id)
    if not result.is_valid:
        raise HTTPException(status_code=400, detail=result.error_message)

    promo = result.promo
    if promo.bonus_days <= 0:
        raise HTTPException(status_code=400, detail="Этот промокод даёт скидку, а не бонусные дни")

    # apply_bonus_days сама атомарно захватывает промокод (try_claim) и только
    # потом продлевает подписку; при сбое extend() (например, таймаут
    # Remnawave) захват откатывается (release_claim), так что промокод не
    # "сгорает" — см. tgbot/services/promo_code_service.py.
    try:
        await promo_service.apply_bonus_days(user.user_id, promo, subscription_service)
    except PromoClaimError:
        # Гонка (двойной клик/повторный запрос) или промокод закончился между
        # validate() и try_claim() — не сбой Remnawave, а ожидаемая ситуация,
        # поэтому 400 с понятным сообщением, а не 500.
        raise HTTPException(status_code=400, detail="Этот промокод уже использован или закончился")
    except Exception as e:
        logger.error(f"Error applying bonus promo code for user {user.user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Не удалось начислить бонусные дни. Попробуйте позже.")

    return {"success": True, "bonus_days": promo.bonus_days}


class DeviceSlotsRequest(BaseModel):
    """Докупка доп. устройств в середине оплаченного периода."""
    slots: int = 1
    source: Literal["web", "tma"] = "web"


@router.post("/device-slots")
async def create_device_slots_payment(
    payload: DeviceSlotsRequest,
    user: User = Depends(get_current_user),
):
    """
    Создаёт счёт на докупку слотов устройств.

    Цену считает сервер (остаток дней подписки × цена слота) — клиент присылает
    только количество. Платёж создаётся с kind='devices': вебхук по нему поднимет
    лимит устройств и НЕ тронет срок подписки.
    """
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if await payment_service.has_pending_payment(user.user_id):
        raise HTTPException(
            status_code=409,
            detail="У вас уже есть неоплаченный счёт. Завершите или дождитесь его отмены."
        )

    quote = await device_slot_service.quote(user.user_id, payload.slots)
    if not quote.ok:
        raise HTTPException(status_code=400, detail=quote.error)

    if payload.source == "tma" and config.tg_bot.tg_bot_username:
        return_url = f"https://t.me/{config.tg_bot.tg_bot_username}/{config.tg_bot.tma_app_name}?startapp=devices"
    else:
        return_url = f"https://{config.webhook.domain}/profile/"

    description = f"Дополнительные устройства ({quote.slots} шт., {quote.remaining_days} дн.)"

    try:
        payment_url, provider_payment_id = await payment_service.create_checkout(
            user_id=user.user_id, tariff_id=None, original_amount=quote.price, final_amount=quote.price,
            description=description, return_url=return_url, source=payload.source,
            kind='devices', extra_devices=quote.slots,
            metadata={"kind": "devices", "slots": str(quote.slots), "source": payload.source},
        )

        logger.info(
            f"Device slots payment created: user={user.user_id}, slots={quote.slots}, "
            f"amount={quote.price}, days_left={quote.remaining_days}, source={payload.source}"
        )

        return {"payment_url": payment_url, "final_amount": quote.price, "currency": "RUB"}

    except PendingPaymentError:
        raise HTTPException(status_code=409, detail='У вас уже есть неоплаченный счет')
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating device slots payment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка платежного шлюза")


@router.post("/cancel-pending")
async def cancel_pending_payment_route(user: User = Depends(get_current_user)):
    """Локальная отмена счета; подтвержденная оплата старого счета будет зачислена."""
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    cancelled = await payment_service.cancel_pending_payment(user.user_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Неоплаченный счёт не найден.")

    return {"cancelled": True}
