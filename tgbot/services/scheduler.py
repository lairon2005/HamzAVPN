# tgbot/services/scheduler.py

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from datetime import datetime, timedelta

from database import user_repo, tariff_repo, lifecycle_repo, stats_repo, channel_repo
from tgbot.keyboards.inline import (
    tariffs_keyboard, lifecycle_cta_keyboard, winback_survey_keyboard,
    tma_web_app_button, tma_mode_enabled,
)
from utils import broadcaster
from .utils import decline_word
from loader import logger, config

# --- 1. Основная функция, которую будет вызывать планировщик ---

async def notify_admins(bot: Bot, text: str) -> None:
    """Отправляет текстовое уведомление всем админам (config.tg_bot.admin_ids).

    Используется всеми джобами планировщика для отчёта о результатах прогона.
    Ошибки отправки отдельным админам не должны валить джобу — broadcast() сам
    их проглатывает и логирует, здесь дополнительно страхуемся try/except.
    """
    try:
        await broadcaster.broadcast(bot, config.tg_bot.admin_ids, text)
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление админам: {e}")


async def send_reminder(bot: Bot, user, text: str) -> bool:
    """Универсальная функция для отправки напоминания с клавиатурой тарифов.

    Возвращает True при успешной отправке и False при ошибке — используется
    вызывающей стороной для подсчёта статистики (см. check_subscriptions).
    """
    # Заблокировавшие бота помечены is_active=False — не дёргаем Telegram
    # впустую и не засоряем логи тысячами Forbidden.
    if not user.is_active:
        return False
    try:
        active_tariffs = await tariff_repo.get_active()
        tariffs_list = list(active_tariffs) if active_tariffs else []

        reply_markup = tariffs_keyboard(tariffs_list) if tariffs_list else None
        if reply_markup is not None and tma_mode_enabled():
            # Кнопка Mini App поверх обычной клавиатуры тарифов (README.md
            # фаза 4, пункт 3) — только в режиме UI_MODE=tma. send_reminder всегда
            # шлёт в личку конкретному пользователю (chat_id=user.user_id), группы
            # сюда не попадают, так что web_app-кнопка допустима.
            reply_markup.inline_keyboard.append(
                [tma_web_app_button("🚀 Продлить в приложении", "/tma/tariffs")]
            )

        await bot.send_message(
            chat_id=user.user_id,
            text=text,
            reply_markup=reply_markup
        )
        logger.info(f"Sent reminder to user {user.user_id}")
        return True
    except TelegramForbiddenError:
        # Пользователь заблокировал бота — помечаем неактивным, чтобы будущие
        # прогоны его пропускали. Ожидаемо, не ошибка → уровень INFO.
        await user_repo.set_active(user.user_id, False)
        logger.info(f"User {user.user_id} blocked the bot — marked inactive")
        return False
    except Exception as e:
        logger.warning(f"Failed to send reminder to user {user.user_id}. Error: {e}")
        return False


# --- 2. Основная функция, которую вызывает планировщик ---

async def check_subscriptions(bot: Bot):
    """Ранний нудж за 7 дней до истечения подписки.

    Шаги D-3 / D-1 / D0 / D+2 / D+5 теперь ведёт lifecycle-серия
    `lifecycle_renewal_reminders` (§7.2) — она канонический источник напоминаний
    о продлении. Здесь остался ТОЛЬКО ранний сигнал за 7 дней, которого в новой
    серии нет; иначе на 3-й и 1-й день пользователь получал бы по два сообщения.
    """
    logger.info("Scheduler job: Running subscription check (7-day early nudge)...")

    count = 0
    errors = 0
    days_left = 7
    users_to_remind = await user_repo.get_with_expiring_subscription(days_left)
    # Подписка продлится сама — напоминание «продлите» только собьёт с толку.
    from database import recurring_repo
    autorenew_ids = await recurring_repo.get_active_user_ids()
    users_to_remind = [u for u in users_to_remind if u.user_id not in autorenew_ids]
    if not users_to_remind:
        return

    logger.info(f"Found {len(users_to_remind)} users with {days_left} days left.")
    day_word = decline_word(days_left, ['день', 'дня', 'дней'])
    text = (
        f"👋 Привет, {{user_full_name}}!\n\n"
        f"Напоминаем, что ваша подписка истекает через <b>{days_left} {day_word}</b>.\n\n"
        "Чтобы не потерять доступ, пожалуйста, продлите ее."
    )
    for user in users_to_remind:
        ok = await send_reminder(bot, user, text.format(user_full_name=user.full_name))
        if ok:
            count += 1
        else:
            errors += 1

    await notify_admins(
        bot,
        "✅ <b>Проверка подписок завершена</b> (нудж за 7 дней)\n\n"
        f"👥 Найдено пользователей: {len(users_to_remind)}\n"
        f"👍 Доставлено: {count}\n"
        f"👎 Ошибок: {errors}"
    )


