# db.py (ФИНАЛЬНАЯ ВЕРСИЯ НА SQLAlchemy)

import datetime
from sqlalchemy import (
    create_engine, BigInteger, String, DateTime, Boolean, ForeignKey,
    Integer, Float, select, func, UniqueConstraint, Index, text, URL
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import load_config

# --- 1. Настройка ---
config = load_config()
db_config = config.dataBase
DSN = URL.create('postgresql+asyncpg', username=db_config.user, password=db_config.password,
                 host=db_config.host, port=int(db_config.port), database=db_config.db_name)
SYNC_DSN = URL.create('postgresql+psycopg2', username=db_config.user, password=db_config.password,
                     host=db_config.host, port=int(db_config.port), database=db_config.db_name)

# Создаем асинхронный "движок" и фабрику сессий
async_engine = create_async_engine(DSN)
async_session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

# --- 2. Базовая модель ---
class Base(DeclarativeBase):
    pass

# --- 3. Определяем все ваши модели на новом синтаксисе ---
class User(Base):
    __tablename__ = 'users'
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=True)
    full_name: Mapped[str] = mapped_column(String)
    reg_date: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)
    subscription_end_date: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    subscription_data_limit_gb: Mapped[int] = mapped_column(Integer, nullable=True)
    vpn_username: Mapped[str] = mapped_column(String, unique=True, nullable=True)
    remnawave_uuid: Mapped[str] = mapped_column(String(36), unique=True, nullable=True)
    
    # --- НОВОЕ ПОЛЕ ДЛЯ ОТСЛЕЖИВАНИЯ ТРИАЛА ---
    has_received_trial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    
    referrer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.user_id', ondelete='SET NULL'), nullable=True)
    referral_bonus_days: Mapped[int] = mapped_column(Integer, default=0)
    is_first_payment_made: Mapped[bool] = mapped_column(Boolean, default=False)
    support_topic_id: Mapped[int] = mapped_column(Integer, nullable=True)
    
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=True)
    
    reset_code: Mapped[str] = mapped_column(String(10), nullable=True)
    reset_code_expire: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)

    verification_code: Mapped[str] = mapped_column(String(10), nullable=True)
    verification_code_expire: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')

    # Активен ли аккаунт для рассылок. False = пользователь заблокировал бота
    # (ставится при TelegramForbiddenError в джобах рассылок), такие юзеры
    # пропускаются, чтобы не долбить Telegram и не засорять логи. Возврат в
    # активные — в get_or_create при следующем обращении пользователя к боту.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default='true', nullable=False)

    # Сколько дополнительных устройств (сверх базового лимита тарифа) оплачено.
    # Слоты живут по дате подписки: действуют до subscription_end_date и
    # обнуляются джобом, когда подписка истекла (см. sync_device_limits в
    # tgbot/services/scheduler.py). В панель уходит абсолютное значение
    # hwidDeviceLimit = базовый лимит + extra_devices — иначе персональный
    # лимит навсегда отвязывается от глобального fallbackDeviceLimit.
    extra_devices: Mapped[int] = mapped_column(Integer, default=0, server_default='0', nullable=False)

class Tariff(Base):
    __tablename__ = 'tariffs'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String)
    price: Mapped[float] = mapped_column(Float)
    duration_days: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- Тарифы 2.0: квота трафика, лоялти-цена, подсветка ---
    data_limit_gb: Mapped[int] = mapped_column(Integer, nullable=True)      # NULL или 0 → безлимит, сброс раз в месяц
    loyalty_price: Mapped[float] = mapped_column(Float, nullable=True)     # "цена навсегда" при непрерывном продлении
    is_highlighted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # метка "⭐ выбор большинства"

    # Цена в Telegram Stars (XTR), README.md: оплата Telegram Stars. NULL → оплата
    # Stars для этого тарифа недоступна (кнопка "⭐" не показывается в tariffs.html).
    price_stars: Mapped[int] = mapped_column(Integer, nullable=True)

class PromoCode(Base):
    __tablename__ = 'promo_codes'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True)
    bonus_days: Mapped[int] = mapped_column(Integer, default=0)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    expire_date: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    uses_left: Mapped[int] = mapped_column(Integer, default=1)

class UsedPromoCode(Base):
    __tablename__ = 'used_promo_codes'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.user_id'))
    promo_code_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('promo_codes.id'))
    used_date: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)

    # Бэкстоп на уровне БД против TOCTOU-гонки validate()/use(): без него два
    # конкурентных апдейта могут оба пройти проверку has_user_used() до того, как
    # первый закоммитит свою строку, и погасить один и тот же промокод дважды
    # (см. security review 2026-07-26). PromoCodeRepository.try_claim() полагается
    # на этот индекс через INSERT ... ON CONFLICT DO NOTHING.
    __table_args__ = (
        UniqueConstraint('user_id', 'promo_code_id', name='uq_used_promo_user_code'),
    )

