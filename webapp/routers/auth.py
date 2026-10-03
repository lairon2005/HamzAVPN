# webapp/routers/auth.py
import random
import hashlib
import hmac
import logging
import secrets
from jose import jwt
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_

from db import async_session_maker, User
from webapp.core.mail import send_reset_code, send_verification_email, MailSendError
from webapp.core.security import get_password_hash, verify_password, create_access_token, ACCESS_TOKEN_EXPIRE_MINUTES, SECRET_KEY, ALGORITHM
from webapp.dependencies import get_current_user
from datetime import timedelta, datetime

router = APIRouter()
templates = Jinja2Templates(directory="webapp/templates")


def _new_email_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"


def _registration_code_digest(email: str, nonce: str, code: str) -> str:
    """JWT публичен: хранить в нём можно только проверку кода с секретным ключом."""
    message = "\0".join(("HamzAVPN:email-verification:v2", email, nonce, code))
    return hmac.new(SECRET_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()


def _valid_registration_payload(payload: dict) -> bool:
    """Старые токены, раскрывающие код, больше не подтверждают регистрацию."""
    digest = payload.get("code_digest")
    nonce = payload.get("code_nonce")
    return (
        payload.get("type") == "registration"
        and payload.get("registration_version") == 2
        and "code" not in payload
        and isinstance(payload.get("email"), str)
        and isinstance(digest, str) and len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest)
        and isinstance(nonce, str) and len(nonce) == 32
        and all(char in "0123456789abcdef" for char in nonce)
    )


def _registration_code_matches(payload: dict, code: str) -> bool:
    if not _valid_registration_payload(payload):
        return False
    code = code.strip()
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return False
    digest = _registration_code_digest(payload["email"], payload["code_nonce"], code)
    return hmac.compare_digest(payload["code_digest"], digest)

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
async def login_page(request: Request, user: User = Depends(get_current_user)):
    if user:
        return RedirectResponse(url="/profile/", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request})

@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, user: User = Depends(get_current_user)):
    if user:
        return RedirectResponse(url="/profile/", status_code=302)
    ref = request.query_params.get("ref")
    return templates.TemplateResponse(request=request, name="register.html", context={"request": request, "ref": ref})

@router.get("/logout")
async def logout_user():
    # Перенаправляем на страницу входа
    response = RedirectResponse(url="/login", status_code=302)
    # Удаляем куку с токеном
    response.delete_cookie(key="access_token")
    return response
@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(request=request, name="forgot_password.html", context={"request": request})

@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request):
    return templates.TemplateResponse(request=request, name="reset_password.html", context={"request": request})

@router.post("/register")
async def register_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(...),
    ref: str = Form(None),
    db: AsyncSession = Depends(get_db)
):
    logging.info(f"Attempting to register user with email: {email}")

    # Валидация пароля
    if len(password) < 6:
        return templates.TemplateResponse(request=request, name="register.html", context={
            "request": request,
            "error": "Пароль должен содержать минимум 6 символов",
            "ref": ref
        })

    # 1. Проверяем, есть ли такой email
    existing_user = await db.execute(select(User).where(User.email == email))
    if existing_user.scalar_one_or_none():
        return templates.TemplateResponse(request=request, name="register.html", context={
            "request": request,
            "error": "Пользователь с таким Email уже существует",
            "ref": ref
        })

    # 2. Генерируем код верификации
    code = _new_email_code()
    code_nonce = secrets.token_hex(16)
    hashed_password = get_password_hash(password)

    # 3. Создаём подписанный JWT с pending-данными (пользователь НЕ создаётся в БД)
    registration_token = jwt.encode({
        "type": "registration",
        "registration_version": 2,
        "email": email,
        "full_name": full_name,
        "password_hash": hashed_password,
        "ref": ref,
        "code_nonce": code_nonce,
        "code_digest": _registration_code_digest(email, code_nonce, code),
        "code_expire": (datetime.utcnow() + timedelta(minutes=15)).isoformat(),
        "code_sent_at": datetime.utcnow().isoformat(),
        "attempts": 0,
        "exp": datetime.utcnow() + timedelta(hours=1),
    }, SECRET_KEY, algorithm=ALGORITHM)

    # 4. Отправляем код на email
    try:
        await send_verification_email(email, code)
    except MailSendError as e:
        return templates.TemplateResponse(request=request, name="register.html", context={
            "request": request,
            "error": str(e),
            "ref": ref
        })

    # 5. Рендерим страницу ввода кода
    return templates.TemplateResponse(request=request, name="verify_email.html", context={
        "request": request,
        "registration_token": registration_token,
        "email": email,
        "message": "Код подтверждения отправлен на вашу почту"
    })


