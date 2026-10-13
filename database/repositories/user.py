from datetime import datetime, timedelta

from sqlalchemy import select, update, delete, func

from db import User, UsedPromoCode


class UserRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def get_or_create(self, user_id: int, full_name: str, username: str | None = None) -> tuple[User, bool]:
        async with self._session_maker() as session:
            stmt = select(User).where(User.user_id == user_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()
            if user:
                # Пользователь снова пишет боту → значит он его не блокировал
                # (или уже разблокировал). Снимаем флаг неактивности, чтобы
                # рассылки снова доходили. expire_on_commit=False — объект
                # остаётся пригодным к использованию после commit. См. set_active.
                if not user.is_active:
                    user.is_active = True
                    await session.commit()
                return user, False

            user = User(user_id=user_id, full_name=full_name, username=username)
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return user, True

    async def set_active(self, user_id: int, active: bool) -> None:
        """Помечает пользователя активным/неактивным (заблокировал ли он бота).

        Неактивных пропускают джобы рассылок (send_reminder /
        _send_lifecycle_message), чтобы не долбить Telegram и не засорять логи
        тысячами `Forbidden: bot was blocked by the user`. Возврат в активные —
        через get_or_create при следующем обращении пользователя к боту.
        """
        async with self._session_maker() as session:
            await session.execute(
                update(User).where(User.user_id == user_id).values(is_active=active)
            )
            await session.commit()

    async def get(self, user_id: int) -> User | None:
        async with self._session_maker() as session:
            return await session.get(User, user_id)

    async def get_by_username(self, username: str) -> User | None:
        async with self._session_maker() as session:
            stmt = select(User).where(func.lower(User.username) == username.lower())
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_all_ids(self) -> list[int]:
        async with self._session_maker() as session:
            stmt = select(User.user_id)
            result = await session.execute(stmt)
            return result.scalars().all()

    async def update_vpn_username(self, user_id: int, vpn_username: str):
        async with self._session_maker() as session:
            stmt = update(User).where(User.user_id == user_id).values(vpn_username=vpn_username)
            await session.execute(stmt)
            await session.commit()

    async def update_remnawave_uuid(self, user_id: int, uuid: str):
        async with self._session_maker() as session:
            stmt = update(User).where(User.user_id == user_id).values(remnawave_uuid=uuid)
            await session.execute(stmt)
            await session.commit()

    async def extend_subscription(self, user_id: int, days: int):
        async with self._session_maker() as session:
            user = await session.get(User, user_id)
            if not user:
                return
            now = datetime.now()
            new_date = (user.subscription_end_date if user.subscription_end_date and user.subscription_end_date > now else now) + timedelta(days=days)
            user.subscription_end_date = new_date
            await session.commit()

    async def set_referrer(self, user_id: int, referrer_id: int):
        async with self._session_maker() as session:
            stmt = update(User).where(User.user_id == user_id).values(referrer_id=referrer_id)
            await session.execute(stmt)
            await session.commit()

    async def add_bonus_days(self, user_id: int, days: int):
        async with self._session_maker() as session:
            user = await session.get(User, user_id)
            if user:
                user.referral_bonus_days = (user.referral_bonus_days or 0) + days
                await session.commit()

    async def set_first_payment_done(self, user_id: int):
        async with self._session_maker() as session:
            stmt = update(User).where(User.user_id == user_id).values(is_first_payment_made=True)
            await session.execute(stmt)
            await session.commit()

    async def set_extra_devices(self, user_id: int, count: int) -> None:
        """Устанавливает количество оплаченных доп. слотов устройств (абсолютное значение)."""
        async with self._session_maker() as session:
            stmt = update(User).where(User.user_id == user_id).values(extra_devices=max(0, count))
            await session.execute(stmt)
            await session.commit()

    async def add_extra_devices(self, user_id: int, count: int) -> int:
        """
        Докупка слотов: прибавляет к текущему значению и возвращает новое.

        Считается в одной транзакции по строке пользователя, а не read-modify-write
        в сервисе: вебхук Platega может прийти параллельно с другим платежом.
        """
        async with self._session_maker() as session:
            stmt = (
                update(User)
                .where(User.user_id == user_id)
                .values(extra_devices=func.greatest(User.extra_devices + count, 0))
                .returning(User.extra_devices)
            )
            result = await session.execute(stmt)
            new_value = result.scalar_one_or_none()
            await session.commit()
            return new_value or 0

    async def get_with_extra_devices(self) -> list[User]:
        """Все, у кого есть оплаченные доп. слоты — для джоба сверки лимитов."""
        async with self._session_maker() as session:
            stmt = select(User).where(User.extra_devices > 0)
            result = await session.execute(stmt)
            return result.scalars().all()

    async def delete(self, user_id: int) -> bool:
        async with self._session_maker() as session:
            user = await session.get(User, user_id)
            if not user:
                return False
            await session.execute(
                delete(UsedPromoCode).where(UsedPromoCode.user_id == user_id)
            )
            await session.delete(user)
            await session.commit()
            return True

    async def get_with_expiring_subscription(self, days_left: int) -> list[User]:
        async with self._session_maker() as session:
            target_date_start = datetime.now().date() + timedelta(days=days_left)
            target_date_end = target_date_start + timedelta(days=1)
            stmt = select(User).where(
                User.subscription_end_date >= target_date_start,
                User.subscription_end_date < target_date_end
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_with_expiring_subscription_in_hours(self, hours: int) -> list[User]:
        async with self._session_maker() as session:
            now = datetime.now()
            expiration_limit = now + timedelta(hours=hours)
            stmt = select(User).where(
                User.subscription_end_date > now,
                User.subscription_end_date <= expiration_limit
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def set_trial_received(self, user_id: int):
        async with self._session_maker() as session:
            stmt = (
                update(User)
                .where(User.user_id == user_id)
                .values(has_received_trial=True)
            )
            await session.execute(stmt)
            await session.commit()

    async def set_support_topic(self, user_id: int, topic_id: int):
        async with self._session_maker() as session:
            stmt = update(User).where(User.user_id == user_id).values(support_topic_id=topic_id)
            await session.execute(stmt)
            await session.commit()

    async def clear_support_topic(self, user_id: int):
        async with self._session_maker() as session:
            stmt = update(User).where(User.user_id == user_id).values(support_topic_id=None)
            await session.execute(stmt)
            await session.commit()

    async def get_by_support_topic(self, topic_id: int) -> User | None:
        async with self._session_maker() as session:
            stmt = select(User).where(User.support_topic_id == topic_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_without_first_payment(self) -> list[int]:
        async with self._session_maker() as session:
            stmt = select(User.user_id).where(User.is_first_payment_made == False)
            result = await session.execute(stmt)
            return [user_id for (user_id,) in result.all()]

    # --- Сегментные запросы для lifecycle-рассылок (напоминания/win-back/активация) ---

    async def get_with_subscription_ended_at_least(self, days_ago: int) -> list[User]:
        """
        Пользователи, чья подписка закончилась минимум `days_ago` дней назад (порог
        открыт в прошлое, без верхней границы). Используется для пост-экспирационных
        шагов renewal-серии (D0 / D+2 grace / D+5 discount) и для грейс-очистки.

        Открытая верхняя граница — намеренно: идемпотентность конкретного шага
        обеспечивает LifecycleRepository.was_sent, а не диапазон дат. Это позволяет
        «догнать» пользователей, пропущенных из-за простоя планировщика — шаг всё
        равно отправится один раз, просто позже.
        """
        async with self._session_maker() as session:
            threshold = datetime.now() - timedelta(days=days_ago)
            stmt = select(User).where(
                User.subscription_end_date.is_not(None),
                User.subscription_end_date <= threshold,
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_churned_paying_users(self, days_ago: int) -> list[User]:
        """
        «Ушедшие плативившие» — сегмент для win-back (§7.3): хотя бы раз оплачивали
        подписку, и она закончилась минимум `days_ago` дней назад.
        """
        async with self._session_maker() as session:
            threshold = datetime.now() - timedelta(days=days_ago)
            stmt = select(User).where(
                User.is_first_payment_made == True,
                User.subscription_end_date.is_not(None),
                User.subscription_end_date <= threshold,
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_non_paying_users_registered_at_least(self, days_ago: int) -> list[User]:
        """
        «Неплатившие для дрипа активации» — сегмент для §7.4: ни разу не оплачивали,
        зарегистрировались минимум `days_ago` дней назад.

        Отсекаем тех, кому дрип физически не доставить, прямо в запросе:
          • is_active=False — заблокировали бота (mark_sent им не проставляется,
            поэтому без фильтра они перебирались на КАЖДОМ прогоне всех 4 шагов и
            раздували счётчик `errors` в отчёте админам до тысяч);
          • user_id < 0 — регистрация через веб-дашборд, Telegram-чата нет вообще
            (send_message возвращает "Bad Request: chat not found").
        Только для lifecycle-рассылки: в грейс-очистке и транзакционных джобах
        заблокировавшие бота нужны и там этот фильтр НЕ применяется.
        """
        async with self._session_maker() as session:
            threshold = datetime.now() - timedelta(days=days_ago)
            stmt = select(User).where(
                User.is_first_payment_made == False,
                User.reg_date <= threshold,
                User.is_active == True,
                User.user_id > 0,
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_by_email(self, email: str) -> User | None:
        async with self._session_maker() as session:
            stmt = select(User).where(User.email == email)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def set_email_and_password(self, user_id: int, email: str, password_hash: str):
        async with self._session_maker() as session:
            stmt = (
                update(User)
                .where(User.user_id == user_id)
                .values(email=email, password_hash=password_hash, is_email_verified=True)
            )
            await session.execute(stmt)
            await session.commit()

    async def set_verification_code(self, user_id: int, code: str, expire: datetime):
        async with self._session_maker() as session:
            stmt = (
                update(User)
                .where(User.user_id == user_id)
                .values(verification_code=code, verification_code_expire=expire)
            )
            await session.execute(stmt)
            await session.commit()

    async def clear_verification_code(self, user_id: int):
        async with self._session_maker() as session:
            stmt = (
                update(User)
                .where(User.user_id == user_id)
                .values(verification_code=None, verification_code_expire=None)
            )
            await session.execute(stmt)
            await session.commit()

    async def mark_email_verified(self, user_id: int):
        async with self._session_maker() as session:
            stmt = (
                update(User)
                .where(User.user_id == user_id)
                .values(is_email_verified=True, verification_code=None, verification_code_expire=None)
            )
            await session.execute(stmt)
            await session.commit()

    async def set_traffic_quota(self, user_id: int, quota_gb: int) -> None:
        """Записывает квоту подписки (ГБ/мес, 0 = безлимит)."""
        async with self._session_maker() as session:
            stmt = update(User).where(User.user_id == user_id, User.subscription_data_limit_gb.is_(None)).values(subscription_data_limit_gb=max(0, quota_gb))
            await session.execute(stmt)
            await session.commit()

    async def get_with_extra_traffic(self) -> list[User]:
        """Все, у кого есть докупленный трафик — для джоба сверки лимитов."""
        async with self._session_maker() as session:
            stmt = select(User).where(User.extra_traffic_gb > 0)
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_active_telegram_subscribers(self) -> list[User]:
        """Telegram-пользователи с живой подпиской и VPN-аккаунтом — адресаты
        предупреждений об исчерпании трафика."""
        async with self._session_maker() as session:
            stmt = select(User).where(
                User.user_id > 0,
                User.is_first_payment_made.is_(True),
                User.is_active == True,
                User.vpn_username.is_not(None),
                User.subscription_end_date.is_not(None),
                User.subscription_end_date > datetime.now(),
            )
            result = await session.execute(stmt)
            return result.scalars().all()
