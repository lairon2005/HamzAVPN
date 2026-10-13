"""Квота трафика и цена докупленных пакетов — чистые функции."""
import importlib.util
import unittest
from pathlib import Path

from real_traffic_pricing import real_traffic_pricing

tp = real_traffic_pricing()


class TariffQuotaTests(unittest.TestCase):
    def setUp(self):
        self.settings = tp.TrafficSettings()

    def test_null_keeps_unlimited_catalog_semantics(self):
        self.assertEqual(tp.tariff_quota_gb(self.settings, None), 0)

    def test_zero_means_unlimited(self):
        self.assertEqual(tp.tariff_quota_gb(self.settings, 0), 0)
        self.assertTrue(tp.is_unlimited(0))

    def test_explicit_quota_is_kept(self):
        self.assertEqual(tp.tariff_quota_gb(self.settings, 100), 100)

    def test_base_follows_settings(self):
        self.assertEqual(tp.tariff_quota_gb(tp.TrafficSettings(base_gb=800), None), 0)


class TotalLimitTests(unittest.TestCase):
    def test_extra_is_added_while_subscription_active(self):
        self.assertEqual(tp.total_limit_gb(500, 200), 700)

    def test_extra_burns_with_subscription(self):
        self.assertEqual(tp.total_limit_gb(500, 200, subscription_active=False), 500)

    def test_unlimited_stays_unlimited(self):
        self.assertEqual(tp.total_limit_gb(0, 300), 0)

    def test_negative_extra_is_ignored(self):
        self.assertEqual(tp.total_limit_gb(500, -50), 500)


class PackCostTests(unittest.TestCase):
    def setUp(self):
        self.settings = tp.TrafficSettings(pack_price=49)

    def test_cost_is_price_times_months_times_packs(self):
        self.assertEqual(tp.packs_cost_for_tariff(self.settings, 90, 2), 49 * 3 * 2)

    def test_short_tariff_pays_at_least_one_month(self):
        self.assertEqual(tp.packs_cost_for_tariff(self.settings, 7, 1), 49)

    def test_no_packs_cost_nothing(self):
        self.assertEqual(tp.packs_cost_for_tariff(self.settings, 30, 0), 0.0)

    def test_prorated_is_never_cheaper_than_a_month(self):
        for days in range(1, 31):
            self.assertGreaterEqual(tp.prorated_pack_cost(self.settings, days), 49)

    def test_prorated_year_costs_more_than_month(self):
        self.assertEqual(tp.prorated_pack_cost(self.settings, 360), 49 * 12)

    def test_prorated_is_monotonic(self):
        costs = [tp.prorated_pack_cost(self.settings, d) for d in range(1, 400)]
        self.assertEqual(costs, sorted(costs))


class PacksConversionTests(unittest.TestCase):
    def test_gb_to_packs_rounds_up(self):
        settings = tp.TrafficSettings(pack_gb=100)
        self.assertEqual(tp.gb_to_packs(settings, 0), 0)
        self.assertEqual(tp.gb_to_packs(settings, 100), 1)
        self.assertEqual(tp.gb_to_packs(settings, 150), 2)

    def test_pack_size_change_does_not_lose_paid_gb(self):
        # Админ уменьшил пакет — прежде купленные 200 ГБ считаются как 4 пакета.
        self.assertEqual(tp.gb_to_packs(tp.TrafficSettings(pack_gb=50), 200), 4)

    def test_zero_pack_size_is_safe(self):
        self.assertEqual(tp.gb_to_packs(tp.TrafficSettings(pack_gb=0), 100), 0)


class CheckoutIntegrationTests(unittest.TestCase):
    """Пакеты входят в сумму счёта и отдельной строкой в чек (54-ФЗ)."""

    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "tgbot" / "services" / "device_pricing.py"
        spec = importlib.util.spec_from_file_location("dp_for_traffic_test", path)
        self.dp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.dp)

    def test_traffic_is_part_of_total_and_not_discounted(self):
        checkout = self.dp.build_checkout(self.dp.DeviceSettings(), 400, 90, slots=0, discount_percent=50)
        full = checkout.with_traffic(2, 294.0, 200)
        self.assertEqual(full.total, 200 + 294)
        self.assertEqual(full.original_total, 400 + 294)
        self.assertTrue(full.has_traffic)

    def test_receipt_has_separate_traffic_line(self):
        checkout = self.dp.build_checkout(self.dp.DeviceSettings(), 399, 90, slots=1)
        full = checkout.with_traffic(2, 294.0, 200)
        items = self.dp.receipt_items("Три месяца", full, 90)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[2]["quantity"], 2)
        self.assertAlmostEqual(sum(i["quantity"] * i["amount"] for i in items), full.total)

    def test_no_traffic_leaves_old_behaviour(self):
        checkout = self.dp.build_checkout(self.dp.DeviceSettings(), 399, 30, slots=0)
        self.assertFalse(checkout.has_traffic)
        self.assertEqual(len(self.dp.receipt_items("Месяц", checkout, 30)), 1)


if __name__ == "__main__":
    unittest.main()