# --- 4. LIFECYCLE-МЕССЕНДЖИНГ (дрип-серии) ---
#
# Единый каркас трекинга «касаний»: LifecycleRepository (database/repositories/
# lifecycle.py) + таблица lifecycle_messages(user_id, series, step, sent_at) с
# UNIQUE(user_id, series, step). Три серии ниже используют его одинаково:
#   1. renewal    — §7.2, напоминания о продлении D-3…D+5 + grace 48ч
#   2. winback    — §7.3, win-back ушедших плативших, 3 волны
#   3. activation — §7.4, дрип активации неплативших, 4 касания за 14 дней
#
# Идемпотентность и антидубли: перед отправкой каждого шага — was_sent(),
# после успешной отправки — mark_sent(). Для повторяющихся событий (продление
# подписки каждый месяц) шаг кодируется вместе с "циклом" — датой-якорем
# (_cycle_key), чтобы напоминания могли повториться в следующем периоде.
#
# Анти-спам: НЕ более 1 промо-касания (со скидкой/подарком) в неделю на
# пользователя — это касания win-back волн, дрипа активации и D+5-скидки
# renewal-серии. Они помечаются префиксом "promo:" в step и проверяются через
# _promo_allowed() перед отправкой. Обычные транзакционные напоминания
# (D-3/D-1/D0/D+2-грейс о СОБСТВЕННОЙ подписке пользователя) под лимит не
# попадают — это не маркетинговая рассылка, а сервисное уведомление.
# =============================================================================

PROMO_ANTISPAM_WINDOW = timedelta(days=7)

# Грейс-очистка Remnawave-конфига после простоя подписки — НЕОБРАТИМОЕ действие
# (удаляет пользователя в Remnawave). Раньше в системе автоудаления НЕ было вообще.
# Держим ВЫКЛЮЧЕННЫМ по умолчанию: текст D+2 остаётся предупреждением, но реально
# ничего не удаляем, пока это не включат осознанным отдельным решением.
GRACE_DELETE_ENABLED = False

# Предохранители против «залпа» на первом прогоне (сегментные запросы открыты в
# прошлое, поэтому без ограничений первый запуск разослал бы сообщения всей
# исторической базе разом → flood-wait и риск бана бота, см. риск «не бомбить всех»).
#   • LIFECYCLE_SEND_DELAY  — пауза между отправками внутри одного прогона.
#   • LIFECYCLE_MAX_SENDS_PER_RUN — потолок отправок на серию за прогон; остаток
#     подхватится на следующих запусках → плавный ramp вместо залпа.
LIFECYCLE_SEND_DELAY = 0.1
LIFECYCLE_MAX_SENDS_PER_RUN = 200


def _cycle_key(dt: datetime) -> str:
    """Ключ "цикла" для шага lifecycle-серии — дата-якорь (конец подписки/регистрация),
    делает шаг уникальным именно для ЭТОГО периода, а не навсегда."""
    return dt.strftime('%Y%m%d')


async def _send_lifecycle_message(bot: Bot, user, text: str, reply_markup) -> bool:
    """Отправляет одно lifecycle-касание. True — если отправлено успешно (можно mark_sent)."""
    # Заблокировавшие бота помечены is_active=False — пропускаем.
    if not user.is_active:
        return False
    try:
        await bot.send_message(chat_id=user.user_id, text=text, reply_markup=reply_markup)
        logger.info(f"Lifecycle: касание отправлено user_id={user.user_id}")
        await asyncio.sleep(LIFECYCLE_SEND_DELAY)  # троттлинг против flood-wait
        return True
    except TelegramForbiddenError:
        await user_repo.set_active(user.user_id, False)
        logger.info(f"Lifecycle: user_id={user.user_id} заблокировал бота — помечен неактивным")
        return False
    except Exception as e:
        logger.warning(f"Lifecycle: не удалось отправить касание user_id={user.user_id}: {e}")
        return False


async def _promo_allowed(user_id: int) -> bool:
    """Анти-спам: не более 1 промо-касания в неделю на пользователя (риск бана бота)."""
    last = await lifecycle_repo.get_last_promo_touch_at(user_id)
    return last is None or (datetime.now() - last) >= PROMO_ANTISPAM_WINDOW


async def _ensure_promo_ttl(code: str, hours: int) -> None:
    """Продлевает TTL промокода минимум на `hours` вперёд перед отправкой промо-касания."""
    from tgbot.services import promo_service
    try:
        await promo_service.ensure_min_ttl(code, hours)
    except Exception as e:
        logger.error(f"Lifecycle: не удалось обновить TTL промокода {code}: {e}")


# --- 4.1. §7.2 — напоминания о продлении D-3…D+5 + grace-период 48ч ---