@router.post("/verify-email")
async def verify_email(
    request: Request,
    registration_token: str = Form(...),
    code: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    # 1. Декодируем токен
    try:
        payload = jwt.decode(registration_token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return templates.TemplateResponse(request=request, name="register.html", context={
            "request": request,
            "error": "Токен регистрации недействителен. Пожалуйста, зарегистрируйтесь заново."
        })

    if not _valid_registration_payload(payload):
        return templates.TemplateResponse(request=request, name="register.html", context={
            "request": request,
            "error": "Недействительный токен регистрации."
        })

    # 2. Проверяем лимит попыток
    attempts = payload.get("attempts", 0)
    if attempts >= 5:
        return templates.TemplateResponse(request=request, name="register.html", context={
            "request": request,
            "error": "Превышено количество попыток. Пожалуйста, зарегистрируйтесь заново."
        })

    # 3. Проверяем срок действия кода
    code_expire = datetime.fromisoformat(payload["code_expire"])
    if datetime.utcnow() > code_expire:
        return templates.TemplateResponse(request=request, name="verify_email.html", context={
            "request": request,
            "registration_token": registration_token,
            "email": payload["email"],
            "error": "Срок действия кода истёк. Запросите новый код."
        })

    # 4. Проверяем код
    if not _registration_code_matches(payload, code):
        # Создаём новый токен с увеличенным счётчиком попыток
        payload["attempts"] = attempts + 1
        new_token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
        return templates.TemplateResponse(request=request, name="verify_email.html", context={
            "request": request,
            "registration_token": new_token,
            "email": payload["email"],
            "error": f"Неверный код. Осталось попыток: {5 - payload['attempts']}"
        })

    # 5. Код верный — создаём пользователя
    # Повторная проверка email (мог быть занят за время ввода кода)
    existing = await db.execute(select(User).where(User.email == payload["email"]))
    if existing.scalar_one_or_none():
        return templates.TemplateResponse(request=request, name="register.html", context={
            "request": request,
            "error": "Пользователь с таким Email уже существует"
        })

    new_user_id = await generate_web_user_id(db)

    # Валидация реферера
    referrer_id = None
    ref = payload.get("ref")
    if ref:
        try:
            potential_referrer = int(ref)
            referrer_result = await db.execute(select(User).where(User.user_id == potential_referrer))
            if referrer_result.scalar_one_or_none():
                referrer_id = potential_referrer
        except (ValueError, TypeError):
            pass

    new_user = User(
        user_id=new_user_id,
        email=payload["email"],
        password_hash=payload["password_hash"],
        full_name=payload["full_name"],
        username=payload["email"].split('@')[0],
        has_received_trial=False,
        referrer_id=referrer_id,
        is_email_verified=True,
    )

    db.add(new_user)
    await db.commit()

    # 6. Логиним и редиректим
    access_token = create_access_token(
        data={"sub": str(new_user.user_id), "email": new_user.email},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    response = RedirectResponse(url="/profile/", status_code=302)
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", secure=True, httponly=True, samesite="lax")
    return response


@router.post("/resend-code")
async def resend_code(
    request: Request,
    registration_token: str = Form(...),
):
    # 1. Декодируем токен
    try:
        payload = jwt.decode(registration_token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return templates.TemplateResponse(request=request, name="register.html", context={
            "request": request,
            "error": "Токен регистрации недействителен. Зарегистрируйтесь заново."
        })

    if not _valid_registration_payload(payload):
        return templates.TemplateResponse(request=request, name="register.html", context={
            "request": request,
            "error": "Недействительный токен."
        })

    # 2. Rate-limit: не чаще 1 раза в 2 минуты
    code_sent_at = datetime.fromisoformat(payload.get("code_sent_at", "2000-01-01"))
    if (datetime.utcnow() - code_sent_at).total_seconds() < 120:
        remaining = 120 - int((datetime.utcnow() - code_sent_at).total_seconds())
        return templates.TemplateResponse(request=request, name="verify_email.html", context={
            "request": request,
            "registration_token": registration_token,
            "email": payload["email"],
            "error": f"Подождите {remaining} сек. перед повторной отправкой кода."
        })

    # 3. Генерируем новый код
    new_code = _new_email_code()

    # 4. Создаём новый токен
    payload["code_nonce"] = secrets.token_hex(16)
    payload["code_digest"] = _registration_code_digest(payload["email"], payload["code_nonce"], new_code)
    payload["code_expire"] = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
    payload["code_sent_at"] = datetime.utcnow().isoformat()
    payload["attempts"] = 0
    payload["exp"] = datetime.utcnow() + timedelta(hours=1)
    new_token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

    # 5. Отправляем
    try:
        await send_verification_email(payload["email"], new_code)
    except MailSendError as e:
        return templates.TemplateResponse(request=request, name="verify_email.html", context={
            "request": request,
            "registration_token": registration_token,
            "email": payload["email"],
            "error": str(e)
        })

    return templates.TemplateResponse(request=request, name="verify_email.html", context={
        "request": request,
        "registration_token": new_token,
        "email": payload["email"],
        "message": "Новый код отправлен на вашу почту!"
    })

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
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", secure=True, httponly=True, samesite="lax")
    return response


@router.post("/forgot-password")
async def send_reset_email(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    # 1. Ищем пользователя
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user:
        # Для безопасности можно писать "Если почта существует, мы отправили код", 
        # но для удобства скажем правду
        return templates.TemplateResponse(request=request, name="forgot_password.html", context={
            "request": request, "error": "Пользователь с таким Email не найден"
        })

    # 2. Генерируем код (6 цифр)
    code = _new_email_code()
    
    # 3. Сохраняем в БД (время жизни 15 мин)
    user.reset_code = code
    user.reset_code_expire = datetime.now() + timedelta(minutes=15)
    await db.commit()

    # 4. Отправляем письмо
    try:
        await send_reset_code(email, code)
    except MailSendError as e:
        return templates.TemplateResponse(request=request, name="forgot_password.html", context={
            "request": request, "error": str(e)
        })

    # 5. Перенаправляем на ввод кода
    return templates.TemplateResponse(request=request, name="reset_password.html", context={
        "request": request, 
        "email": email, # Передаем email, чтобы юзеру не вводить его снова
        "message": "Код отправлен на вашу почту!"
    })


@router.post("/reset-password")
async def process_reset_password(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    new_password: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    # 0. Валидация пароля
    if len(new_password) < 6:
        return templates.TemplateResponse(request=request, name="reset_password.html", context={
            "request": request, "email": email, "error": "Пароль должен содержать минимум 6 символов"
        })

    # 1. Ищем пользователя
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user:
        return templates.TemplateResponse(request=request, name="reset_password.html", context={
            "request": request, "email": email, "error": "Пользователь не найден"
        })

    # 2. Проверяем код и время
    if user.reset_code != code:
        return templates.TemplateResponse(request=request, name="reset_password.html", context={
            "request": request, "email": email, "error": "Неверный код"
        })
    
    if not user.reset_code_expire or user.reset_code_expire < datetime.now():
        return templates.TemplateResponse(request=request, name="reset_password.html", context={
            "request": request, "email": email, "error": "Срок действия кода истек"
        })

    # 3. Меняем пароль
    user.password_hash = get_password_hash(new_password)
    
    # 4. Очищаем код (чтобы нельзя было использовать повторно)
    user.reset_code = None
    user.reset_code_expire = None
    await db.commit()

    # 5. Отправляем на логин
    return templates.TemplateResponse(request=request, name="login.html", context={
        "request": request, 
        "message": "Пароль успешно изменен! Теперь войдите."
    })
