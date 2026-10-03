import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage

from utils.telegram_ui import replace_message_text


def bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(
        method=DeleteMessage(chat_id=1, message_id=1),
        message=message,
    )


class ReplaceMessageTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_message_delete_failure_does_not_escape(self):
        replacement = object()
        message = SimpleNamespace(
            edit_text=AsyncMock(side_effect=bad_request("message can't be edited")),
            answer=AsyncMock(return_value=replacement),
            delete=AsyncMock(
                side_effect=bad_request("message can't be deleted for everyone")
            ),
        )

        result = await replace_message_text(message, "Новый текст")

        self.assertIs(result, replacement)
        message.answer.assert_awaited_once_with("Новый текст", reply_markup=None)
        message.delete.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
