import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('tariffs_catalog', Path(__file__).resolve().parents[1] / 'scripts/tariffs_catalog.py')
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


class TariffTransferValidationTests(unittest.TestCase):
    def setUp(self):
        self.tariff = dict(name='Тестовый тариф', price=149.90, duration_days=30,
                           is_active=True, data_limit_gb=100, loyalty_price=None,
                           is_highlighted=False, price_stars=None)

    def test_catalog_keeps_price_duration_quota_and_flags(self):
        self.assertEqual(catalog.validate([self.tariff]), [tuple(self.tariff[f] for f in catalog.FIELDS)])

    def test_does_not_import_personal_or_unknown_fields(self):
        row = dict(self.tariff, user_id=77)
        with self.assertRaises(ValueError):
            catalog.validate([row])

    def test_rejects_invalid_price_duration_and_flags(self):
        for field, value in [('price',float('nan')), ('price',float('inf')), ('price',-1),
                             ('duration_days',0), ('duration_days',True), ('is_active','yes')]:
            with self.subTest(field=field,value=value):
                row=copy.copy(self.tariff);row[field]=value
                with self.assertRaises(ValueError):
                    catalog.validate([row])
