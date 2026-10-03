# tgbot/handlers/user/lifecycle.py
"""Интерактивные элементы lifecycle-рассылок: опрос win-back волны 3 (§7.3)."""

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from loader import logger
from tgbot.services import subscription_service
from database import lifecycle_repo

lifecycle_router = Router()

_WINBACK_CLAIM_DAYS = 7

_SURVEY_REASONS = {
    "winback_survey_price": "дорого",
    "winback_survey_speed": "скорость/стабильность",
    "winback_survey_noneed": "больше не нужно",
}

_SURVEY_BONUS_DAYS = 5


@lifecycle_router.callback_query(F.data == "winback_claim7")
async def winback_claim7_handler(call: CallbackQuery) -> None:
    """
    Забрать «+7 дней бесплатно» из win-back волны 1 (§7.3). Начисляем ТОЛЬКО тем,
    кто реально нажал кнопку (а не всей ушедшей базе при рассылке), и не более
    одного раза — guard через lifecycle_repo (series='winback', step='claim7').
    """
    await call.answer()
    user_id = call.from_user.id

    if await lifecycle_repo.was_sent(user_id, 'winback', 'claim7'):
        text = "Вы уже забрали эти 7 дней 🙂 Продлить подписку можно в меню."
        try:
            await call.message.edit_text(text)
        except TelegramBadRequest:
            await call.message.answer(text)
        return

    try:
        await subscription_service.extend(user_id, _WINBACK_CLAIM_DAYS)
        await lifecycle_repo.mark_sent(user_id, 'winback', 'claim7')
        logger.info(f"Win-back claim7: user_id={user_id} забрал {_WINBACK_CLAIM_DAYS} дней.")
        text = (
            f"🎁 Готово! Вам начислено <b>{_WINBACK_CLAIM_DAYS} дней</b> подписки.\n\n"
            "Ключи и статус — в вашем профиле. С возвращением!"
        )
    except Exception as e:
        logger.error(f"Win-back claim7: не удалось начислить бонус user_id={user_id}: {e}", exc_info=True)
        text = (
            "Не удалось начислить дни — напишите в поддержку, мы всё исправим."
        )

    try:
        await call.message.edit_text(text)
    except TelegramBadRequest:
        await call.message.delete()
        await call.message.answer(text)


@lifecycle_router.callback_query(F.data.in_(_SURVEY_REASONS.keys()))
async def winback_survey_answer_handler(call: CallbackQuery) -> None:
    """
    Обрабатывает ответ на опрос win-back волны 3: логирует причину ухода
    и начисляет 5 дней подписки в подарок, как обещано в тексте касания.
    """
    await call.answer()
    reason = _SURVEY_REASONS[call.data]
    user_id = call.from_user.id

    try:
        await subscription_service.extend(user_id, _SURVEY_BONUS_DAYS)
        logger.info(f"Win-back survey: user_id={user_id} ответил «{reason}», начислено {_SURVEY_BONUS_DAYS} дней.")
        text = (
            f"Спасибо за ответ! 🙏 Вам начислено <b>{_SURVEY_BONUS_DAYS} дней</b> подписки.\n\n"
            "Будем рады видеть вас снова."
        )
    except Exception as e:
        logger.error(f"Win-back survey: не удалось начислить бонус user_id={user_id}: {e}", exc_info=True)
        text = (
            "Спасибо за ответ! Произошла ошибка при начислении бонуса — "
            "напишите в поддержку, мы всё исправим."
        )

    try:
        await call.message.edit_text(text)
    except TelegramBadRequest:
        await call.message.delete()
        await call.message.answer(text)