async def lifecycle_renewal_reminders(bot: Bot):
    """
    Анкер серии — user.subscription_end_date ("end"). Шаги D-3/D-1 переиспользуют
    существующие сегментные запросы (get_with_expiring_subscription), пост-экспирационные
    шаги (D0/D+2/D+5) и грейс-очистка используют get_with_subscription_ended_at_least.
    """
    logger.info("Lifecycle: renewal reminders job started.")

    step_counts: dict[str, int] = {}
    sent = 0  # счётчик отправок за прогон (потолок LIFECYCLE_MAX_SENDS_PER_RUN)
    errors = 0

    async def _run() -> None:
        nonlocal sent, errors
        from database import recurring_repo
        # С действующим автопродлением подписка продлится сама: напоминания до истечения и
        # «подписка закончилась» (списание может прийти с задержкой) не шлём.
        autorenew_ids = await recurring_repo.get_active_user_ids()

        # --- D-3 и D-1 (до истечения) ---
        for days_left, step_name in ((3, 'd-3'), (1, 'd-1')):
            users = await user_repo.get_with_expiring_subscription(days_left)
            for user in users:
                if not user.subscription_end_date or user.user_id in autorenew_ids:
                    continue
                step = f"{step_name}_{_cycle_key(user.subscription_end_date)}"
                if await lifecycle_repo.was_sent(user.user_id, 'renewal', step):
                    continue

                if step_name == 'd-3':
                    date_str = user.subscription_end_date.strftime('%d.%m.%Y')
                    text = (
                        f"⏳ Подписка закончится {date_str} — через 3 дня. Продлите заранее, "
                        "чтобы срок подписки оставался непрерывным."
                    )
                    kb = lifecycle_cta_keyboard(("💎 Продлить", "buy_subscription"))
                else:  # d-1
                    text = "Подписка истекает завтра. Выберите тариф для продления в меню."
                    kb = lifecycle_cta_keyboard(("💎 Продлить", "buy_subscription"))


                if await _send_lifecycle_message(bot, user, text, kb):
                    await lifecycle_repo.mark_sent(user.user_id, 'renewal', step)
                    sent += 1
                    step_counts[step_name] = step_counts.get(step_name, 0) + 1
                    if sent >= LIFECYCLE_MAX_SENDS_PER_RUN:
                        logger.info(f"Lifecycle renewal: лимит {LIFECYCLE_MAX_SENDS_PER_RUN}/прогон, остаток — на следующем запуске.")
                        return
                else:
                    errors += 1

        # --- D0 / D+2 (grace) / D+5 (промо-скидка) — по факту истечения ---
        for days_ago, step_name in ((0, 'd0'), (2, 'd2_grace'), (5, 'd5_discount')):
            users = await user_repo.get_with_subscription_ended_at_least(days_ago)
            for user in users:
                if step_name == 'd0' and user.user_id in autorenew_ids:
                    continue
                is_promo = step_name == 'd5_discount'
                step = f"{step_name}_{_cycle_key(user.subscription_end_date)}"
                if is_promo:
                    step = f"promo:{step}"
                    if not await _promo_allowed(user.user_id):
                        continue
                if await lifecycle_repo.was_sent(user.user_id, 'renewal', step):
                    continue

                if step_name == 'd0':
                    text = "Подписка закончилась, конфиг пока сохранён. Вернуть доступ за минуту"
                    kb = lifecycle_cta_keyboard(("💎 Продлить", "buy_subscription"))
                elif step_name == 'd2_grace':
                    text = "Держим ваше место ещё 48 часов, потом конфиг будет удалён."
                    kb = lifecycle_cta_keyboard(("💎 Вернуть доступ", "buy_subscription"))
                else:  # d5_discount
                    await _ensure_promo_ttl('BACK30', hours=72)
                    text = "🎁 −30% на месяц по коду BACK30 — действует 72 часа."
                    kb = lifecycle_cta_keyboard(("🎁 Активировать со скидкой", "apply_promo_BACK30"))

                if await _send_lifecycle_message(bot, user, text, kb):
                    await lifecycle_repo.mark_sent(user.user_id, 'renewal', step)
                    sent += 1
                    step_counts[step_name] = step_counts.get(step_name, 0) + 1
                    if sent >= LIFECYCLE_MAX_SENDS_PER_RUN:
                        logger.info(f"Lifecycle renewal: лимит {LIFECYCLE_MAX_SENDS_PER_RUN}/прогон, остаток — на следующем запуске.")
                        return
                else:
                    errors += 1

        # --- Грейс-очистка: удаляем Remnawave-конфиг спустя ~4 дня простоя подписки
        #     (48ч после предупреждения D+2), но НЕ удаляем пользователя из БД —
        #     при следующей оплате subscription_service.extend() пересоздаст его в Remnawave.
        #     По умолчанию ОТКЛЮЧЕНО (GRACE_DELETE_ENABLED): это необратимое действие,
        #     которого раньше в системе не было.
        if not GRACE_DELETE_ENABLED:
            logger.info("Lifecycle: renewal reminders job finished (grace-delete отключён).")
            return
        users_for_cleanup = await user_repo.get_with_subscription_ended_at_least(4)
        for user in users_for_cleanup:
            step = f"grace_delete_{_cycle_key(user.subscription_end_date)}"
            if await lifecycle_repo.was_sent(user.user_id, 'renewal', step):
                continue
            if not user.remnawave_uuid:
                await lifecycle_repo.mark_sent(user.user_id, 'renewal', step)
                continue
            try:
                from loader import remnawave_client
                await remnawave_client.delete_user(user.remnawave_uuid)
                logger.info(
                    f"Lifecycle grace-cleanup: удалён Remnawave-конфиг user_id={user.user_id} "
                    f"(remnawave_uuid={user.remnawave_uuid}) — 4 дня простоя подписки."
                )
            except Exception as e:
                logger.error(
                    f"Lifecycle grace-cleanup: не удалось удалить Remnawave-пользователя "
                    f"{user.remnawave_uuid}: {e}"
                )
                continue
            await lifecycle_repo.mark_sent(user.user_id, 'renewal', step)

        logger.info("Lifecycle: renewal reminders job finished.")

    try:
        await _run()
    finally:
        # Статистика уходит админам при ЛЮБОМ выходе из _run(), включая ранний
        # return по лимиту LIFECYCLE_MAX_SENDS_PER_RUN — иначе прогон с лимитом
        # молча "терял" бы отчёт.
        if sent > 0:
            breakdown = ", ".join(f"{name}: {count}" for name, count in step_counts.items())
            await notify_admins(
                bot,
                "📨 <b>Lifecycle: напоминания о продлении</b>\n\n"
                f"{breakdown}\n\n"
                f"👍 Всего отправлено: {sent}\n"
                f"👎 Ошибок: {errors}"
            )


