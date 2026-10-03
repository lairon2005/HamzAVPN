import importlib.util
import unittest
from datetime import datetime, timedelta
from pathlib import Path


def _load_device_pricing():
    """Грузим модуль по пути: импорт пакета tgbot.services поднял бы синглтоны и конфиг."""
    module_path = (
        Path(__file__).resolve().parents[1] / "tgbot" / "services" / "device_pricing.py"
    )
    spec = importlib.util.spec_from_file_location("device_pricing_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pricing = _load_device_pricing()

DeviceSettings = pricing.DeviceSettings
billing_months = pricing.billing_months
build_checkout = pricing.build_checkout
days_left = pricing.days_left
device_limit = pricing.device_limit
prorated_slot_cost = pricing.prorated_slot_cost
prorated_slots_cost = pricing.prorated_slots_cost
receipt_items = pricing.receipt_items
slots_cost_for_tariff = pricing.slots_cost_for_tariff

SETTINGS = DeviceSettings(price=49, base_limit=5, max_extra=5)


class BillingMonthsTests(unittest.TestCase):
    def test_round_months_for_standard_durations(self):
        self.assertEqual(billing_months(30), 1)
        self.assertEqual(billing_months(90), 3)
        self.assertEqual(billing_months(180), 6)

    def test_year_is_twelve_months_not_twelve_and_a_bit(self):
        """365/30 = 12.17 — округляем к ближайшему, иначе платим за 13-й месяц."""
        self.assertEqual(billing_months(365), 12)

    def test_short_tariff_still_pays_a_full_month(self):
        self.assertEqual(billing_months(7), 1)
        self.assertEqual(billing_months(0), 1)


class TariffSlotsCostTests(unittest.TestCase):
    def test_three_month_tariff_with_one_slot(self):
        """Схема из ТЗ: тариф 399 + 49×3 за одно доп. устройство."""
        self.assertEqual(slots_cost_for_tariff(SETTINGS, 90, 1), 147.0)

    def test_several_slots_multiply(self):
        self.assertEqual(slots_cost_for_tariff(SETTINGS, 30, 3), 147.0)

    def test_no_slots_costs_nothing(self):
        self.assertEqual(slots_cost_for_tariff(SETTINGS, 90, 0), 0.0)


class ProrationTests(unittest.TestCase):
    def test_half_month_left_still_costs_a_full_month(self):
        """Минимальный чек за слот — месячная цена."""
        self.assertEqual(prorated_slot_cost(SETTINGS, 15), 49)

    def test_full_month_left(self):
        self.assertEqual(prorated_slot_cost(SETTINGS, 30), 49)

    def test_long_subscription_pays_for_the_whole_remainder(self):
        """Главная дыра схемы: слот на год не должен стоить 49 ₽."""
        self.assertEqual(prorated_slot_cost(SETTINGS, 300), 490)

    def test_partial_month_rounds_up(self):
        # 45 дней → 49 × 1.5 = 73.5 → 74
        self.assertEqual(prorated_slot_cost(SETTINGS, 45), 74)

    def test_multiple_slots(self):
        self.assertEqual(prorated_slots_cost(SETTINGS, 60, 2), 196.0)

    def test_zero_slots(self):
        self.assertEqual(prorated_slots_cost(SETTINGS, 60, 0), 0.0)


class DaysLeftTests(unittest.TestCase):
    def test_expired_subscription(self):
        past = datetime.now() - timedelta(days=1)
        self.assertEqual(days_left(past), 0)

    def test_missing_date(self):
        self.assertEqual(days_left(None), 0)

    def test_started_day_counts_as_paid(self):
        soon = datetime.now() + timedelta(hours=5)
        self.assertEqual(days_left(soon), 1)

    def test_rounds_up(self):
        end = datetime.now() + timedelta(days=10, hours=3)
        self.assertEqual(days_left(end), 11)


class DeviceLimitTests(unittest.TestCase):
    def test_active_subscription_gets_base_plus_slots(self):
        self.assertEqual(device_limit(SETTINGS, 2, subscription_active=True), 7)

    def test_expired_subscription_falls_back_to_base(self):
        self.assertEqual(device_limit(SETTINGS, 3, subscription_active=False), 5)

    def test_no_slots(self):
        self.assertEqual(device_limit(SETTINGS, 0, subscription_active=True), 5)


class CheckoutTests(unittest.TestCase):
    def test_tariff_with_slots_matches_spec_formula(self):
        checkout = build_checkout(SETTINGS, tariff_price=399, duration_days=90, slots=1)
        self.assertEqual(checkout.total, 399 + 49 * 3)

    def test_discount_applies_to_tariff_only(self):
        """Промокод -50% режет тариф, но не цену устройств."""
        checkout = build_checkout(
            SETTINGS, tariff_price=400, duration_days=30, slots=2, discount_percent=50
        )
        self.assertEqual(checkout.tariff_final, 200)
        self.assertEqual(checkout.slots_cost, 98.0)
        self.assertEqual(checkout.total, 298.0)

    def test_no_slots_leaves_price_untouched(self):
        checkout = build_checkout(SETTINGS, tariff_price=399, duration_days=90, slots=0)
        self.assertEqual(checkout.total, 399)
        self.assertFalse(checkout.has_slots)


class ReceiptTests(unittest.TestCase):
    def test_slots_are_a_separate_receipt_line(self):
        checkout = build_checkout(SETTINGS, tariff_price=399, duration_days=90, slots=2)
        items = receipt_items("Три месяца", checkout, 90)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["amount"], 399)
        self.assertEqual(items[1]["quantity"], 2)
        # Цена за единицу: 49 × 3 месяца
        self.assertEqual(items[1]["amount"], 147.0)

    def test_receipt_total_matches_charged_amount(self):
        """Сумма позиций заказа должна совпадать с итоговой суммой платежа."""
        checkout = build_checkout(
            SETTINGS, tariff_price=399, duration_days=90, slots=3, discount_percent=20
        )
        items = receipt_items("Три месяца", checkout, 90)
        total = sum(item["amount"] * item["quantity"] for item in items)
        self.assertAlmostEqual(total, checkout.total, places=2)

    def test_single_line_without_slots(self):
        checkout = build_checkout(SETTINGS, tariff_price=199, duration_days=30, slots=0)
        self.assertEqual(len(receipt_items("Месяц", checkout, 30)), 1)


if __name__ == "__main__":
    unittest.main()
