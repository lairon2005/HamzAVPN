from database import (
    user_repo, tariff_repo, promo_repo, channel_repo, stats_repo,
    payment_repo, lifecycle_repo, settings_repo,
)
from loader import remnawave_client

from .subscription_service import SubscriptionService
from .referral_service import ReferralService
from .promo_code_service import PromoCodeService
from .user_service import UserService
from .profile_service import ProfileService
from .device_service import DeviceService
from .device_slot_service import DeviceSlotService
from .key_service import KeyService
from .admin_stats_service import AdminStatsService
from .payment_service import PaymentService
from .payment_method_service import PaymentMethodService
from .support_service import SupportService

subscription_service = SubscriptionService(user_repo, remnawave_client)
referral_service = ReferralService(user_repo, subscription_service)
promo_service = PromoCodeService(promo_repo, user_repo)
user_service = UserService(user_repo, stats_repo)
profile_service = ProfileService(user_repo, remnawave_client)
device_service = DeviceService(user_repo, remnawave_client)
device_slot_service = DeviceSlotService(user_repo, settings_repo, remnawave_client)
key_service = KeyService(user_repo, remnawave_client)
admin_stats_service = AdminStatsService(stats_repo, remnawave_client, payment_repo, lifecycle_repo)
payment_method_service = PaymentMethodService()
payment_service = PaymentService(
    subscription_service, referral_service, user_repo, tariff_repo, payment_repo,
    device_slot_service=device_slot_service,
)
support_service = SupportService(user_repo)
