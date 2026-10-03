import logging
import unittest

from aiohttp.http_exceptions import BadStatusLine

from utils.logger import AiohttpNoiseFilter


def _record(exc: BaseException | None) -> logging.LogRecord:
    exc_info = (type(exc), exc, None) if exc is not None else None
    return logging.LogRecord(
        name="aiohttp.server",
        level=logging.ERROR,
        pathname="web_protocol.py",
        lineno=421,
        msg="Error handling request",
        args=None,
        exc_info=exc_info,
    )


class AiohttpNoiseFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.filter = AiohttpNoiseFilter()

    def test_bad_status_line_is_dropped(self):
        # Мусор от интернет-сканера на открытом порту → запись не должна пройти.
        self.assertFalse(self.filter.filter(_record(BadStatusLine("garbage"))))

    def test_connection_reset_is_dropped(self):
        self.assertFalse(self.filter.filter(_record(ConnectionResetError())))

    def test_real_handler_error_passes(self):
        # Настоящая ошибка обработчика (любой другой тип) должна логироваться.
        self.assertTrue(self.filter.filter(_record(ValueError("real bug"))))

    def test_record_without_exception_passes(self):
        self.assertTrue(self.filter.filter(_record(None)))


if __name__ == "__main__":
    unittest.main()
