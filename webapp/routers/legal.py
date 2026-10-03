"""Документы, цены и контакт поддержки доступны без авторизации."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from database import tariff_repo
from loader import config, logger
from tgbot.services import device_slot_service
from utils.legal import (
    DOCUMENT_DATE, PRIVACY, TERMS, REVIEW_PHRASE,
    platega_review_mode, public_tariff_rows, telegram_support_url,
)

router = APIRouter()
templates = Jinja2Templates(directory="webapp/templates")


def context(request: Request, title: str, **extra):
    return {
        "request": request, "title": title, "user": None,
        "document_date": DOCUMENT_DATE,
        "review_mode": platega_review_mode(), "review_phrase": REVIEW_PHRASE,
        "support_url": telegram_support_url(config.tg_bot.tg_bot_username),
        **extra,
    }


@router.get("/documents", response_class=HTMLResponse)
async def documents(request: Request):
    return templates.TemplateResponse(request=request, name="public/documents.html", context=context(request, "Документы и тарифы"))


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse(request=request, name="public/legal.html", context=context(request, PRIVACY.title, document=PRIVACY))


@router.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return templates.TemplateResponse(request=request, name="public/legal.html", context=context(request, TERMS.title, document=TERMS))


@router.get("/support", response_class=HTMLResponse)
async def support(request: Request):
    return templates.TemplateResponse(request=request, name="public/support.html", context=context(request, "Поддержка"))


@router.get("/tariffs", response_class=HTMLResponse)
async def tariffs(request: Request):
    unavailable = False
    rows = []
    try:
        active_tariffs = await tariff_repo.get_active()
        if active_tariffs:
            settings = await device_slot_service.settings()
            rows = public_tariff_rows(active_tariffs, settings)
    except Exception:
        logger.exception("Не удалось загрузить публичные тарифы")
        unavailable = True
    return templates.TemplateResponse(request=request, name="public/tariffs.html", context=context(
        request, "Тарифы и цены", tariffs=rows, unavailable=unavailable,
        purchase_url=(telegram_support_url(config.tg_bot.tg_bot_username) or "").replace("?start=support", "?start=tariffs") or "/register",
    ))
