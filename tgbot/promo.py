from dataclasses import dataclass
from typing import Literal, Protocol


class PromoLike(Protocol):
    bonus_days: int
    discount_percent: int


@dataclass(frozen=True)
class PromoReward:
    kind: Literal["bonus_days", "discount"]
    value: int

    @property
    def button_text(self) -> str:
        if self.kind == "bonus_days":
            return f"🎁 Получить {_bonus_days_phrase(self.value)}"
        return f"💰 Применить скидку {self.value}%"

    @property
    def description(self) -> str:
        if self.kind == "bonus_days":
            return _bonus_days_phrase(self.value)
        return f"скидка {self.value}%"


def _bonus_days_phrase(value: int) -> str:
    remainder_100 = value % 100
    remainder_10 = value % 10
    if 11 <= remainder_100 <= 14:
        return f"{value} бонусных дней"
    if remainder_10 == 1:
        return f"{value} бонусный день"
    if 2 <= remainder_10 <= 4:
        return f"{value} бонусных дня"
    return f"{value} бонусных дней"


def get_promo_reward(promo: PromoLike) -> PromoReward | None:
    """Возвращает фактическую награду промокода.

    Админская форма создания промокодов задаёт только один тип. Если в старой
    записи заполнены оба значения, сохраняем поведение ручного ввода и сначала
    начисляем бонусные дни.
    """
    if promo.bonus_days > 0:
        return PromoReward(kind="bonus_days", value=promo.bonus_days)
    if promo.discount_percent > 0:
        return PromoReward(kind="discount", value=promo.discount_percent)
    return None
