"""Первое личное обращение через публичные документы сохраняет тикет."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_ui_mode import load_inline_module


def load_support_module(existing_topic=None):
    user_service = SimpleNamespace(register_or_get=AsyncMock(return_value=(SimpleNamespace(user_id=42), True)))
    support_service = SimpleNamespace(
        get_topic_id=AsyncMock(return_value=existing_topic), save_topic=AsyncMock(),
    )
    services = types.ModuleType("tgbot.services")
    services.user_service = user_service
    services.support_service = support_service
    database = types.ModuleType("database")
    database.user_repo = SimpleNamespace()
    loader = types.ModuleType("loader")
    loader.logger = Mock()
    loader.config = SimpleNamespace(tg_bot=SimpleNamespace(support_chat_id=-10042))
    stubs = {
        "loader": loader, "database": database, "tgbot.services": services,
        "tgbot.keyboards.inline": load_inline_module(),
    }
    path = Path(__file__).resolve().parents[1] / "tgbot/handlers/support.py"
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("support_first_contact_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, user_service, support_service


class SupportFirstContactTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_user_is_saved_before_ticket_and_name_is_escaped(self):
        module, user_service, support_service = load_support_module()
        sequence = Mock()
        sequence.attach_mock(user_service.register_or_get, "register")
        sequence.attach_mock(support_service.save_topic, "save_ticket")
        call = SimpleNamespace(
            from_user=SimpleNamespace(id=42, full_name="<Alice>", username="alice_test"),
            message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(),
        )
        state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock())
        bot = SimpleNamespace(
            create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=7)),
            send_message=AsyncMock(),
        )
        await module.start_support_chat_confirmed(call, state, bot)
        user_service.register_or_get.assert_awaited_once_with(42, "<Alice>", "alice_test")
        support_service.save_topic.assert_awaited_once_with(42, 7)
        self.assertEqual([c[0] for c in sequence.mock_calls], ["register", "save_ticket"])
        text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("&lt;Alice&gt;", text)
        self.assertNotIn("<Alice>", text)
        state.set_state.assert_awaited_once_with(module.SupportFSM.in_chat)

    async def test_existing_ticket_resumes_without_creating_duplicate(self):
        module, user_service, support_service = load_support_module(existing_topic=7)
        call = SimpleNamespace(
            from_user=SimpleNamespace(id=42, full_name="Alice", username="alice_test"),
            message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(),
        )
        state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock())
        bot = SimpleNamespace(create_forum_topic=AsyncMock(), send_message=AsyncMock())
        await module.start_support_chat_confirmed(call, state, bot)
        user_service.register_or_get.assert_awaited_once()
        bot.create_forum_topic.assert_not_awaited()
        support_service.save_topic.assert_not_awaited()
        state.set_state.assert_awaited_once_with(module.SupportFSM.in_chat)
