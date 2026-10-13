"""Рендер изменённых Jinja-экранов с докупкой и без неё."""
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from jinja2 import Environment, FileSystemLoader

ROOT=Path(__file__).resolve().parents[1]


class TrafficTemplateTests(unittest.TestCase):
    def setUp(self):
        self.env=Environment(loader=FileSystemLoader(ROOT/'webapp/templates'))
        self.env.filters['timestamp_to_date']=str
        self.context=dict(user=NS(full_name='Test',user_id=42,email=None,vpn_username='test',extra_devices=1,
            extra_traffic_gb=200,has_received_trial=True,subscription_end_date=datetime.now()+timedelta(days=30)),
            vpn_data=NS(status='active',expire=1,used_traffic=100*1024**3,data_limit=500*1024**3),
            tariffs=[NS(id=1,name='Тариф',price=129,duration_days=30,data_limit_gb=500,price_stars=100)],
            tariff_prices={1:129},device_settings=NS(price=49,base_limit=5,max_extra=10),
            slot_quote=NS(current_extra=1,ok=False),traffic_settings=NS(pack_price=49,pack_gb=100,max_packs=10),
            current_traffic_packs=2,current_extra_devices=1,
            traffic_quote=NS(ok=True,available=8,pack_gb=100,current_limit_gb=700,price_per_pack=49,
                valid_until=datetime.now()+timedelta(days=30)),request=NS(),sub_link='https://example.com/sub',
            payments=[],devices=None,referral_count=0,referral_bonus=0)

    def test_all_changed_screens_render_with_purchase_and_saved_renewal_packs(self):
        for name in ['dashboard.html','tma/index.html','tma/tariffs.html']:
            with self.subTest(name=name):
                html=self.env.get_template(name).render(**self.context)
                self.assertIn('/static/js/traffic.js',html)
                if name in ('dashboard.html','tma/index.html'):
                    self.assertIn('id="buyTrafficBox"',html)
                if name in ('dashboard.html','tma/tariffs.html'):
                    self.assertIn('data-current-packs="2"',html)
                    self.assertIn('+200 ГБ/мес',html)
                self.assertNotIn('tma-stars-btn',html)

    def test_unavailable_quote_hides_purchase_and_limited_status_has_correct_label(self):
        self.context['traffic_quote']=NS(ok=False)
        self.context['vpn_data'].status='limited'
        for name in ['dashboard.html','tma/index.html']:
            html=self.env.get_template(name).render(**self.context)
            self.assertNotIn('id="buyTrafficBox"',html)
            self.assertIn('Квота трафика исчерпана',html)
            self.assertNotIn('Подписка истекла',html)

    def test_unlimited_catalog_does_not_display_paid_pack_selector(self):
        self.context['tariffs'][0].data_limit_gb=0
        for name in ['dashboard.html','tma/tariffs.html']:
            html=self.env.get_template(name).render(**self.context)
            self.assertNotIn('id="trafficSelector"',html)
