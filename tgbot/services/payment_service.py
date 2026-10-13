from dataclasses import dataclass
from db import Tariff, Payment
from tgbot.services.subscription_service import ExtensionResult
from loader import logger
from platega.client import PlategaError, money, verified_order_amount


@dataclass
class PaymentResult:
    tariff: Tariff | None
    extension: ExtensionResult | None
    referrer_id: int | None
    is_first_payment: bool
    payment: Payment | None = None
    kind: str = 'subscription'
    extra_devices: int = 0
    extra_traffic_gb: int = 0


@dataclass
class StarsPaymentResult:
    tariff: Tariff
    extension: ExtensionResult
    payment: Payment
    referrer_id: int | None = None
    is_first_payment: bool = False


class PendingPaymentError(RuntimeError):
    pass


class PaymentService:
    def __init__(self, subscription_service, referral_service, user_repo, tariff_repo,
                 payment_repo, payment_method_service=None, device_slot_service=None, recurring_service=None):
        self._subscription_service = subscription_service
        self._referral_service = referral_service
        self._user_repo = user_repo
        self._tariff_repo = tariff_repo
        self._payment_repo = payment_repo
        self._device_slot_service = device_slot_service
        self._recurring_service = recurring_service

    async def create_payment_record(self, provider_payment_id: str, user_id: int,
                                    tariff_id: int | None, original_amount: float,
                                    final_amount: float, source: str = 'bot',
                                    promo_code: str = None, discount_percent: int = 0,
                                    kind: str = 'subscription', extra_devices: int = 0,
                                    payment_url: str | None = None, extra_traffic_gb: int = 0) -> Payment:
        return await self._payment_repo.create(
            provider_payment_id=provider_payment_id, user_id=user_id, tariff_id=tariff_id,
            original_amount=original_amount, final_amount=final_amount, source=source,
            promo_code=promo_code, discount_percent=discount_percent, kind=kind,
            extra_devices=extra_devices, payment_url=payment_url, extra_traffic_gb=extra_traffic_gb,
        )

    async def create_checkout(self, user_id: int, tariff_id: int | None,
                              original_amount: float, final_amount: float, description: str,
                              return_url: str, source: str = 'bot', promo_code=None,
                              discount_percent: int = 0, kind: str = 'subscription',
                              extra_devices: int = 0, metadata: dict | None = None,
                              extra_traffic_gb: int = 0):
        from uuid import uuid4
        from tgbot.services.payment import create_payment
        async with self._payment_repo.checkout_lock(user_id):
            if await self.has_pending_payment(user_id):
                raise PendingPaymentError('У вас уже есть неоплаченный счет')
            order_id = str(uuid4())
            reservation = f'order:{order_id}'
            await self.create_payment_record(
                reservation, user_id, tariff_id, original_amount, final_amount, source,
                promo_code, discount_percent, kind, extra_devices, extra_traffic_gb=extra_traffic_gb,
            )
            details = dict(metadata or {})
            details['orderId'] = order_id
            try:
                payment_url, identifier = await create_payment(
                    amount=final_amount, description=description, return_url=return_url,
                    user_id=user_id, metadata=details,
                )
                await self._payment_repo.bind_transaction(reservation, identifier, payment_url)
                return payment_url, identifier
            except Exception:
                # Резерв остается для recovery по API payload даже при потере ответа.
                await self._payment_repo.cancel_if_pending(reservation)
                raise

    async def has_pending_payment(self, user_id: int) -> bool:
        return await self._payment_repo.get_user_pending(user_id) is not None

    async def get_pending_payment(self, user_id: int) -> Payment | None:
        return await self._payment_repo.get_user_pending(user_id)

    async def cancel_pending_payment(self, user_id: int) -> Payment | None:
        """Локально закрывает счет. Подтвержденная позже оплата все равно зачисляется."""
        pending = await self._payment_repo.get_user_pending(user_id)
        if pending and await self._payment_repo.cancel_if_pending(pending.provider_payment_id):
            return pending
        return None

    async def cancel_stale_payments(self, older_than_minutes: int) -> list[Payment]:
        cancelled = []
        for payment in await self._payment_repo.get_pending_older_than(older_than_minutes):
            try:
                if await self._payment_repo.cancel_if_pending(payment.provider_payment_id):
                    cancelled.append(payment)
            except Exception:
                logger.exception('Не удалось закрыть неоплаченный счет %s', payment.provider_payment_id)
        return cancelled

    async def _settings(self):
        if self._device_slot_service:
            return await self._device_slot_service.settings()
        from tgbot.services.device_pricing import DeviceSettings
        return DeviceSettings()

    async def sync_fulfillment(self, payment: Payment):
        if not payment.sync_pending:
            return
        settings = await self._settings()
        panel = self._subscription_service._remnawave
        await self._payment_repo.sync_user(payment.user_id, panel, settings.base_limit, settings.max_extra)
        if payment.referrer_id:
            await self._payment_repo.sync_user(payment.referrer_id, panel, settings.base_limit, settings.max_extra)
        await self._payment_repo.reset_paid_quota(payment.provider_payment_id, panel)
        if self._recurring_service and payment.kind == 'traffic':
            user = await self._user_repo.get(payment.user_id)
            if user:
                # Сбой отмены оставляет sync_pending для следующей сверки.
                await self._recurring_service.cancel_if_composition_changed(user)
        await self._payment_repo.mark_synced(payment.provider_payment_id, payment.status)

    async def process_successful_payment(self, provider_payment_id: str,
                                         paid_amount: float, currency: str = 'RUB') -> PaymentResult | None:
        settings = await self._settings()
        grant = await self._payment_repo.apply_confirmed(
            provider_payment_id, paid_amount, currency, settings.max_extra,
        )
        payment = grant['payment'] if grant else await self._payment_repo.get_by_provider_id(provider_payment_id)
        retry_sync = bool(payment and payment.sync_pending and payment.status == 'succeeded')
        if payment:
            await self.sync_fulfillment(payment)
        if not grant and not retry_sync:
            return None
        user = await self._user_repo.get(payment.user_id)
        tariff = await self._tariff_repo.get_by_id(payment.tariff_id) if payment.tariff_id else None
        extension = None
        if payment.kind == 'subscription':
            if tariff is None:
                raise LookupError('Тариф платежа не найден')
            extension = ExtensionResult(
                is_new_user=grant['is_new'] if grant else False,
                username=user.vpn_username or self._subscription_service._resolve_username(user),
            )
        return PaymentResult(
            tariff=tariff, extension=extension, payment=payment,
            referrer_id=payment.referrer_id,
            is_first_payment=grant['first'] if grant else False,
            kind=payment.kind, extra_devices=user.extra_devices or 0,
            extra_traffic_gb=user.extra_traffic_gb or 0,
        )

    async def prepare_stars_invoice(self, user_id: int, tariff_id: int):
        from uuid import uuid4
        tariff = await self._tariff_repo.get_by_id(tariff_id)
        if not tariff or not tariff.is_active or not tariff.price_stars:
            raise ValueError('Тариф Stars недоступен')
        order_id = str(uuid4())
        payment = await self._payment_repo.create(
            provider_payment_id=f'order:{order_id}', user_id=user_id, tariff_id=tariff_id,
            original_amount=tariff.price_stars, final_amount=tariff.price_stars,
            source='stars', currency='XTR',
        )
        return payment

    async def get_stars_invoice(self, order_id: str):
        return await self._payment_repo.get_stars_invoice(order_id)

    async def process_stars_payment(self, user_id: int, tariff_id: int, total_amount: int,
                                    telegram_payment_charge_id: str,
                                    order_id: str | None = None) -> StarsPaymentResult | None:
        identifier = f'stars:{telegram_payment_charge_id}'
        if order_id:
            invoice = await self._payment_repo.bind_stars_charge(
                order_id, telegram_payment_charge_id, user_id, total_amount, tariff_id)
            if invoice.tariff_id != tariff_id:
                raise ValueError('Тариф счета Stars не совпадает')
        else:
            # Совместимость ранее выпущенных инвойсов без идентификатора заказа.
            existing = await self._payment_repo.get_by_telegram_charge_id(telegram_payment_charge_id)
            tariff = await self._tariff_repo.get_by_id(tariff_id)
            if existing is None and (not tariff or tariff.price_stars != total_amount):
                raise ValueError('Цена тарифа в Stars не совпадает')
            await self._payment_repo.create(
                provider_payment_id=identifier, user_id=user_id, tariff_id=tariff_id,
                original_amount=total_amount, final_amount=total_amount, source='stars',
                currency='XTR', telegram_payment_charge_id=telegram_payment_charge_id,
            )
        result = await self.process_successful_payment(identifier, total_amount, currency='XTR')
        if result is None:
            return None
        return StarsPaymentResult(result.tariff, result.extension, result.payment,
                                  result.referrer_id, result.is_first_payment)

    async def process_refund(self, provider_payment_id: str) -> Payment | None:
        payment = await self._payment_repo.apply_refund(provider_payment_id)
        if payment:
            await self.sync_fulfillment(payment)
        else:
            existing = await self._payment_repo.get_by_provider_id(provider_payment_id)
            if existing:
                await self.sync_fulfillment(existing)
        return payment

    async def get_user_payments(self, user_id: int, limit: int = 20) -> list[Payment]:
        return await self._payment_repo.get_user_payments(user_id, limit)

    async def reconcile(self):
        """Проверка недоставленных callback и повтор безопасной выдачи в панель."""
        from loader import platega_client
        for payment in await self._payment_repo.get_pending_stars():
            try:
                await self.process_successful_payment(payment.provider_payment_id,
                                                      payment.final_amount, currency='XTR')
            except Exception:
                logger.exception('Выдача Stars платежа %s отложена', payment.provider_payment_id)
        for payment in await self._payment_repo.get_sync_pending():
            try:
                await self.sync_fulfillment(payment)
            except Exception:
                logger.exception('Повтор выдачи платежа %s отложен', payment.provider_payment_id)
        if not platega_client.configured:
            return
        for payment in await self._payment_repo.get_unsettled():
            try:
                verified = await platega_client.get_payment(payment.provider_payment_id)
                try:
                    order_amount = verified_order_amount(
                        verified, payment.final_amount, payment.currency,
                        order_id=payment.order_id, user_id=payment.user_id)
                except ValueError:
                    logger.error('Несовпадение суммы при сверке платежа %s', payment.provider_payment_id)
                    await self._payment_repo.mark_checked(payment.provider_payment_id)
                    continue
                if verified['status'] == 'CONFIRMED':
                    await self.process_successful_payment(payment.provider_payment_id, order_amount)
                elif verified['status'] == 'CANCELED':
                    await self._payment_repo.cancel_if_pending(payment.provider_payment_id)
                elif verified['status'] == 'CHARGEBACKED':
                    await self.process_refund(payment.provider_payment_id)
                await self._payment_repo.mark_checked(payment.provider_payment_id)
            except PlategaError as error:
                # Отказ API не меняет финансовое состояние. Отмечаем попытку,
                # чтобы неисправная строка не занимала начало очереди постоянно.
                await self._payment_repo.mark_checked(payment.provider_payment_id)
                log = logger.warning if error.retryable else logger.error
                log('Сверка платежа %s отложена: %s; причина=%s; HTTP=%s',
                    payment.provider_payment_id, error, error.reason, error.status_code)
                if error.retryable or error.status_code in (401, 403):
                    # Общая проблема шлюза: не посылаем ещё сотню запросов.
                    break
            except Exception:
                logger.exception('Сверка платежа %s отложена', payment.provider_payment_id)
