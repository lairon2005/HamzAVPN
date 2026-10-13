# tgbot/handlers/user/__init__.py

from aiogram import Router, F
from aiogram.enums import ChatType

from .start import start_router
from .profile import profile_router
from .devices import devices_router
from .device_slots import device_slots_router
from .traffic import traffic_router
from .revoke_key import revoke_key_router
from .payment import payment_router
from .payment_methods import payment_methods_router
from .instruction import instruction_router
from .trial_sub import trial_sub_router
from .link_email import link_email_router
from .lifecycle import lifecycle_router
from .stars_payment import stars_payment_router
from .legal import legal_router

# Создаем один большой роутер для всех пользовательских хендлеров
user_router = Router(name="user")

# --- ВОТ ВАЖНОЕ ИЗМЕНЕНИЕ ---
# Применяем фильтр на приватный чат КО ВСЕМ хендлерам ВНУТРИ user_router
user_router.message.filter(F.chat.type == ChatType.PRIVATE)


# Подключаем к нему все дочерние роутеры
user_router.include_routers(
    legal_router,
    start_router,
    profile_router,
    devices_router,
    device_slots_router,
    traffic_router,
    revoke_key_router,
    payment_methods_router,
    payment_router,
    instruction_router,
    trial_sub_router,
    link_email_router,
    lifecycle_router,
    stars_payment_router,
)

__all__ = ["user_router"]