# --- 4.2. §7.3 — win-back для ушедших плативших: 3 волны ---

# Волна 1 — спустя 2 недели простоя (после того как renewal-серия §7.2 уже отработала
# и не вернула пользователя). Волна 2/3 — сдвиг относительно предыдущей волны, как
# описано в документе ("+4 дня", "+10 дней"). Точной базы для волны 1 в документе нет —
# выбрана как разумный отступ, см. отчёт.
WINBACK_WAVE1_DAYS = 14
WINBACK_WAVE2_DAYS = WINBACK_WAVE1_DAYS + 4
WINBACK_WAVE3_DAYS = WINBACK_WAVE2_DAYS + 10


async def lifecycle_winback(bot: Bot):
    """Анкер серии — user.subscription_end_date (дата, когда пользователь перестал платить)."""
    logger.info("Lifecycle: win-back job started.")

    step_counts: dict[str, int] = {}
    sent = 0  # счётчик отправок за прогон (потолок LIFECYCLE_MAX_SENDS_PER_RUN)
    errors = 0

    steps = (
        (WINBACK_WAVE1_DAYS, 'wave1'),
        (WINBACK_WAVE2_DAYS, 'wave2'),
        (WINBACK_WAVE3_DAYS, 'wave3'),
    )

    async def _run() -> None:
        nonlocal sent, errors

        for days_ago, step_name in steps:
            users = await user_repo.get_churned_paying_users(days_ago)
            for user in users:
                step = f"promo:{step_name}_{_cycle_key(user.subscription_end_date)}"
                if await lifecycle_repo.was_sent(user.user_id, 'winback', step):
                    continue
                if not await _promo_allowed(user.user_id):
                    continue

                if step_name == 'wave1':
                    text = (
                        "Давно не виделись 👋 За это время мы добавили Польшу и Нидерланды-3, "
                        "ускорили Reality и обновили панель. Возвращайтесь: 🎁 +7 дней "
                        "бесплатно к оплаченному месяцу"
                    )
                    kb = lifecycle_cta_keyboard(("🎁 Забрать 7 дней", "winback_claim7"))
                elif step_name == 'wave2':
                    await _ensure_promo_ttl('COMEBACK', hours=72)
                    text = (
                        "Вернитесь с промокодом COMEBACK. Актуальные тарифы и итоговая цена доступны в меню. "
                        "Код сгорит через 72 часа"
                    )
                    kb = lifecycle_cta_keyboard(("Выбрать тариф с промокодом", "apply_promo_COMEBACK"))
                else:  # wave3
                    text = (
                        "Последнее сообщение — обещаем 🙏 Подскажите, почему ушли? За ответ — "
                        "5 дней подписки в подарок."
                    )
                    kb = winback_survey_keyboard()

                if await _send_lifecycle_message(bot, user, text, kb):
                    await lifecycle_repo.mark_sent(user.user_id, 'winback', step)
                    sent += 1
                    step_counts[step_name] = step_counts.get(step_name, 0) + 1
                    if sent >= LIFECYCLE_MAX_SENDS_PER_RUN:
                        logger.info(f"Lifecycle win-back: лимит {LIFECYCLE_MAX_SENDS_PER_RUN}/прогон, остаток — на следующем запуске.")
                        return
                    # Волна 1 обещает «+7 дней бесплатно» — но НЕ начисляем всем подряд
                    # при отправке: это раздало бы бесплатный VPN всей ушедшей базе (~416
                    # человек) и завысило бы метрику возвратов. Дни получает только тот,
                    # кто нажмёт «Забрать 7 дней» → callback winback_claim7
                    # (tgbot/handlers/user/lifecycle.py).
                else:
                    errors += 1

        logger.info("Lifecycle: win-back job finished.")

    try:
        await _run()
    finally:
        if sent > 0:
            breakdown = ", ".join(f"{name}: {count}" for name, count in step_counts.items())
            await notify_admins(
                bot,
                "📨 <b>Lifecycle: win-back ушедших плативших</b>\n\n"
                f"{breakdown}\n\n"
                f"👍 Всего отправлено: {sent}\n"
                f"👎 Ошибок: {errors}"
            )


