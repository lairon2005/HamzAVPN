from dataclasses import dataclass
from datetime import datetime, timedelta

from db import PromoCode
from database.repositories.promo_code import PromoCodeRepository
from database.repositories.user import UserRepository
from tgbot.promo import get_promo_reward


@dataclass
class PromoValidationResult:
    is_valid: bool
    error_message: str | None = None
    promo: PromoCode | None = None


class PromoClaimError(Exception):
    """
    Атомарный захват промокода (PromoCodeRepository.try_claim) не удался: между
    validate() и фактической записью использования кто-то другой (или этот же
    пользователь второй раз — например, повторное нажатие кнопки из рассылки)
    успел погасить промокод первым, либо uses_left закончился именно в этот
    момент. Отличаем от ошибок Remnawave/БД отдельным типом, чтобы обработчики
    в tgbot/handlers/user/payment.py могли показать пользователю понятное
    «промокод уже использован/закончился» вместо общего «обратитесь в
    поддержку» (см. security review TOCTOU-гонки, 2026-07-26).
    """
    pass


class PromoCodeService:
    def __init__(self, promo_repo: PromoCodeRepository, user_repo: UserRepository):
        self._promo_repo = promo_repo
        self._user_repo = user_repo

    async def validate(self, code: str, user_id: int, require_discount: bool = False) -> PromoValidationResult:
        """
        Единая валидация промокода.
        require_discount: если True, отклоняет промокоды без скидки (только бонусные дни).
        """
        promo = await self._promo_repo.get_by_code(code)
        if not promo:
            return PromoValidationResult(False, "Промокод не найден.")

        if await self._promo_repo.has_user_used(user_id, promo.id):
            return PromoValidationResult(False, "Вы уже использовали этот промокод.")

        reward = get_promo_reward(promo)
        if not reward:
            return PromoValidationResult(False, "Промокод не содержит ни скидки, ни бонусных дней.")

        if require_discount and reward.kind != "discount":
            return PromoValidationResult(
                False,
                "Этот промокод дает бонусные дни, а не скидку. Введите его вручную в разделе «Промокод»."
            )

        if promo.uses_left <= 0:
            return PromoValidationResult(False, "К сожалению, этот промокод уже закончился.")

        if promo.expire_date and datetime.now() > promo.expire_date:
            return PromoValidationResult(False, "Срок действия этого промокода истек.")

        return PromoValidationResult(True, promo=promo)

    async def apply(self, user_id: int, promo: PromoCode) -> None:
        """
        Атомарно пометить промокод как использованный (скидочный путь и ручной
        ввод бонусного промокода — см. process_promo_code).

        Идёт через PromoCodeRepository.try_claim(), а не через прямое
        чтение/запись: это закрывает TOCTOU-гонку между validate() (проверил
        has_user_used/uses_left) и записью использования — окно между ними
        размером в сетевую задержку, которое конкурентные апдейты от Telegram
        (ThrottlingMiddleware L1=0.5s его не закрывает, aiogram обрабатывает
        апдейты отдельными тасками) успевают проскочить. Если захват не удался
        (гонка проиграна или промокод уже исчерпан) — бросаем PromoClaimError,
        чтобы вызывающий хендлер показал понятное сообщение, а не притворялся,
        что промокод применён.
        """
        claimed = await self._promo_repo.try_claim(user_id, promo)
        if not claimed:
            raise PromoClaimError(
                f"Промокод '{promo.code}' уже использован пользователем {user_id} "
                f"или исчерпан (проиграна гонка try_claim)."
            )

    async def release(self, user_id: int, promo: PromoCode) -> None:
        """
        Компенсирующий откат apply(): вернуть захваченный промокод пользователю,
        если шаг после захвата не довёл сценарий до конца (например, не удалось
        показать тарифы со скидкой). Без этого попытка сгорает молча — повторное
        нажатие упрётся в «вы уже использовали этот промокод», хотя скидкой
        человек так и не воспользовался. Симметрично компенсации внутри
        apply_bonus_days().
        """
        await self._promo_repo.release_claim(user_id, promo)

    async def apply_bonus_days(self, user_id: int, promo: PromoCode, subscription_service):
        """Применить промокод с бонусными днями: claim → extend, с компенсацией.

        Порядок изменён относительно первой версии (инцидент 2026-07-23:
        ConnectTimeout Remnawave во время рассылки LOVEHamzAVPN — там extend()
        вызывался ДО пометки использованным, чтобы промокод не «сгорал» при
        сбое панели). Тот порядок открывал TOCTOU-гонку: между validate() и
        записью использования оставалось окно в 2-3 HTTP-вызова к Remnawave,
        и повторное нажатие той же кнопки из рассылки успевало пройти
        validate() ещё раз до коммита первого запроса (security review
        2026-07-26).

        Новый порядок сохраняет оба свойства:
          1. Сначала атомарно захватываем промокод (try_claim) — если захват
             не удался (гонка проиграна / уже исчерпан), бросаем
             PromoClaimError, дни не начисляем.
          2. Только после успешного захвата вызываем subscription_service.extend().
          3. Если extend() упал — компенсируем захват (release_claim: снимаем
             отметку использования, возвращаем uses_left) и пробрасываем
             исключение дальше. Промокод по-прежнему не «сгорает» при сбое
             Remnawave, а гонка закрыта на уровне БД (уникальный индекс +
             условный декремент в try_claim).
        """
        claimed = await self._promo_repo.try_claim(user_id, promo)
        if not claimed:
            raise PromoClaimError(
                f"Промокод '{promo.code}' уже использован пользователем {user_id} "
                f"или исчерпан (проиграна гонка try_claim)."
            )

        try:
            result = await subscription_service.extend(user_id, promo.bonus_days)
        except Exception:
            await self._promo_repo.release_claim(user_id, promo)
            raise
        return result

    # --- Admin methods ---

    async def create(self, code: str, bonus_days: int = 0, discount_percent: int = 0,
                     max_uses: int = 1, expire_date=None):
        return await self._promo_repo.create(code, bonus_days, discount_percent, max_uses, expire_date)

    async def delete(self, promo_id: int):
        return await self._promo_repo.delete_by_id(promo_id)

    async def get_all(self):
        return await self._promo_repo.get_all()

    async def get_by_code(self, code: str):
        return await self._promo_repo.get_by_code(code)

    async def ensure_min_ttl(self, code: str, hours: int) -> None:
        """
        Продлевает срок действия промокода минимум на `hours` вперёд от текущего момента,
        если он ещё не создан достаточно "живым". Используется lifecycle-рассылками перед
        отправкой промо-касания (BACK30/COMEBACK/START20), т.к. у промокода один общий
        expire_date на всех получателей, а не персональный TTL "N часов с момента отправки".
        """
        promo = await self._promo_repo.get_by_code(code)
        if not promo:
            return
        min_expire = datetime.now() + timedelta(hours=hours)
        if not promo.expire_date or promo.expire_date < min_expire:
            await self._promo_repo.update_expire(promo.id, min_expire)
