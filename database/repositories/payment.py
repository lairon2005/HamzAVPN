from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update, func, case, text
from contextlib import asynccontextmanager
import asyncio

from db import Payment, Tariff, User
from sqlalchemy.exc import IntegrityError

_MSK = ZoneInfo("Europe/Moscow")
_checkout_locks = {}


def _msk_period_start(kind: str, now_utc: datetime | None = None) -> datetime:
    """Начало календарного периода в МСК, приведённое к наивному UTC —
    в той же конвенции, в какой хранится Payment.completed_at (наивный UTC).

    kind: 'day' | 'week' (с понедельника) | 'month' (с 1-го числа) | 'year' (с 1 января).
    now_utc: aware-datetime в UTC; для тестов. По умолчанию datetime.now(timezone.utc).
    Границы периода вычисляются по московским календарным суткам, затем
    переводятся обратно в наивный UTC для сравнения с completed_at.
    """
    base = now_utc or datetime.now(timezone.utc)
    now_msk = base.astimezone(_MSK)
    if kind == "day":
        start = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    elif kind == "week":
        start = (now_msk - timedelta(days=now_msk.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
    elif kind == "month":
        start = now_msk.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif kind == "year":
        start = now_msk.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unknown period kind: {kind}")
    # aware(MSK) -> aware(UTC) -> наивный UTC (как хранится completed_at)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


class PaymentRepository:
    def __init__(self, session_maker):
        self._session_maker = session_maker

    async def create(self, provider_payment_id: str, user_id: int, tariff_id: int | None,
                     original_amount: float, final_amount: float, source: str = 'bot',
                     promo_code: str = None, discount_percent: int = 0,
                     telegram_payment_charge_id: str | None = None,
                     kind: str = 'subscription', extra_devices: int = 0,
                     payment_url: str | None = None, currency: str = 'RUB') -> Payment:
        async with self._session_maker() as session:
            tariff = await session.get(Tariff, tariff_id) if tariff_id else None
            if kind == 'subscription' and tariff is None:
                raise ValueError('Тариф не найден')
            payment = Payment(
                provider_payment_id=provider_payment_id,
                order_id=provider_payment_id[6:] if provider_payment_id.startswith('order:') else None,
                user_id=user_id,
                tariff_id=tariff_id,
                original_amount=original_amount,
                final_amount=final_amount,
                source=source,
                promo_code=promo_code,
                discount_percent=discount_percent,
                telegram_payment_charge_id=telegram_payment_charge_id,
                kind=kind,
                extra_devices=extra_devices,
                payment_url=payment_url, currency=currency,
                duration_days=tariff.duration_days if tariff else 0,
                data_limit_gb=(tariff.data_limit_gb or 0) if tariff else None,
            )
            session.add(payment)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(select(Payment).where(
                    Payment.provider_payment_id == provider_payment_id))
                if existing is None:
                    raise
                if (existing.user_id != user_id or existing.final_amount != final_amount
                        or existing.tariff_id != tariff_id or existing.currency != currency):
                    raise ValueError('Конфликт идентификатора платежа')
                return existing
            await session.refresh(payment)
            return payment

    @asynccontextmanager
    async def checkout_lock(self, user_id: int):
        """Общая блокировка checkout бота и сайта, в том числе между процессами."""
        async with self._session_maker() as session, session.begin():
            if session.bind.dialect.name == 'postgresql':
                await session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': user_id})
                yield
            else:
                lock = _checkout_locks.setdefault(user_id, asyncio.Lock())
                async with lock:
                    yield

    async def bind_transaction(self, reservation: str, identifier: str, payment_url=None):
        async with self._session_maker() as session, session.begin():
            existing = await session.scalar(select(Payment).where(
                Payment.provider_payment_id == identifier))
            if existing is not None:
                if existing.order_id != reservation[6:]:
                    raise ValueError('Транзакция принадлежит другому заказу')
                return existing
            order = await session.scalar(select(Payment).where(
                Payment.provider_payment_id == reservation).with_for_update())
            if order is None:
                raise LookupError('Резерв заказа не найден')
            order.provider_payment_id = identifier
            if payment_url:
                order.payment_url = payment_url
            return order

    async def get_by_provider_id(self, provider_payment_id: str) -> Payment | None:
        async with self._session_maker() as session:
            stmt = select(Payment).where(Payment.provider_payment_id == provider_payment_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_stars_invoice(self, order_id: str):
        async with self._session_maker() as session:
            return await session.scalar(select(Payment).where(
                Payment.order_id == order_id, Payment.source == 'stars', Payment.currency == 'XTR'))

    async def bind_stars_charge(self, order_id: str, charge_id: str, user_id: int, amount, tariff_id: int):
        from platega.client import money
        async with self._session_maker() as session, session.begin():
            payment = await session.scalar(select(Payment).where(
                Payment.order_id == order_id, Payment.source == 'stars').with_for_update())
            if (payment is None or payment.user_id != user_id or payment.tariff_id != tariff_id
                    or money(payment.final_amount) != money(amount)):
                raise ValueError('Счет Stars не совпадает с оплаченной транзакцией')
            if payment.telegram_payment_charge_id and payment.telegram_payment_charge_id != charge_id:
                raise ValueError('Счет Stars уже оплачен другой транзакцией')
            payment.telegram_payment_charge_id = charge_id
            payment.provider_payment_id = f'stars:{charge_id}'
            return payment

    async def get_by_telegram_charge_id(self, telegram_payment_charge_id: str) -> Payment | None:
        """Идемпотентность Stars-платежей (README.md: оплата Telegram Stars): Telegram может
        повторно доставить Message.successful_payment (ретрай доставки апдейта) — по
        этому charge_id проверяем, не зачислили ли мы его уже."""
        async with self._session_maker() as session:
            stmt = select(Payment).where(
                Payment.telegram_payment_charge_id == telegram_payment_charge_id
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def update_status(self, provider_payment_id: str, status: str) -> bool:
        async with self._session_maker() as session:
            completed_at = datetime.now() if status in ('succeeded', 'failed', 'refunded') else None
            stmt = (
                update(Payment)
                .where(Payment.provider_payment_id == provider_payment_id)
                .values(status=status, completed_at=completed_at)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def get_user_payments(self, user_id: int, limit: int = 20) -> list[Payment]:
        async with self._session_maker() as session:
            stmt = (
                select(Payment)
                .where(Payment.user_id == user_id)
                .order_by(Payment.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_user_pending(self, user_id: int) -> Payment | None:
        async with self._session_maker() as session:
            stmt = select(Payment).where(
                Payment.user_id == user_id,
                Payment.status == 'pending', Payment.source != 'stars'
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_pending_older_than(self, minutes: int) -> list[Payment]:
        async with self._session_maker() as session:
            cutoff = datetime.now() - timedelta(minutes=minutes)
            stmt = select(Payment).where(
                Payment.status == 'pending', Payment.source != 'stars',
                Payment.created_at < cutoff
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def cancel_if_pending(self, provider_payment_id: str) -> bool:
        """Атомарно помечает счёт 'cancelled' — но только если он ВСЁ ЕЩЁ 'pending'.

        Нужно джобу автоотмены (tgbot/services/scheduler.py): между выборкой
        зависших счетов и их отметкой вебхук мог принести оплату по старой
        ссылке, и безусловный update_status затёр бы 'succeeded' вместе с
        completed_at — платёж пропал бы из выручки. Условие в WHERE закрывает
        это окно на стороне БД.

        Возвращает False, если строка уже сменила статус (и значит трогать её
        не надо) либо её нет вовсе.
        """
        async with self._session_maker() as session:
            stmt = (
                update(Payment)
                .where(
                    Payment.provider_payment_id == provider_payment_id,
                    Payment.status == 'pending',
                )
                .values(status='cancelled', completed_at=None)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def get_revenue_stats(self, days: int) -> dict:
        async with self._session_maker() as session:
            since = datetime.now() - timedelta(days=days)
            stmt = select(
                func.count(Payment.id),
                func.coalesce(func.sum(Payment.final_amount), 0)
            ).where(
                Payment.status == 'succeeded',
                Payment.completed_at >= since,
                # Stars-платежи (README.md: оплата Telegram Stars) хранят сумму в XTR,
                # а не в рублях — не смешиваем их с рублёвым доходом (см. get_stars_revenue_total).
                Payment.source != 'stars',
            )
            result = await session.execute(stmt)
            row = result.one()
            return {"count": row[0], "revenue": float(row[1])}

    async def get_revenue_for_period(self, kind: str) -> dict:
        """Доход за КАЛЕНДАРНЫЙ период (kind: 'day'|'week'|'month'|'year'),
        границы считаются по МСК. Возвращает {"count", "revenue", "since"}.
        Stars-платежи исключены (сумма в XTR, не в рублях) — см. get_stars_revenue_total."""
        async with self._session_maker() as session:
            since = _msk_period_start(kind)
            stmt = select(
                func.count(Payment.id),
                func.coalesce(func.sum(Payment.final_amount), 0)
            ).where(
                Payment.status == 'succeeded',
                Payment.completed_at >= since,
                Payment.source != 'stars',
            )
            result = await session.execute(stmt)
            row = result.one()
            return {"count": row[0], "revenue": float(row[1]), "since": since}

    async def get_total_revenue(self) -> dict:
        async with self._session_maker() as session:
            stmt = select(
                func.count(Payment.id),
                func.coalesce(func.sum(Payment.final_amount), 0)
            ).where(Payment.status == 'succeeded', Payment.source != 'stars')
            result = await session.execute(stmt)
            row = result.one()
            return {"count": row[0], "revenue": float(row[1])}

    async def get_stars_revenue_total(self) -> dict:
        """Доход Stars (XTR) за всё время — отдельно от рублёвого дохода
        (README.md: оплата Telegram Stars, п.7). final_amount для source='stars'
        хранит сумму в XTR (столько же, сколько price_stars у тарифа на момент оплаты)."""
        async with self._session_maker() as session:
            stmt = select(
                func.count(Payment.id),
                func.coalesce(func.sum(Payment.final_amount), 0)
            ).where(Payment.status == 'succeeded', Payment.source == 'stars')
            result = await session.execute(stmt)
            row = result.one()
            return {"count": row[0], "stars": float(row[1])}

    async def get_mrr(self) -> dict:
        """
        Метрика №1 — MRR: сумма успешных платежей за ТЕКУЩИЙ календарный месяц
        (с 1-го числа месяца по текущий момент), а не скользящее окно в днях.
        Stars-платежи исключены (сумма в XTR, не в рублях).
        """
        async with self._session_maker() as session:
            month_start = _msk_period_start("month")
            stmt = select(
                func.count(Payment.id),
                func.coalesce(func.sum(Payment.final_amount), 0)
            ).where(
                Payment.status == 'succeeded',
                Payment.completed_at >= month_start,
                Payment.source != 'stars',
            )
            result = await session.execute(stmt)
            row = result.one()
            return {"count": row[0], "revenue": float(row[1]), "month_start": month_start}

    async def count_first_payments_for_period(self, days: int) -> int:
        """
        Метрика №3 — «первые оплаты»: количество НОВЫХ платящих пользователей
        за период, т.е. пользователей, чей самый первый успешный платёж за всю
        историю пришёлся именно на этот период (а не просто любой их платёж).
        """
        async with self._session_maker() as session:
            since = datetime.now() - timedelta(days=days)
            first_payment_subq = (
                select(
                    Payment.user_id.label("user_id"),
                    func.min(Payment.completed_at).label("first_completed_at"),
                )
                .where(Payment.status == "succeeded", Payment.completed_at.is_not(None))
                .group_by(Payment.user_id)
                .subquery()
            )
            stmt = select(func.count()).select_from(first_payment_subq).where(
                first_payment_subq.c.first_completed_at >= since
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def get_long_tariff_share(self, days: int, min_duration_days: int = 90) -> dict:
        """
        Метрика №6 — доля платежей за «длинные» тарифы (duration_days >= 90)
        среди всех успешных платежей за период.
        """
        async with self._session_maker() as session:
            since = datetime.now() - timedelta(days=days)
            stmt = (
                select(
                    func.count(Payment.id),
                    func.coalesce(
                        func.sum(case((Tariff.duration_days >= min_duration_days, 1), else_=0)),
                        0,
                    ),
                )
                .select_from(Payment)
                .join(Tariff, Tariff.id == Payment.tariff_id)
                .where(
                    Payment.status == 'succeeded',
                    Payment.completed_at >= since,
                    # Докупка слотов устройств идёт без тарифа и метрику «длинных
                    # тарифов» разбавлять не должна (JOIN её и так отсекает —
                    # условие оставлено явным, чтобы смысл не терялся).
                    Payment.kind == 'subscription',
                )
            )
            result = await session.execute(stmt)
            row = result.one()
            total = row[0] or 0
            long_count = row[1] or 0
            share = (long_count / total) if total else 0.0
            return {"total": total, "long": long_count, "share": share}

    async def apply_confirmed(self, identifier: str, amount, currency: str,
                              max_extra: int, referral_days: int = 7):
        """Зачисление и маркер успеха коммитятся вместе. Повторы ничего не добавляют."""
        from platega.client import money
        async with self._session_maker() as session, session.begin():
            payment = await session.scalar(select(Payment).where(
                Payment.provider_payment_id == identifier).with_for_update())
            if payment is None:
                raise LookupError('Платеж не найден')
            if money(amount) != money(payment.final_amount) or currency != payment.currency:
                raise ValueError('Сумма или валюта платежа не совпадает')
            if payment.status not in ('pending', 'cancelled'):
                return None
            payer = await session.get(User, payment.user_id)
            if payer is None:
                raise LookupError('Пользователь не найден')
            # Единый порядок блокировки предотвращает deadlock взаимных рефералов.
            ids = sorted({payment.user_id, payer.referrer_id} - {None})
            users = (await session.scalars(select(User).where(User.user_id.in_(ids))
                                          .order_by(User.user_id).with_for_update()
                                          .execution_options(populate_existing=True))).all()
            by_id = {u.user_id: u for u in users}
            payer = by_id[payment.user_id]
            now = datetime.now()
            first = not payer.is_first_payment_made
            is_new = not payer.remnawave_uuid
            referrer_id = None
            if payment.kind == 'devices':
                payer.extra_devices = min(max_extra, (payer.extra_devices or 0) + payment.extra_devices)
            else:
                payer.subscription_end_date = max(payer.subscription_end_date or now, now) + timedelta(days=payment.duration_days)
                payer.subscription_data_limit_gb = payment.data_limit_gb
                payer.extra_devices = min(max_extra, max(0, payment.extra_devices))
                if first:
                    referrer = by_id.get(payer.referrer_id)
                    if referrer is not None and referrer.user_id != payer.user_id:
                        referrer.subscription_end_date = max(referrer.subscription_end_date or now, now) + timedelta(days=referral_days)
                        referrer.referral_bonus_days = (referrer.referral_bonus_days or 0) + referral_days
                        referrer_id = referrer.user_id
                    payer.is_first_payment_made = True
            payment.referrer_id = referrer_id
            payment.status = 'succeeded'
            payment.completed_at = now
            payment.sync_pending = True
            return {'payment': payment, 'first': first, 'is_new': is_new,
                    'referrer_id': referrer_id, 'extra_devices': payer.extra_devices or 0}

    async def apply_refund(self, identifier: str):
        """Возврат один раз корректирует услугу в той же транзакции, что и статус."""
        async with self._session_maker() as session, session.begin():
            payment = await session.scalar(select(Payment).where(
                Payment.provider_payment_id == identifier).with_for_update())
            if payment is None:
                raise LookupError('Платеж не найден')
            if payment.status == 'refunded':
                return None
            if payment.status in ('pending', 'cancelled'):
                # Возврат пришел раньше подтверждения: нечего выдавать и вычитать.
                payment.status = 'refunded'
                payment.completed_at = datetime.now()
                return payment
            if payment.status != 'succeeded':
                return None
            user = await session.scalar(select(User).where(
                User.user_id == payment.user_id).with_for_update())
            if user is None:
                raise LookupError('Пользователь не найден')
            if payment.kind == 'devices':
                user.extra_devices = max(0, (user.extra_devices or 0) - payment.extra_devices)
            elif user.subscription_end_date:
                user.subscription_end_date -= timedelta(days=payment.duration_days)
            payment.status = 'refunded'
            payment.completed_at = datetime.now()
            payment.sync_pending = True
            return payment

    async def get_pending_stars(self) -> list[Payment]:
        async with self._session_maker() as session:
            return (await session.scalars(select(Payment).where(
                Payment.source == 'stars', Payment.currency == 'XTR', Payment.status == 'pending',
                Payment.telegram_payment_charge_id.is_not(None)
            ).order_by(Payment.id).limit(100))).all()

    async def get_sync_pending(self) -> list[Payment]:
        async with self._session_maker() as session:
            return (await session.scalars(select(Payment).where(Payment.sync_pending.is_(True))
                                          .order_by(Payment.id).limit(100))).all()

    async def mark_synced(self, identifier: str, expected_status: str):
        async with self._session_maker() as session:
            await session.execute(update(Payment).where(
                Payment.provider_payment_id == identifier,
                Payment.status == expected_status,
            ).values(sync_pending=False))
            await session.commit()

    async def get_unsettled(self) -> list[Payment]:
        async with self._session_maker() as session:
            cutoff = datetime.now() - timedelta(days=180)
            return (await session.scalars(select(Payment).where(
                Payment.source != 'stars',
                Payment.status.in_(('pending', 'cancelled', 'succeeded')),
                ~Payment.provider_payment_id.startswith('order:'),
                Payment.created_at >= cutoff,
            ).order_by(Payment.last_checked_at.asc().nullsfirst(), Payment.id).limit(100))).all()

    async def mark_checked(self, identifier: str):
        async with self._session_maker() as session:
            await session.execute(update(Payment).where(
                Payment.provider_payment_id == identifier).values(last_checked_at=datetime.now()))
            await session.commit()

    async def reset_paid_quota(self, identifier: str, remnawave):
        """Сброс свежей квоты один раз на оплату; бонусные пользователи сюда не попадают."""
        async with self._session_maker() as session, session.begin():
            payment = await session.scalar(select(Payment).where(
                Payment.provider_payment_id == identifier).with_for_update())
            if (payment is None or payment.traffic_reset_done or payment.status != 'succeeded'
                    or payment.kind != 'subscription'):
                return
            user = await session.scalar(select(User).where(
                User.user_id == payment.user_id).with_for_update())
            latest = await session.scalar(select(Payment).where(
                Payment.user_id == payment.user_id, Payment.status == 'succeeded',
                Payment.kind == 'subscription').order_by(Payment.completed_at.desc(), Payment.id.desc()).limit(1))
            if latest.id == payment.id and user.remnawave_uuid:
                await remnawave.reset_user_traffic(user.remnawave_uuid)
            payment.traffic_reset_done = True

    async def sync_user(self, user_id: int, remnawave, base_limit: int, max_extra: int):
        """Блокировка пользователя сериализует абсолютные обновления панели.

        Сбой до коммита оставляет sync_pending у платежа: повтор безопасно применяет
        текущую дату из БД, а не прибавляет длительность платежа к панели.
        """
        async with self._session_maker() as session, session.begin():
            user = await session.scalar(select(User).where(User.user_id == user_id).with_for_update())
            if user is None or user.subscription_end_date is None:
                return
            username = user.vpn_username or (f'user_{user_id}' if user_id > 0 else f'web_{abs(user_id)}')
            existing = await remnawave.get_user_by_username(username)
            expiry = user.subscription_end_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')
            gb = user.subscription_data_limit_gb
            byte_limit = None if gb is None else gb * 1024 ** 3
            strategy = None if gb is None else ('MONTH' if gb > 0 else 'NO_RESET')
            active = user.subscription_end_date > datetime.now()
            limit = base_limit + min(max_extra, user.extra_devices or 0) if active else base_limit
            if existing:
                identifier = existing['uuid']
                await remnawave.update_user(identifier, expire_at=expiry,
                    traffic_limit_bytes=byte_limit, traffic_limit_strategy=strategy,
                    hwid_device_limit=limit)
                if not active:
                    await remnawave.disable_user(identifier)
                elif existing.get('status') in ('DISABLED', 'EXPIRED', 'LIMITED'):
                    await remnawave.enable_user(identifier)
            elif active:
                created = await remnawave.create_user(username=username, expire_at=expiry,
                    traffic_limit_bytes=byte_limit if byte_limit is not None else 1000 * 1024 ** 3,
                    traffic_limit_strategy=strategy or 'MONTH')
                await remnawave.update_user(created['uuid'], hwid_device_limit=limit)
                identifier = created['uuid']
            else:
                return
            user.vpn_username = username
            user.remnawave_uuid = identifier
