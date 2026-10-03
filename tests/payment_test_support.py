"""Реальные SQLAlchemy модели и локальная тестовая БД; внешние API подменены."""
import importlib.util
import sys
import os
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from sqlalchemy import BigInteger, text
from uuid import uuid4
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.ext.compiler import compiles
from test_stars_payment import _load_module, load_payment_service_module


@compiles(BigInteger, 'sqlite')
def _sqlite_bigint(element, compiler, **kwargs):
    return 'INTEGER'


def load_models():
    config_module = types.ModuleType('config')
    config_module.load_config = lambda: SimpleNamespace(dataBase=SimpleNamespace(
        user='test', password='test', host='localhost', port='5432', db_name='test'))
    return _load_module('payment_models_under_test', 'db.py', {'config': config_module})


class PaymentDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.models = load_models()
        dsn = os.environ.get('PAYMENT_TEST_DSN', 'sqlite+aiosqlite:///:memory:')
        self.schema = 'hamzavpn_test_' + uuid4().hex if dsn.startswith('postgresql') else None
        options = {'schema_translate_map': {None: self.schema}} if self.schema else {}
        self.engine = create_async_engine(dsn, execution_options=options)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            if self.schema:
                await conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            await conn.run_sync(self.models.Base.metadata.create_all)
        self.repository_module = _load_module('real_payment_repo_under_test',
            'database/repositories/payment.py', {'db': self.models})
        self.repo = self.repository_module.PaymentRepository(self.sessions)
        user_module = _load_module('real_user_repo_under_test',
            'database/repositories/user.py', {'db': self.models})
        self.user_repo = user_module.UserRepository(self.sessions)
        self.now = datetime.now()
        async with self.sessions() as session:
            session.add(self.models.User(user_id=77, full_name='Реферер',
                subscription_end_date=self.now + timedelta(days=5)))
            await session.flush()
            session.add_all([
                self.models.User(user_id=42, full_name='Покупатель', referrer_id=77,
                    subscription_end_date=self.now + timedelta(days=10),
                    is_first_payment_made=False, extra_devices=0),
                self.models.Tariff(id=5, name='Три месяца', duration_days=90,
                    price=399, price_stars=150, data_limit_gb=100),
            ])
            await session.commit()
        self.panel = SimpleNamespace(
            get_user_by_username=AsyncMock(side_effect=lambda username: {'uuid': f'uuid-{username}', 'status': 'ACTIVE'}),
            update_user=AsyncMock(), create_user=AsyncMock(return_value={'uuid': 'test-uuid'}),
            enable_user=AsyncMock(), disable_user=AsyncMock(), reset_user_traffic=AsyncMock(),
        )
        async def get_tariff(identifier):
            async with self.sessions() as session:
                return await session.get(self.models.Tariff, identifier)
        self.tariffs = SimpleNamespace(get_by_id=get_tariff)
        self.service_module = load_payment_service_module()
        self.slot_service = SimpleNamespace(settings=AsyncMock(return_value=SimpleNamespace(
            price=49, base_limit=3, max_extra=10)))
        self.service = self.service_module.PaymentService(
            subscription_service=SimpleNamespace(_remnawave=self.panel,
                _resolve_username=lambda user: f'user_{user.user_id}'),
            referral_service=SimpleNamespace(), user_repo=self.user_repo,
            tariff_repo=self.tariffs, payment_repo=self.repo,
            device_slot_service=self.slot_service,
        )

    async def asyncTearDown(self):
        if self.schema:
            async with self.engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.engine.dispose()

    async def create_invoice(self, identifier='p-1', **kwargs):
        args = dict(provider_payment_id=identifier, user_id=42, tariff_id=5,
                    original_amount=399.0, final_amount=399.0, source='bot')
        args.update(kwargs)
        return await self.repo.create(**args)

    async def user(self, identifier=42):
        return await self.user_repo.get(identifier)

    async def payment(self, identifier='p-1'):
        return await self.repo.get_by_provider_id(identifier)
