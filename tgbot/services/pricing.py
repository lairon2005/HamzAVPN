# tgbot/services/pricing.py
"""
Расчёт эффективной цены тарифа (§7.1 «цена навсегда» / лоялти-цена).

Правило: новым пользователям — обычная `price`; тем, кто продлевается
НЕПРЕРЫВНО (на момент покупки их подписка ещё активна: `subscription_end_date > now`)
— `loyalty_price`, если он задан у тарифа.

Применяется согласованно в нескольких местах:
  1. Отображение тарифов (tariffs_keyboard)
  2. Чекаут (select_tariff_handler / _compute_price / _create_and_send_payment)
  3. Автосписание (charge_renewal — оно по определению непрерывно)
  4. Веб-оплата (webapp/routers/payment.py)
"""


def effective_price(tariff, user_has_active_sub: bool) -> float:
    """Возвращает цену, которую должен заплатить пользователь за тариф прямо сейчас."""
    if user_has_active_sub and tariff.loyalty_price:
        return tariff.loyalty_price
    return tariff.price


def format_quota(data_limit_gb) -> str:
    """Человекочитаемая квота трафика тарифа: '100 ГБ/мес' или 'Безлимит' (None/0)."""
    return f"{data_limit_gb} ГБ/мес" if data_limit_gb else "Безлимит"