# --- 4.3. §7.4 — активация неплативших: дрип 4 касания за 14 дней ---

ACTIVATION_STEPS = (
    (1, 'touch1'),
    (3, 'touch2'),
    (7, 'touch3'),
    (14, 'touch4'),
)


async def lifecycle_activation_drip(bot: Bot):
    """Анкер серии — user.reg_date. Только пользователи без первой оплаты (is_first_payment_made=False)."""
    logger.info("Lifecycle: activation drip job started.")

    step_counts: dict[str, int] = {}
    sent = 0  # счётчик отправок за прогон (потолок LIFECYCLE_MAX_SENDS_PER_RUN)
    errors = 0

    async def _run() -> None:
        nonlocal sent, errors

        for days_ago, step_name in ACTIVATION_STEPS:
            users = await user_repo.get_non_paying_users_registered_at_least(days_ago)
            for user in users:
                is_promo = step_name in ('touch1', 'touch3', 'touch4')
                step = f"promo:{step_name}" if is_promo else step_name
                if await lifecycle_repo.was_sent(user.user_id, 'activation', step):
                    continue
                if is_promo and not await _promo_allowed(user.user_id):
                    continue

                if step_name == 'touch1':
                    text = (
                        "Вы зарегистрировались, но еще не выбрали тариф HamzAVPN. "
                        "Посмотрите срок подписки, объем трафика и лимит устройств. "
                        "Актуальные тарифы и цены — в меню."
                    )
                    kb = lifecycle_cta_keyboard(
                        ("💎 Выбрать тариф", "buy_subscription"),
                        ("🎁 3 дня бесплатно", "start_trial_process"),
                    )
                elif step_name == 'touch2':
                    text = (
                        "HamzAVPN создает защищенное соединение для передачи данных. "
                        "Параметры услуги и цены указаны в тарифах, "
                        "а документы доступны в меню бота."
                    )
                    kb = lifecycle_cta_keyboard(("Выбрать тариф", "buy_subscription"))
                elif step_name == 'touch3':
                    await _ensure_promo_ttl('START20', hours=72)
                    text = "−20% на первый месяц по коду START20. Код живёт 3 дня"
                    kb = lifecycle_cta_keyboard(("Активировать", "apply_promo_START20"))
                else:  # touch4
                    text = "Актуальные тарифы доступны в меню. Подписка оплачивается на выбранный срок без автоматического списания."
                    kb = lifecycle_cta_keyboard(("Взять неделю", "buy_subscription"))

                if await _send_lifecycle_message(bot, user, text, kb):
                    await lifecycle_repo.mark_sent(user.user_id, 'activation', step)
                    sent += 1
                    step_counts[step_name] = step_counts.get(step_name, 0) + 1
                    if sent >= LIFECYCLE_MAX_SENDS_PER_RUN:
                        logger.info(f"Lifecycle activation: лимит {LIFECYCLE_MAX_SENDS_PER_RUN}/прогон, остаток — на следующем запуске.")
                        return
                else:
                    errors += 1

        logger.info("Lifecycle: activation drip job finished.")

    try:
        await _run()
    finally:
        if sent > 0:
            breakdown = ", ".join(f"{name}: {count}" for name, count in step_counts.items())
            await notify_admins(
                bot,
                "📨 <b>Lifecycle: дрип активации неплативших</b>\n\n"
                f"{breakdown}\n\n"
                f"👍 Всего отправлено: {sent}\n"
                f"👎 Ошибок: {errors}"
            )


# --- 4.4. §7.5 — награждение победителя реферального лидерборда за прошлый месяц ---

REFERRAL_LEADERBOARD_AWARD_DAYS = 365

