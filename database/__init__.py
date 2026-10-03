from db import async_session_maker
from database.repositories.user import UserRepository
from database.repositories.tariff import TariffRepository
from database.repositories.promo_code import PromoCodeRepository
from database.repositories.channel import ChannelRepository
from database.repositories.stats import StatsRepository
from database.repositories.payment import PaymentRepository
from database.repositories.lifecycle import LifecycleRepository
from database.repositories.settings import SettingsRepository

user_repo = UserRepository(async_session_maker)
tariff_repo = TariffRepository(async_session_maker)
promo_repo = PromoCodeRepository(async_session_maker)
channel_repo = ChannelRepository(async_session_maker)
stats_repo = StatsRepository(async_session_maker)
payment_repo = PaymentRepository(async_session_maker)
lifecycle_repo = LifecycleRepository(async_session_maker)
settings_repo = SettingsRepository(async_session_maker)
