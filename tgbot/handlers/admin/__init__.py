# tgbot/handlers/admin/__init__.py

from aiogram import Router

# --- Фильтры ---
# Импортируем наш фильтр, чтобы применить его ко всей админке
from tgbot.filters.admin import IsAdmin

# --- Роутеры ---
# Импортируем все наши админские "под-роутеры"
from .main import admin_main_router
from .users import admin_users_router
from .broadcast import admin_broadcast_router
from .tariffs import admin_tariffs_router
from .cancel import cancel_router
from .promocodes import admin_promo_router
from .channels import admin_channels_router
from .device_settings import admin_device_settings_router

# Создаем один большой "агрегирующий" роутер для всей админки
admin_router = Router(name="admin")

# --- ГЛОБАЛЬНЫЙ ФИЛЬТР ---
# Применяем фильтр IsAdmin ко всем хендлерам (и message, и callback_query)
# которые будут подключены к этому роутеру.
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

# Подключаем к нему все дочерние роутеры
# Порядок здесь тоже важен: сначала отмена, потом все остальное.
admin_router.include_routers(
    cancel_router,
    admin_main_router,
    admin_users_router,
    admin_broadcast_router,
    admin_tariffs_router,
    admin_promo_router,
    admin_channels_router,
    admin_device_settings_router,
)

# Экспортируем только один, уже собранный и настроенный admin_router
__all__ = [
    "admin_router",
]