# Минимальное число приглашённых, чтобы победитель получил приз. Защита от геймленга
# дорогого приза (год бесплатно) в «мёртвый» месяц с низким трафиком: пригласить
# один фейк-аккаунт и выиграть год нельзя.
REFERRAL_LEADERBOARD_MIN_INVITED = 3


async def award_referral_leaderboard(bot: Bot):
    """
    Подводит итоги реферального соревнования за ПРОШЕДШИЙ календарный месяц и
    начисляет топ-1 365 дней подписки. Запускается 1-го числа месяца в 09:00 —
    к этому моменту прошлый месяц уже полностью закрыт.

    Идемпотентность: guard через lifecycle_repo (series='referral_leaderboard',
    step=f'award_{YYYYMM прошлого месяца}'), где user_id — сам победитель
    (LifecycleMessage.user_id — FK на users.user_id, поэтому "системного" ID без
    реального пользователя использовать нельзя). Победитель прошедшего месяца
    определяется детерминированно (агрегат по закрытому периоду), поэтому
    привязка guard-записи к его user_id корректно защищает от повторной выдачи
    при повторных прогонах джобы.
    """
    logger.info("Scheduler job: подведение итогов реферального лидерборда за прошлый месяц...")

    now = datetime.now()
    first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_start = (first_of_this_month - timedelta(days=1)).replace(day=1)
    award_month_key = last_month_start.strftime('%Y%m')
    step = f"award_{award_month_key}"

    leaderboard = await stats_repo.get_monthly_referral_leaderboard(
        last_month_start.year, last_month_start.month, limit=1
    )
    if not leaderboard:
        logger.info(f"Referral leaderboard: за {award_month_key} нет участников, награда не выдаётся.")
        return

    winner_id, invited_count = leaderboard[0]

    if invited_count < REFERRAL_LEADERBOARD_MIN_INVITED:
        logger.info(
            f"Referral leaderboard: за {award_month_key} лучший результат {invited_count} "
            f"< порога {REFERRAL_LEADERBOARD_MIN_INVITED}, приз не выдаётся."
        )
        await notify_admins(
            bot,
            f"🏆 <b>Реферальный лидерборд за {last_month_start.strftime('%m.%Y')}</b>\n\n"
            f"Лучший результат: {invited_count} приглашённых — ниже порога "
            f"({REFERRAL_LEADERBOARD_MIN_INVITED}). Приз не выдан."
        )
        return

    if await lifecycle_repo.was_sent(winner_id, 'referral_leaderboard', step):
        logger.info(f"Referral leaderboard: награда за {award_month_key} уже выдана победителю {winner_id}, пропуск.")
        return

    try:
        from tgbot.services import subscription_service
        await subscription_service.extend(winner_id, REFERRAL_LEADERBOARD_AWARD_DAYS, data_limit_gb=None)
    except Exception as e:
        logger.error(f"Referral leaderboard: не удалось начислить награду победителю {winner_id}: {e}")
        return  # не помечаем mark_sent — попробуем снова на следующем прогоне

    await lifecycle_repo.mark_sent(winner_id, 'referral_leaderboard', step)
    logger.info(
        f"Referral leaderboard: победитель {winner_id} за {award_month_key} — "
        f"{invited_count} приглашённых, начислено {REFERRAL_LEADERBOARD_AWARD_DAYS} дней."
    )

    month_label = last_month_start.strftime('%m.%Y')

    # Уведомляем победителя (только Telegram-пользователи, web-пользователи имеют отрицательный ID)
    if winner_id > 0:
        try:
            await bot.send_message(
                winner_id,
                f"🏆 Поздравляем! Вы заняли <b>1-е место</b> в реферальном соревновании за {month_label} — "
                f"пригласили {invited_count} чел.!\n\n"
                f"Вам начислено <b>{REFERRAL_LEADERBOARD_AWARD_DAYS} дней</b> подписки бесплатно! 🎉"
            )
        except Exception as e:
            logger.error(f"Referral leaderboard: не удалось уведомить победителя {winner_id}: {e}")

    # Опционально — публикуем итоги в первый доступный канал (не критично при ошибке)
    try:
        channels = await channel_repo.get_all()
        if channels:
            await bot.send_message(
                channels[0].channel_id,
                f"🏆 Итоги реферального соревнования за {month_label}:\n"
                f"Победитель пригласил {invited_count} новых пользователей и получил год VPN бесплатно!\n\n"
                "Участвуйте и вы — приглашайте друзей по своей ссылке в боте 🎁"
            )
    except Exception as e:
        logger.error(f"Referral leaderboard: не удалось опубликовать итоги в канал: {e}")

    await notify_admins(
        bot,
        f"🏆 <b>Реферальный лидерборд за {month_label} подведён</b>\n\n"
        f"👤 Победитель: <code>{winner_id}</code>\n"
        f"👥 Приглашено: {invited_count}\n"
        f"🎁 Начислено: {REFERRAL_LEADERBOARD_AWARD_DAYS} дней подписки"
    )

    logger.info("Scheduler job: подведение итогов реферального лидерборда завершено.")


