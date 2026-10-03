"""Совместимость старых внутренних вызовов: разовые платежи Platega не сохраняют карту."""

class PaymentMethodService:
    async def get_card(self, user_id: int):
        return None

    async def toggle_auto_renew(self, user_id: int) -> bool:
        return False

    async def set_renew_tariff(self, user_id: int, tariff_id: int) -> bool:
        return False

    async def delete_card(self, user_id: int) -> bool:
        return False
