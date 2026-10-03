import asyncio
import hashlib

from aiogram import Dispatcher, F, Bot
from aiogram.enums import ChatType
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat, BotCommandScopeAllPrivateChats
from aiogram.utils.callback_answer import CallbackAnswerMiddleware
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# --- ШАГ 1: Импортируем готовые объекты из loader ---
# Мы импортируем уже настроенные: bot, config, logger и КЛИЕНТ REMNAWAVE
from loader import bot, config, logger, remnawave_client, platega_client, shutdown_logging

# --- ШАГ 2: Импортируем наши новые модули и хендлеры ---
from db import setup_database_sync
from tgbot.handlers import routers_list
from tgbot.middlewares.flood import ThrottlingMiddleware
from tgbot.handlers.webhook_handlers import platega_webhook_handler
from utils import broadcaster

# Telegram callback защищает, в том числе, сообщения об оплате Stars.
TELEGRAM_WEBHOOK_SECRET = hashlib.sha256(
    f'{config.tg_bot.token}:HamzAVPN:webhook'.encode()
).hexdigest()

scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
async def on_startup(bot, remnawave): # Добавили remnawave в аргументы
    """Выполняется при запуске бота."""
    # 1. Инициализируем базу данных
    setup_database_sync()

    # 1.1 Идемпотентно создаём промокоды lifecycle-рассылок (BACK30/COMEBACK/START20)
    try:
        from tgbot.services.lifecycle_promos import ensure_lifecycle_promo_codes
        await ensure_lifecycle_promo_codes()
    except Exception as e:
        logger.error(f"❌ Не удалось инициализировать lifecycle-промокоды: {e}", exc_info=True)

    # 2. Запускаем планировщик
    try:
        scheduler.start()
        logger.info("✅ Scheduler started successfully.")
    except Exception as e:
        logger.error(f"❌ Failed to start scheduler: {e}", exc_info=True)

    # ...
    from tgbot.services.scheduler import schedule_jobs
    schedule_jobs(scheduler, bot)
    # Можно добавить проверку соединения с Remnawave
    # if await remnawave.is_online():
    #     logger.info("Remnawave panel is online.")
    # else:
    #     logger.error("Could not connect to Remnawave panel!")

    # 2. Отправляем сообщение админу о запуске
    await broadcaster.broadcast(bot, config.tg_bot.admin_ids, "Бот запущен")
    logger.info("Startup message sent to admin.")

    # 3. Устанавливаем команды меню
    await register_commands(bot)
    logger.info("Bot commands registered.")

    # 4. Установка вебхука
    if config.webhook.use_webhook:
        webhook_url = f"https://{config.webhook.domain}{config.webhook.url}"
        await bot.set_webhook(webhook_url, drop_pending_updates=False, secret_token=TELEGRAM_WEBHOOK_SECRET) # Добавлено drop_pending_updates
        logger.info(f"Webhook set to: {webhook_url}")
    else:
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("Polling mode: Webhook deleted and pending updates dropped.")

# bot.py

async def register_commands(bot: Bot):
    logger.info("Registering bot commands...")

    # --- 1. Устанавливаем команды для ВСЕХ по умолчанию ---
    user_commands = [
        BotCommand(command='start', description='🏠 Главное меню'),
        BotCommand(command='documents', description='Документы, тарифы и поддержка'),
        BotCommand(command='profile', description='👤 Мой профиль'),
        BotCommand(command='support', description='💬 Поддержка'),
        BotCommand(command='referral', description='🤝 Реф. программа'),
        BotCommand(command='instruction', description='📲 Инструкция'),
        BotCommand(command='promo', description='🎁Ввести промокод'),
    ]
    await bot.set_my_commands(user_commands, BotCommandScopeDefault())

    # --- 2. Устанавливаем РАСШИРЕННЫЕ команды для АДМИНОВ ---
    # Этот набор ПЕРЕЗАПИШЕТ дефолтный для конкретных пользователей
    admin_commands = user_commands + [
        BotCommand(command='admin', description='👑 Админ-панель'),
        BotCommand(command='cancel', description='❌ Отменить действие'),
    ]
    if config.tg_bot.admin_ids:
        for admin_id in config.tg_bot.admin_ids:
            try:
                # Устанавливаем команды персонально для каждого админа
                await bot.set_my_commands(admin_commands, BotCommandScopeChat(chat_id=admin_id))
            except Exception as e:
                logger.error(f"Failed to set admin commands for {admin_id}: {e}")

    # --- 3. Устанавливаем команды для чата поддержки ---
    support_chat_commands = [
        BotCommand(command='close', description='🔒 Закрыть тикет'),
    ]
    if config.tg_bot.support_chat_id:
        try:
            await bot.set_my_commands(
                support_chat_commands, 
                BotCommandScopeChat(chat_id=config.tg_bot.support_chat_id)
            )
        except Exception as e:
            logger.error(f"Failed to set support chat commands: {e}")