# =============================================================================
# --- 4.5 СВЕРКА ЛИМИТОВ УСТРОЙСТВ ---
#
# Джоб закрывает два разрыва, которые иначе работают против сервиса:
#
#  1. Панель могла не принять PATCH в момент оплаты (лежала, таймаут прокси) —
#     деньги списаны, слот не выдан. sync_limit идемпотентен, поэтому просто
#     доводит лимит до состояния БД при следующем прогоне.
#
#  2. Remnawave проверяет лимит ТОЛЬКО при регистрации нового hwid: уже
#     записанные устройства продолжают работать после снижения лимита. Без
#     чистки «купил 5 слотов на месяц, привязал 10 устройств, перестал платить»
#     оставалось бы рабочей схемой навсегда.
#
# Ходит в панель по одному пользователю на каждого владельца слотов, поэтому
# запускается раз в час, а не каждые пять минут.
# =============================================================================

DEVICE_SLOTS_SERIES = 'device_slots'


async def sync_device_limits(bot: Bot):
    """Сверяет лимиты устройств с панелью, гасит истёкшие слоты и предупреждает владельцев."""
    from tgbot.services import device_slot_service
    from tgbot.services.device_pricing import days_left

    logger.info("Scheduler job: сверка лимитов устройств запущена.")

    try:
        users = await user_repo.get_with_extra_devices()
    except Exception as e:
        logger.error(f"Сверка лимитов устройств: не удалось получить список пользователей: {e}")
        return

    expired_count = 0
    synced_count = 0
    removed_total = 0
    warned_count = 0
    failed_count = 0

    for user in users:
        left = days_left(user.subscription_end_date)

        try:
            if left <= 0:
                result = await device_slot_service.expire_slots(user.user_id)
                expired_count += 1
                if result and result.removed:
                    removed_total += result.removed
                    await _notify_slots_expired(bot, user, result.removed)
                else:
                    await _notify_slots_expired(bot, user, 0)
                continue

            result = await device_slot_service.sync_limit(user.user_id)
            if result is None:
                failed_count += 1
                continue

            synced_count += 1
            if result.removed:
                removed_total += result.removed

            # Предупреждение за сутки — чтобы отключение устройств не стало
            # сюрпризом. Трекер касаний не даёт повторить его каждый час.
            if left <= 1:
                step = f"expiry_{_cycle_key(user.subscription_end_date)}"
                if not await lifecycle_repo.was_sent(user.user_id, DEVICE_SLOTS_SERIES, step):
                    if await _notify_slots_expiring(bot, user):
                        await lifecycle_repo.mark_sent(user.user_id, DEVICE_SLOTS_SERIES, step)
                        warned_count += 1
        except Exception as e:
            failed_count += 1
            logger.error(f"Сверка лимитов устройств: ошибка для user_id={user.user_id}: {e}")

    logger.info(
        f"Сверка лимитов устройств завершена: синхронизировано={synced_count}, "
        f"слоты сгорели={expired_count}, отключено устройств={removed_total}, "
        f"предупреждено={warned_count}, ошибок={failed_count}."
    )


async def _notify_slots_expiring(bot: Bot, user) -> bool:
    """Предупреждение за сутки: продлите подписку, иначе лишние устройства отключатся."""
    if not user.is_active or user.user_id < 0:
        return False

    slots = user.extra_devices or 0
    text = (
        "⏳ <b>Завтра истекают дополнительные устройства</b>\n\n"
        f"Вместе с подпиской заканчиваются оплаченные слоты "
        f"(+{slots} {decline_word(slots, ['устройство', 'устройства', 'устройств'])}).\n\n"
        "Продлите подписку, чтобы сохранить их — иначе лишние устройства "
        "будут отключены автоматически, начиная с самых давно неактивных."
    )
    return await send_reminder(bot, user, text)


async def _notify_slots_expired(bot: Bot, user, removed: int) -> None:
    """Слоты сгорели вместе с подпиской."""
    if not user.is_active or user.user_id < 0:
        return

    text = "📱 <b>Дополнительные устройства отключены</b>\n\nПодписка закончилась, "
    if removed:
        word = decline_word(removed, ['устройство', 'устройства', 'устройств'])
        text += f"поэтому отключено {removed} {word} — остались самые активные.\n\n"
    else:
        text += "оплаченные слоты больше не действуют.\n\n"
    text += "Продлите подписку и докупите устройства снова в любой момент:"

    try:
        await send_reminder(bot, user, text)
    except Exception as e:
        logger.error(f"Не удалось уведомить user_id={user.user_id} об отключении устройств: {e}")


# --- 4.6 АВТООТМЕНА ЗАВИСШИХ НЕОПЛАЧЕННЫХ СЧЕТОВ ---

