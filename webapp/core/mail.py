# webapp/core/mail.py
import os
import asyncio
import logging
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from pydantic import EmailStr

logger = logging.getLogger(__name__)

MAIL_TIMEOUT = 15  # секунд на отправку письма

def _load_mail_config() -> ConnectionConfig | None:
    """Неполный SMTP отключает письма, но не публичный сайт и бот."""
    values = {name: os.getenv(name, "").strip() for name in (
        "MAIL_SERVER", "MAIL_FROM", "MAIL_USERNAME",
    )}
    values["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD", "")
    if not all(value.strip() for value in values.values()):
        return None
    try:
        port = int(os.getenv("MAIL_PORT", "587"))
        # Порт 465 = SSL/TLS; остальные SMTP-порты используют STARTTLS.
        return ConnectionConfig(
            **values,
            MAIL_FROM_NAME=os.getenv("MAIL_FROM_NAME", "HamzAVPN"),
            MAIL_PORT=port,
            MAIL_STARTTLS=port != 465,
            MAIL_SSL_TLS=port == 465,
            USE_CREDENTIALS=True,
            VALIDATE_CERTS=True,
        )
    except ValueError:
        # Не выводим значения конфигурации или ValidationError с секретами.
        logger.warning("SMTP settings are invalid; email delivery is disabled")
        return None


conf = _load_mail_config()


async def _send_mail(subject: str, recipients: list[str], html: str):
    """Отправляет email с таймаутом."""
    if conf is None:
        raise MailSendError("Отправка писем временно недоступна: почтовый сервер не настроен.")
    try:
        message = MessageSchema(
            subject=subject,
            recipients=recipients,
            body=html,
            subtype=MessageType.html,
        )
        fm = FastMail(conf)
        await asyncio.wait_for(fm.send_message(message), timeout=MAIL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error(f"Mail send timeout ({MAIL_TIMEOUT}s) for {recipients}")
        raise MailSendError("Сервер почты не отвечает. Попробуйте позже.")
    except Exception as e:
        logger.error(f"Mail send failed for {recipients}: {e}")
        raise MailSendError("Не удалось отправить письмо. Попробуйте позже.")


class MailSendError(Exception):
    """Ошибка отправки email — содержит безопасное сообщение для пользователя."""
    pass


async def send_reset_code(email: EmailStr, code: str):
    """Отправляет код восстановления на почту"""
    html = f"""
    <div style="background-color: #081222; color: #ffffff; padding: 20px; font-family: Arial;">
        <h2 style="color: #8bb8ef;">HamzAVPN</h2>
        <p>Вы запросили сброс пароля.</p>
        <p>Ваш код подтверждения:</p>
        <h1 style="background: #102c57; padding: 10px; display: inline-block; border-radius: 8px; border: 1px solid #8bb8ef;">{code}</h1>
        <p style="color: #b2bfd2; font-size: 12px; margin-top: 20px;">Если это были не вы, просто проигнорируйте письмо.</p>
    </div>
    """
    await _send_mail("Сброс пароля | HamzAVPN", [email], html)


async def send_verification_email(email: EmailStr, code: str):
    """Отправляет код подтверждения email при регистрации"""
    html = f"""
    <div style="background-color: #081222; color: #ffffff; padding: 20px; font-family: Arial;">
        <h2 style="color: #8bb8ef;">HamzAVPN</h2>
        <p>Для подтверждения вашего email введите код:</p>
        <h1 style="background: #102c57; padding: 10px; display: inline-block; border-radius: 8px; border: 1px solid #8bb8ef;">{code}</h1>
        <p style="color: #b2bfd2; font-size: 12px; margin-top: 20px;">Код действителен 15 минут. Если вы не регистрировались, просто проигнорируйте это письмо.</p>
    </div>
    """
    await _send_mail("Подтверждение Email | HamzAVPN", [email], html)