def register_global_middlewares(dp: Dispatcher):
    """Регистрирует глобальные мидлвари."""
    middleware_types = [
        ThrottlingMiddleware(),
    ]
    for middleware_type in middleware_types:
        dp.message.outer_middleware(middleware_type)
        dp.callback_query.outer_middleware(middleware_type)
    # Telegram принимает answerCallbackQuery ограниченное время. Подтверждаем
    # нажатие сразу, до потенциально долгого запроса к Remnawave.
    dp.callback_query.outer_middleware(CallbackAnswerMiddleware(pre=True))
    logger.info("Global middlewares registered.")


def main_webhook():
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage, remnawave=remnawave_client)
    dp.include_routers(*routers_list)
    register_global_middlewares(dp)
    dp.startup.register(on_startup)

    app = web.Application()
    app['bot'] = bot
    app['dp'] = dp

    app['remnawave'] = remnawave_client # Это у вас уже должно быть
    app['config'] = config # <--- ДОБАВЬТЕ ЭТУ СТРОКУ

    async def close_external_resources(_app):
        try:
            await remnawave_client.aclose()
            await platega_client.aclose()
        finally:
            await asyncio.to_thread(shutdown_logging)

    app.on_cleanup.append(close_external_resources)


    # --- НАШИ ИЗМЕНЕНИЯ ---
    # 1. Регистрируем обработчик для вебхуков Telegram
    telegram_webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=TELEGRAM_WEBHOOK_SECRET)
    telegram_webhook_handler.register(app, path=config.webhook.url)

    # 2. Регистрируем обработчик для вебхуков Platega на отдельный путь
    app.router.add_post('/platega', platega_webhook_handler)

    setup_application(app, dp, bot=bot, remnawave=remnawave_client)
    
    logger.info("Starting bot in webhook mode...")
    web.run_app(app, host='0.0.0.0', port=8081)


async def main_polling():
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage, remnawave=remnawave_client)
    dp.include_routers(*routers_list)
    register_global_middlewares(dp)
    try:
        # Вызываем on_startup до запуска основных процессов
        await on_startup(bot, remnawave_client)

        logger.info("Starting bot in polling mode...")

        # Создаем задачу для запуска веб-сервера Platega в фоне
        platega_server_task = asyncio.create_task(start_platega_webhook_server(dp))

        # Создаем задачу для запуска поллинга Telegram в фоне
        polling_task = asyncio.create_task(dp.start_polling(bot))

        # Запускаем обе задачи и ждем их завершения (что в норме не произойдет)
        await asyncio.gather(
            polling_task,
            platega_server_task
        )
    finally:
        try:
            await remnawave_client.aclose()
            await platega_client.aclose()
        finally:
            await asyncio.to_thread(shutdown_logging)


async def start_platega_webhook_server(dp: Dispatcher):
    app = web.Application()
    
    # "Внедряем" в приложение все нужные нам объекты
    app['bot'] = bot
    app['remnawave'] = remnawave_client # <--- ВОТ ЭТА СТРОКА РЕШАЕТ ПРОБЛЕМУ
    app['config'] = config         # <--- ЭТА СТРОКА НУЖНА ДЛЯ ОПОВЕЩЕНИЙ АДМИНА
    app['dp'] = dp
    
    app.router.add_post('/platega', platega_webhook_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host='0.0.0.0', port=8081)
    await site.start()
    logger.info("Platega webhook server started on port 8081")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    # Логирование уже настроено в loader.py
    logger.info("Initializing bot...")
    
    if config.webhook.use_webhook:
        main_webhook()
    else:
        try:
            asyncio.run(main_polling())
        except (KeyboardInterrupt, SystemExit):
            logger.warning("Bot stopped!")