# Локальный таймаут неоплаченного счёта, минуты.
#
# Локальное закрытие счета освобождает checkout. Подтвержденная позже
# транзакция все равно зачисляется; фактический срок оплаты задает Platega.
PENDING_PAYMENT_TTL_MINUTES = 60


async def cancel_stale_payments():
    """Помечает 'cancelled' счета, провисевшие в 'pending' дольше таймаута."""
    # Ленивый импорт — избегаем циклической зависимости на этапе загрузки модулей.
    from tgbot.services import payment_service

    logger.info("Scheduler job: автоотмена зависших счетов запущена.")

    try:
        cancelled = await payment_service.cancel_stale_payments(PENDING_PAYMENT_TTL_MINUTES)
    except Exception as e:
        logger.error(f"Автоотмена зависших счетов: прогон не удался: {e}")
        return

    if cancelled:
        logger.info(
            f"Автоотмена зависших счетов завершена: отменено {len(cancelled)} "
            f"(старше {PENDING_PAYMENT_TTL_MINUTES} мин)."
        )
    else:
        logger.info("Автоотмена зависших счетов завершена: зависших счетов нет.")


async def reconcile_payments():
    from tgbot.services import payment_service
    try:
        await payment_service.reconcile()
    except Exception:
        logger.exception('Сверка платежей отложена')


# --- 5. Функция для добавления всех задач в планировщик ---

# --- 4.7 СВЕРКА АВТОПРОДЛЕНИЯ (рекуррентные подписки Platega) ---

async def reconcile_recurring(bot: Bot):
    """Брошенные привязки счёта, внешние отмены, потерянные callback'и списаний."""
    from tgbot.services import recurring_service
    from tgbot.handlers.recurring_webhook import notify_status_change

    try:
        changes = await recurring_service.reconcile()
    except Exception:
        logger.exception("Не удалось выполнить сверку автопродления")
        return
    for sub, old, new in changes:
        try:
            await notify_status_change(bot, sub, old, new)
        except Exception:
            logger.exception("Не удалось уведомить об изменении автопродления user=%s", sub.user_id)


def schedule_jobs(scheduler: AsyncIOScheduler, bot: Bot):
    """
    Добавляет все фоновые задачи в планировщик.
    Вызывается один раз при старте бота.
    """
    scheduler.add_job(reconcile_payments, trigger='interval', minutes=5,
                      max_instances=1, coalesce=True)

    # Запускать проверку подписок каждый день в 12:49 по МСК
    scheduler.add_job(
        check_subscriptions,
        trigger='cron',
        hour=12,
        minute=49,
        kwargs={'bot': bot}
    )

    # --- Lifecycle-мессенджинг: отдельное время, чтобы не конкурировать с 12:00/12:49 ---
    scheduler.add_job(
        lifecycle_renewal_reminders,
        trigger='cron',
        hour=10,
        minute=0,
        kwargs={'bot': bot}
    )
    scheduler.add_job(
        lifecycle_winback,
        trigger='cron',
        hour=10,
        minute=15,
        kwargs={'bot': bot}
    )
    scheduler.add_job(
        lifecycle_activation_drip,
        trigger='cron',
        hour=10,
        minute=30,
        kwargs={'bot': bot}
    )

    # Награждение победителя реферального лидерборда — 1-го числа месяца в 09:00,
    # ДО остальных job'ов дня (§7.5).
    scheduler.add_job(
        award_referral_leaderboard,
        trigger='cron',
        day=1,
        hour=9,
        minute=0,
        kwargs={'bot': bot}
    )

    # Сверка автопродления с Platega: незавершённые привязки (30 мин на банк) и
    # активные подписки раз в несколько часов.
    scheduler.add_job(reconcile_recurring, trigger='interval', minutes=10,
                      kwargs={'bot': bot}, max_instances=1, coalesce=True)

    # Сверка лимитов устройств — каждый час: страхует неприменённый после оплаты
    # лимит и гасит слоты, пережившие свою подписку.
    scheduler.add_job(
        sync_device_limits,
        trigger='cron',
        minute=20,
        kwargs={'bot': bot}
    )

    # Автоотмена зависших счетов — каждые 15 минут. Задача чисто служебная
    # (пометка в БД), пользователю и админам писать не о чем, поэтому bot не нужен.
    scheduler.add_job(
        cancel_stale_payments,
        trigger='cron',
        minute='*/15',
    )

    logger.info(
        "Scheduler jobs added: reconcile_payments (каждые 5 мин), check_subscriptions (12:49), "
        "lifecycle_renewal_reminders (10:00), lifecycle_winback (10:15), "
        "lifecycle_activation_drip (10:30), award_referral_leaderboard (1st day 09:00), "
        "sync_device_limits (каждый час в :20), cancel_stale_payments (каждые 15 мин)."
    )
