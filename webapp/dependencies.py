# webapp/dependencies.py

from fastapi import Request
from jose import jwt, JWTError
from sqlalchemy import select

from db import async_session_maker, User
from webapp.core.security import SECRET_KEY, ALGORITHM

async def get_current_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        # Фолбэк на заголовок Authorization — в webview Telegram Desktop (Mini App)
        # cookie иногда режутся, поэтому фронт TMA дублирует токен в заголовке.
        token = request.headers.get("Authorization")
    if not token:
        return None
    
    try:
        # Убираем префикс "Bearer ", если он есть
        scheme, _, param = token.partition(" ")
        token_str = param if scheme.lower() == "bearer" else token

        payload = jwt.decode(token_str, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") == "registration":
            return None
        user_id: str = payload.get("sub")
        if user_id is None:
            return None
        
        # Получаем пользователя из БД
        async with async_session_maker() as session:
            # Важно: приводим user_id к int, так как в токене это строка
            result = await session.execute(select(User).where(User.user_id == int(user_id)))
            user = result.scalar_one_or_none()
            return user
            
    except JWTError:
        return None
    except ValueError:
        # Если ID в токене не число
        return None