class Payment(Base):
    __tablename__ = 'payments'
    __table_args__ = (
        Index('uq_payment_user_pending', 'user_id', unique=True,
              postgresql_where=text("status = 'pending' AND source NOT IN ('stars', 'auto')"),
              sqlite_where=text("status = 'pending' AND source NOT IN ('stars', 'auto')")),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=True)
    provider_payment_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.user_id'))
    tariff_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('tariffs.id'), nullable=True)
    original_amount: Mapped[float] = mapped_column(Float)
    final_amount: Mapped[float] = mapped_column(Float)
    promo_code: Mapped[str] = mapped_column(String, nullable=True)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default='pending')  # pending / succeeded / failed / refunded / cancelled
    currency: Mapped[str] = mapped_column(String(3), default='RUB', server_default='RUB', nullable=False)
    payment_url: Mapped[str] = mapped_column(String, nullable=True)
    duration_days: Mapped[int] = mapped_column(Integer, default=0, server_default='0', nullable=False)
    data_limit_gb: Mapped[int] = mapped_column(Integer, nullable=True)
    last_checked_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    traffic_reset_done: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false', nullable=False)
    sync_pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false', nullable=False)
    referrer_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String, default='bot')  # bot / web / tma / stars / auto (списание подписки)

    # 'subscription' — покупка/продление тарифа (в т.ч. вместе со слотами устройств),
    # 'devices' — докупка слотов в середине оплаченного периода (tariff_id = NULL,
    # подписка не продлевается). Вебхуку нужно различать: во втором случае
    # трогать expireAt и квоту трафика нельзя.
    kind: Mapped[str] = mapped_column(String(16), default='subscription', server_default='subscription', nullable=False)

    # Сколько слотов устройств оплачено этим платежом. Для kind='subscription'
    # это подтверждение уже имеющихся слотов на новый срок, для kind='devices' —
    # сколько слотов добавить к User.extra_devices.
    extra_devices: Mapped[int] = mapped_column(Integer, default=0, server_default='0', nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)
    completed_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)

    # Telegram Stars (XTR), README.md: оплата Telegram Stars. Для source='stars' —
    # "сырой" telegram_payment_charge_id из Message.successful_payment, нужен
    # verbatim для рефандов через Bot API refundStarPayment(user_id, charge_id).
    # NULL для всех остальных источников (Platega-платежей).
    telegram_payment_charge_id: Mapped[str] = mapped_column(String, unique=True, nullable=True)


class RecurringSubscription(Base):
    """
    Рекуррентная СБП-подписка Platega (автопродление).

    Деньги списывает Platega по своему расписанию и присылает callback на каждое
    списание; дни выдаются тем же конвейером, что и разовый платёж: каждое списание
    становится строкой Payment (source='auto', provider_payment_id = ID списания).
    Сумма и состав подписки (тариф, слоты устройств) фиксируются при создании —
    Platega не умеет менять цену действующей подписки.

    status: pending (ждём привязку счёта в банке, 30 минут) / active / past_due
    (списание не прошло, повторов не будет) / cancelled / failed (привязка не удалась).
    Живыми считаются pending и active — не больше одной на пользователя.
    """
    __tablename__ = 'recurring_subscriptions'
    __table_args__ = (
        Index('uq_recurring_user_live', 'user_id', unique=True,
              postgresql_where=text("status IN ('pending', 'active')"),
              sqlite_where=text("status IN ('pending', 'active')")),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    platega_subscription_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.user_id'), index=True)
    tariff_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('tariffs.id'))
    extra_devices: Mapped[int] = mapped_column(Integer, default=0, server_default='0', nullable=False)
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default='RUB', server_default='RUB', nullable=False)
    status: Mapped[str] = mapped_column(String(16), default='pending', server_default='pending', nullable=False)
    payment_url: Mapped[str] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, default='bot')
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)
    activated_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    last_charge_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    next_charge_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    charges_ok: Mapped[int] = mapped_column(Integer, default=0, server_default='0', nullable=False)
    last_checked_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)


class LifecycleMessage(Base):
    """
    Единый трекер «касаний» для всех lifecycle-серий (напоминания о продлении,
    win-back для ушедших плативших, дрип активации неплативших).

    UNIQUE(user_id, series, step) не даёт отправить одно и то же касание дважды.
    `step` для повторяющихся событий (например, продление подписки каждый месяц)
    кодируется вместе с "циклом" (датой-якорем), чтобы серия могла повториться
    заново в следующем цикле — см. `_cycle_key()` в tgbot/services/scheduler.py.
    Промо-касания (несущие скидку/подарок, из-за которых есть риск бана бота при
    слишком частой рассылке) дополнительно помечаются префиксом "promo:" в `step`.
    """
    __tablename__ = 'lifecycle_messages'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.user_id', ondelete='CASCADE'))
    series: Mapped[str] = mapped_column(String(32))    # 'renewal' | 'winback' | 'activation'
    step: Mapped[str] = mapped_column(String(64))       # напр. 'd-3_20260716', 'promo:wave2_20260701'
    sent_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.now)

    __table_args__ = (
        UniqueConstraint('user_id', 'series', 'step', name='uq_lifecycle_user_series_step'),
    )


class Channel(Base):
    __tablename__ = 'channels'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String)
    invite_link: Mapped[str] = mapped_column(String)


class AppSetting(Base):
    """
    Изменяемые из админки настройки сервиса (key → value строкой).

    Зачем не .env: цену доп. устройства и лимиты админ меняет заметно чаще, чем
    выкатывается релиз, а правка .env требует refresh.sh и пересоздания
    контейнеров. Значения читаются через SettingsRepository с кэшем в памяти
    (ключей единицы, запрос на каждый показ тарифов не нужен).

    Известные ключи — в tgbot/services/device_pricing.py.
    """
    __tablename__ = 'app_settings'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now
    )


# --- 4. Функция для создания таблиц ---
def setup_database_sync():
    """Создает таблицы синхронно при старте бота."""
    engine = create_engine(SYNC_DSN)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    print("INFO: Database tables created or already exist via SQLAlchemy.")