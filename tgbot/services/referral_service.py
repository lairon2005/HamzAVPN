from database.repositories.user import UserRepository
from tgbot.services.subscription_service import SubscriptionService
from loader import logger

# Реферальная программа (§7.5) — длительность начислений в днях. Единый источник
# правды: значения переиспользуются в текстах tgbot/handlers/user/start.py, чтобы
# UI не расходился с фактическим начислением.
REFERRAL_TRIAL_DAYS = 3          # другу — пробный период за переход по ссылке
REFERRER_LAUNCH_BONUS_DAYS = 3   # рефереру — сразу за факт приглашения
REFERRER_PAYMENT_BONUS_DAYS = 7  # рефереру — после первой оплаты друга


class ReferralService:
    def __init__(self, user_repo: UserRepository, subscription_service: SubscriptionService):
        self._user_repo = user_repo
        self._subscription_service = subscription_service

    async def activate_new_user_referral(self, user_id: int, referrer_id: int, bonus_days: int = REFERRAL_TRIAL_DAYS,
                                          referrer_launch_bonus_days: int = REFERRER_LAUNCH_BONUS_DAYS):
        """
        Регистрация нового реферала: установить реферера + выдать пробную подписку другу
        (bonus_days) + начислить рефереру бонус за сам факт привлечения (§7.5,
        referrer_launch_bonus_days — «+3 дня за запуск»).

        Анти-абуз / идемпотентность:
          - self-referral (referrer_id == user_id) — бонус рефереру не начисляется;
          - referrer должен существовать в БД;
          - бонус рефереру за «запуск» начисляется только при ПЕРВИЧНОЙ установке
            referrer_id у пользователя — если у user_id уже стоит referrer_id
            (повторный вызов, например баг в вызывающем коде), повторного начисления
            не будет — только продление триала другу.
        """
        user = await self._user_repo.get(user_id)
        if user is None:
            logger.warning(f"Referral: user {user_id} not found, skip referral activation")
            return None

        already_had_referrer = user.referrer_id is not None
        is_self_referral = referrer_id == user_id
        referrer = None if (already_had_referrer or is_self_referral) else await self._user_repo.get(referrer_id)

        if is_self_referral:
            logger.warning(f"Referral: user {user_id} tried to refer themselves, skip referrer bonus")
        elif already_had_referrer:
            logger.info(f"Referral: user {user_id} already has a referrer set, skip re-award")
        elif referrer is None:
            logger.warning(f"Referral: referrer {referrer_id} not found, skip referrer bonus")
        else:
            await self._user_repo.set_referrer(user_id, referrer_id)

        # Другу — пробная подписка (всегда, независимо от анти-абуз проверок выше)
        result = await self._subscription_service.extend(user_id, bonus_days)
        logger.info(f"Referral bonus: user {user_id} got {bonus_days} days via referrer {referrer_id}")

        # Рефереру — бонус за сам факт привлечения друга (только если анти-абуз проверки пройдены)
        if referrer is not None and not already_had_referrer and not is_self_referral:
            try:
                await self._subscription_service.extend(referrer_id, referrer_launch_bonus_days, data_limit_gb=None)
                await self._user_repo.add_bonus_days(referrer_id, days=referrer_launch_bonus_days)
                logger.info(
                    f"Referral bonus: referrer {referrer_id} got {referrer_launch_bonus_days} days "
                    f"for inviting {user_id} (launch bonus)"
                )
            except Exception as e:
                logger.error(f"Referral: failed to award launch bonus to referrer {referrer_id}: {e}")

        return result

    async def process_first_payment_bonus(self, user_who_paid_id: int,
                                           bonus_days: int = REFERRER_PAYMENT_BONUS_DAYS) -> int | None:
        """
        Бонус рефереру при первой оплате друга (§7.5 — 7 дней).
        Возвращает referrer_id если бонус начислен, None иначе.
        """
        user = await self._user_repo.get(user_who_paid_id)
        if not (user and user.referrer_id and not user.is_first_payment_made):
            return None

        referrer = await self._user_repo.get(user.referrer_id)
        if not referrer:
            return None

        referrer_id = user.referrer_id

        try:
            # extend() сам синхронизирует Remnawave (создаст пользователя, если его ещё нет)
            # и НЕ трогает квоту трафика (data_limit_gb=None) — не затирает купленную квоту.
            await self._subscription_service.extend(referrer_id, bonus_days, data_limit_gb=None)
            await self._user_repo.add_bonus_days(referrer_id, days=bonus_days)

            logger.info(f"Referral bonus: Granted {bonus_days} days to referrer {referrer_id}")
            return referrer_id
        except Exception as e:
            logger.error(f"Error handling referral bonus for {referrer_id}: {e}")
            return None
