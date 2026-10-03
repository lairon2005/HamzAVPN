# webapp/routers/auth.py
import random
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_

from db import async_session_maker, User
from webapp.core.security import get_password_hash, verify_password, create_access_token, ACCESS_TOKEN_EXPIRE_MINUTES
from datetime import timedelta

router = APIRouter()
templates = Jinja2Templates(directory="webapp/templates")

# Зависимость для получения сессии БД
async def get_db():
    async with async_session_maker() as session:
        yield session

# --- ГЕНЕРАЦИЯ ID ДЛЯ WEB ---
async def generate_web_user_id(session: AsyncSession) -> int:
    """
    Генерирует отрицательный ID, чтобы не конфликтовать с Telegram ID.
    Проверяет, свободен ли ID.
    """
    while True:
        # Генерируем ID от -1 до -1 000 000 000
        new_id = random.randint(-1000000000, -1)
        result = await session.execute(select(User).where(User.user_id == new_id))
        if not result.scalar_one_or_none():
            return new_id

# --- СТРАНИЦЫ (GET) ---

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request})

@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request=request, name="register.html", context={"request": request})

@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("access_token")
    return response

# --- ЛОГИКА (POST) ---

@router.post("/register")
async def register_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    # 1. Проверяем, есть ли такой email
    existing_user = await db.execute(select(User).where(User.email == email))
    if existing_user.scalar_one_or_none():
        return templates.TemplateResponse(request=request, name="register.html", context={
            "request": request, 
            "error": "Пользователь с таким Email уже существует"
        })

    # 2. Генерируем ID и хеш
    new_user_id = await generate_web_user_id(db)
    hashed_password = get_password_hash(password)

    # 3. Создаем пользователя
    new_user = User(
        user_id=new_user_id,
        email=email,
        password_hash=hashed_password,
        full_name=full_name,
        username=email.split('@')[0], # Временное решение
        has_received_trial=False
    )
    
    db.add(new_user)
    await db.commit()
    
    # 4. Сразу логиним (создаем токен) и редиректим
    access_token = create_access_token(
        data={"sub": str(new_user.user_id), "email": new_user.email},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)
    return response

@router.post("/login")
async def login_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    # 1. Ищем пользователя
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    
    # 2. Проверка пароля
    if not user or not user.password_hash or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(request=request, name="login.html", context={
            "request": request, 
            "error": "Неверный Email или пароль"
        })
    
    # 3. Создаем токен
    access_token = create_access_token(
        data={"sub": str(user.user_id), "email": user.email},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
    response = RedirectResponse(url="/", status_code=302)
    # httponly=True защищает от кражи кук через JS
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)
    return response