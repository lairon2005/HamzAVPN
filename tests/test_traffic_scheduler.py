"""Сверка трафика и предупреждения: восстановление, дедупликация, отказ панели."""
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from test_stale_payment_cancel import load_scheduler_module


class TrafficSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setup_module(self):
        module,stubs=load_scheduler_module()
        stubs['tgbot.services'].traffic_service=Mock()
        stubs['tgbot.services'].device_slot_service=NS(settings=AsyncMock(return_value=NS(base_limit=5,max_extra=10)))
        stubs['database'].payment_repo=NS(sync_user=AsyncMock())
        stubs['loader'].remnawave_client=NS(get_all_users=AsyncMock(return_value=[]))
        return module,stubs

    def test_traffic_jobs_are_registered(self):
        module,_=self.setup_module();scheduler=Mock()
        module.schedule_jobs(scheduler,Mock())
        functions=[call.args[0] for call in scheduler.add_job.call_args_list]
        self.assertIn(module.sync_traffic_limits,functions)
        self.assertIn(module.traffic_usage_alerts,functions)

    async def test_sync_is_serialized_by_repository_and_one_failure_does_not_stop_other_users(self):
        module,stubs=self.setup_module()
        module.user_repo.get_with_extra_traffic=AsyncMock(return_value=[NS(user_id=1),NS(user_id=2)])
        repo=stubs['database'].payment_repo
        repo.sync_user.side_effect=[RuntimeError('temporary panel outage'),None]
        with patch.dict(sys.modules,stubs):
            await module.sync_traffic_limits(Mock())
        self.assertEqual(repo.sync_user.await_count,2)
        self.assertEqual([call.args[0] for call in repo.sync_user.await_args_list],[1,2])

    async def test_warning_is_sent_once_per_level_per_month(self):
        module,stubs=self.setup_module();sent=set()
        module.user_repo.get_active_telegram_subscribers=AsyncMock(return_value=[NS(user_id=1,vpn_username='user_1')])
        module.lifecycle_repo.was_sent=AsyncMock(side_effect=lambda *args:args in sent)
        module.lifecycle_repo.mark_sent=AsyncMock(side_effect=lambda *args:sent.add(args))
        module._send_lifecycle_message=AsyncMock(return_value=True)
        panel=stubs['loader'].remnawave_client
        row={'username':'user_1','trafficLimitBytes':100*1024**3,'userTraffic':{'usedTrafficBytes':90*1024**3}}
        panel.get_all_users.return_value=[row]
        with patch.dict(sys.modules,stubs):
            await module.traffic_usage_alerts(Mock())
            await module.traffic_usage_alerts(Mock())
            self.assertEqual(module._send_lifecycle_message.await_count,1)
            row['userTraffic']['usedTrafficBytes']=100*1024**3
            await module.traffic_usage_alerts(Mock())
            await module.traffic_usage_alerts(Mock())
            self.assertEqual(module._send_lifecycle_message.await_count,2)
            row['userTraffic']['usedTrafficBytes']=90*1024**3
            await module.traffic_usage_alerts(Mock())
            self.assertEqual(module._send_lifecycle_message.await_count,2)

    async def test_panel_outage_does_not_mark_warning_as_sent(self):
        module,stubs=self.setup_module()
        module.user_repo.get_active_telegram_subscribers=AsyncMock(return_value=[])
        module.lifecycle_repo.mark_sent=AsyncMock()
        stubs['loader'].remnawave_client.get_all_users.side_effect=RuntimeError('offline')
        with patch.dict(sys.modules,stubs):
            await module.traffic_usage_alerts(Mock())
        module.lifecycle_repo.mark_sent.assert_not_awaited()
