from dataclasses import dataclass

from environs import Env


@dataclass
class TgBot:
    token: str
    admin_ids: list[int]
    support_chat_id: int
    transaction_log_topic_id: int
    instruction_video_id: str | None
    proxy_url: str | None
    tg_bot_username: str | None
    tma_app_name: str
    ui_mode: str
    @staticmethod
    def from_env(env: Env):
        token = env.str("BOT_TOKEN")
        # env.list преобразует строку "123,456" в список [123, 456]
        admin_ids = env.list("ADMINS", subcast=int, default=[])
        support_chat_id = env.int("SUPPORT_CHAT_ID", default=0)
        transaction_log_topic_id = env.int("TRANSACTION_LOG_TOPIC_ID", default=0)
        instruction_video_id = (env.str("INSTRUCTION_VIDEO_ID", default="").strip() or None)
        proxy_url = (env.str("TG_PROXY_URL", default="").strip() or None)
        # Telegram Mini App (README.md: настройка Mini App): username бота без "@" — нужен
        # для реферальных ссылок вида t.me/<bot>/<app>?startapp=<id>. Короткое имя самого
        # Mini App задаётся в BotFather при /newapp. Обе опциональны: если
        # TG_BOT_USERNAME пуст, блок "поделиться" в /tma/referral скрывается.
        tg_bot_username = (env.str("TG_BOT_USERNAME", default="").strip() or None)
        tma_app_name = env.str("TMA_APP_NAME", default="app")
        # Режим интерфейса бота — глобальный переключатель для всех пользователей:
        #   "bot" — классические callback-кнопки (сценарии живут в хендлерах бота);
        #   "tma" — те же пункты меню открывают экраны Mini App через web_app.
        # Дефолт "bot" — обратная совместимость: режим "tma" требует настроенного
        # публичного домена и созданного в BotFather приложения, иначе Telegram
        # отвергает web_app-кнопки (см. tgbot/keyboards/inline.py::tma_mode_enabled).
        ui_mode = env.str("UI_MODE", default="bot").strip().lower()
        if ui_mode not in ("bot", "tma"):
            # Падаем на старте, а не молча откатываемся: опечатка в UI_MODE иначе
            # выглядела бы как "переключатель не работает".
            raise ValueError(f"UI_MODE должен быть 'bot' или 'tma', получено: {ui_mode!r}")
        return TgBot(token=token, admin_ids=admin_ids,
                     support_chat_id=support_chat_id,
                     transaction_log_topic_id=transaction_log_topic_id,
                     instruction_video_id=instruction_video_id,
                     proxy_url=proxy_url,
                     tg_bot_username=tg_bot_username,
                     tma_app_name=tma_app_name,
                     ui_mode=ui_mode)
@dataclass
class Platega:
    merchant_id: str
    secret: str
    api_url: str = "https://app.platega.io"

    @staticmethod
    def from_env(env: Env):
        return Platega(
            merchant_id=env.str("PLATEGA_MERCHANT_ID", default="").strip(),
            secret=env.str("PLATEGA_SECRET", default="").strip(),
            api_url=env.str("PLATEGA_API_URL", default="https://app.platega.io").strip(),
        )
@dataclass
class DataBase:
    host: str
    port: str
    user: str
    password: str
    db_name: str

    @staticmethod
    def from_env(env: Env):
        host = env.str("DB_HOST")
        port = env.str("DB_PORT")
        user = env.str("DB_USER")
        password = env.str("DB_PASSWORD")
        db_name = env.str("DB_NAME")
        return DataBase(host=host, port=port, user=user, password=password, db_name=db_name)


@dataclass
class Webhook:
    url: str
    domain: str
    use_webhook: bool

    @staticmethod
    def from_env(env: Env):
        url = env.str('SERVER_URL')
        domain = env.str('DOMAIN')
        use_webhook = env.bool('USE_WEBHOOK')
        return Webhook(url=url, domain=domain, use_webhook=use_webhook)


@dataclass
class Remnawave:
    api_url: str
    api_token: str
    squad_uuid: str
    access_cookie: str
    proxy_url: str | None

    @staticmethod
    def from_env(env: Env):
        api_url = env.str("REMNAWAVE_API_URL")
        api_token = env.str("REMNAWAVE_API_TOKEN")
        squad_uuid = env.str("REMNAWAVE_DEFAULT_SQUAD_UUID", default="")
        access_cookie = env.str("REMNAWAVE_ACCESS_COOKIE", default="")
        proxy_url = env.str("REMNAWAVE_PROXY_URL", default="").strip() or None
        return Remnawave(api_url=api_url, api_token=api_token,
                         squad_uuid=squad_uuid, access_cookie=access_cookie,
                         proxy_url=proxy_url)


@dataclass
class Config:
    tg_bot: TgBot
    webhook: Webhook
    remnawave: Remnawave
    dataBase: DataBase
    platega: Platega


def load_config():
    from environs import Env
    env = Env()
    env.read_env('.env')
    return Config(
        tg_bot=TgBot.from_env(env),
        webhook=Webhook.from_env(env),
        remnawave=Remnawave.from_env(env),
        dataBase=DataBase.from_env(env),
        platega=Platega.from_env(env)
    )
