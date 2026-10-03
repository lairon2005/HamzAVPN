# webapp/core/security.py
import os
from datetime import datetime, timedelta
from typing import Optional
from passlib.context import CryptContext
from jose import jwt

# Настройки
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if len(SECRET_KEY) < 32:
    raise RuntimeError("Задайте отдельный SECRET_KEY длиной не менее 32 символов в .env")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 дней

# ИЗМЕНЕНИЕ: Используем argon2 вместо bcrypt
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверка пароля (Argon2 не требует обрезки)"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Хеширование пароля (Argon2 не требует обрезки)"""
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt