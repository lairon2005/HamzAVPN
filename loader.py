import logging
import betterlogging as bl
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import load_config  # Убедитесь, что путь до конфига правильный
from remnawave.client import RemnawaveClient
from platega.client import PlategaClient
from utils.logger import APINotificationHandler, AiohttpNoiseFilter

# Загружаем конфиг
config = load_config()

api_notification_handler: APINotificationHandler | None = None

# loader.py

def setup_logging():
    """Настраивает production-логирование без секретов в HTTP URL."""
    # DEBUG для HTTP-библиотек выводит URL запросов, в том числе
    # Telegram Bot Token. В production оставляем информативный уровень.
    log_level = logging.INFO
    bl.basic_colorized_config(level=log_level)

    # Устанавливаем базовый конфиг для всех логгеров
    logging.basicConfig(
        level=log_level,
        format="%(filename)s:%(lineno)d #%(levelname)-8s [%(asctime)s] - %(name)s - %(message)s",
    )
    
    logging.getLogger("aiogram.event").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Гасим шум от интернет-сканеров, шлющих мусор в открытый порт вебхуков:
    # фильтр на самом логгере отбрасывает запись до всех хендлеров (включая
    # APINotificationHandler на root), поэтому она не попадёт ни в логи, ни
    # админу в Telegram. Реальные ошибки обработчиков проходят.
    logging.getLogger("aiohttp.server").addFilter(AiohttpNoiseFilter())
    
    logger_init = logging.getLogger(__name__)
    root_logger = logging.getLogger()

    # Отправка критических ошибок админу
    main_admin_id = config.tg_bot.admin_ids[0] if config.tg_bot.admin_ids else None
    global api_notification_handler
    if main_admin_id and api_notification_handler is None:
        api_notification_handler = APINotificationHandler(
            config.tg_bot.token,
            main_admin_id,
            redactions=(
                config.tg_bot.token,
                config.remnawave.api_token,
                config.remnawave.access_cookie,
                config.remnawave.proxy_url or "",
                config.platega.secret,
                config.dataBase.password,
                config.tg_bot.proxy_url or "",
            ),
            proxy_url=config.tg_bot.proxy_url,
        )
        api_notification_handler.setLevel(logging.ERROR)
        # Корневой logger получает ошибки из Remnawave, webapp и сервисов,
        # а не только сообщения самого loader.py.
        root_logger.addHandler(api_notification_handler)

    return logger_init


def shutdown_logging() -> bool:
    """Останавливает фоновый sender уведомлений без сетевого I/O в event loop."""
    global api_notification_handler
    handler = api_notification_handler
    if handler is None:
        return True

    logging.getLogger().removeHandler(handler)
    stopped = handler.shutdown(drain=False)
    handler.close()
    api_notification_handler = None
    return stopped

# --- Инициализируем наши "сервисы" ---

# 1. Логгер
logger = setup_logging()

# 2. Объект бота с вашими настройками
if config.tg_bot.proxy_url:
    _session = AiohttpSession(proxy=config.tg_bot.proxy_url)
    bot = Bot(
        token=config.tg_bot.token,
        session=_session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True)
    )
else:
    bot = Bot(
        token=config.tg_bot.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True)
    )

# 3. Клиент Remnawave API
remnawave_client = RemnawaveClient(
    base_url=config.remnawave.api_url,
    token=config.remnawave.api_token,
    squad_uuid=config.remnawave.squad_uuid,
    access_cookie=config.remnawave.access_cookie,
    proxy_url=config.remnawave.proxy_url,
)

platega_client = PlategaClient(config.platega.merchant_id, config.platega.secret, config.platega.api_url)
