# webapp/main.py
import asyncio
import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request, Depends
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware 
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware

from db import User
from webapp.routers import auth, dashboard, payment, tma, legal
from webapp.dependencies import get_current_user
from loader import logger, remnawave_client, shutdown_logging
from typing import Optional
from db import Tariff
from database import tariff_repo
from tgbot.services import device_slot_service



@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.remnawave_client = remnawave_client
    try:
        yield  # Здесь приложение работает (принимает запросы)
    finally:
        try:
            await remnawave_client.aclose()
        finally:
            await asyncio.to_thread(shutdown_logging)

app = FastAPI(lifespan=lifespan)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])

# --- Статика (создаем папку, если нет) ---
static_dir = "webapp/static"
if not os.path.exists(static_dir):
    os.makedirs(static_dir)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# --- Шаблоны ---
templates = Jinja2Templates(directory="webapp/templates")

# Фильтр даты (ОБЯЗАТЕЛЕН, иначе дашборд упадет)
def timestamp_to_date(value):
    if value:
        try:
            return datetime.fromtimestamp(float(value)).strftime('%Y-%m-%d %H:%M')
        except Exception:
            return "Err"
    return "Неограниченно"

templates.env.filters['timestamp_to_date'] = timestamp_to_date

# --- Роутеры ---
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(payment.router)
app.include_router(tma.router)
app.include_router(legal.router)

# --- Главная ---
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, user: User = Depends(get_current_user)):
    if user:
        return RedirectResponse(url="/profile/", status_code=302)
    tariffs = await tariff_repo.get_active()
    # Базовый лимит устройств правится из админки — на лендинге он не должен
    # расходиться с тем, что человек получит после оплаты.
    device_settings = await device_slot_service.settings()
    return templates.TemplateResponse(request=request, name="index.html", context={
        "request": request,
        "title": "Главная",
        "user": user,
        "tariffs": tariffs,
        "device_settings": device_settings,
    })
