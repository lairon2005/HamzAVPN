import logging
import threading
import time
import unittest
from unittest.mock import patch

import requests

from utils.logger import APINotificationHandler


class _Response:
    def raise_for_status(self) -> None:
        return None


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="test.logger",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class APINotificationHandlerTests(unittest.TestCase):
    def test_emit_does_not_wait_for_network(self):
        request_started = threading.Event()
        release_request = threading.Event()

        def blocked_post(*args, **kwargs):
            request_started.set()
            release_request.wait(timeout=2.0)
            return _Response()

        with patch("utils.logger.requests.post", side_effect=blocked_post):
            handler = APINotificationHandler("token", 1)
            try:
                started_at = time.monotonic()
                handler.emit(_record("panel unavailable"))
                elapsed = time.monotonic() - started_at

                self.assertLess(elapsed, 0.1)
                self.assertTrue(request_started.wait(timeout=0.5))
            finally:
                release_request.set()
                handler.close()

    def test_duplicate_and_rate_limited_records_are_dropped(self):
        with patch("utils.logger.requests.post", return_value=_Response()) as post:
            handler = APINotificationHandler(
                "token",
                1,
                dedupe_window=60,
                rate_limit=2,
                rate_window=60,
            )
            try:
                handler.emit(_record("same error"))
                handler.emit(_record("same error"))
                handler.emit(_record("different error"))
                handler.emit(_record("third unique error"))

                self.assertTrue(_wait_until(lambda: post.call_count == 2))
                self.assertEqual(handler.dropped_notifications, 2)
            finally:
                handler.close()

    def test_delivery_failure_does_not_escape_or_stop_worker(self):
        outcomes = [requests.RequestException("offline"), _Response()]

        def post(*args, **kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch("utils.logger.requests.post", side_effect=post) as mocked_post:
            handler = APINotificationHandler("token", 1)
            try:
                handler.emit(_record("first"))
                handler.emit(_record("second"))

                self.assertTrue(_wait_until(lambda: mocked_post.call_count == 2))
                self.assertEqual(handler.failed_notifications, 1)
                self.assertTrue(handler._worker.is_alive())
            finally:
                handler.close()

    def test_secrets_are_redacted_and_html_is_escaped(self):
        delivered = threading.Event()
        captured = {}

        def post(url, *, json, timeout, proxies):
            captured.update(
                url=url,
                payload=json,
                timeout=timeout,
                proxies=proxies,
            )
            delivered.set()
            return _Response()

        with patch("utils.logger.requests.post", side_effect=post):
            handler = APINotificationHandler(
                "bot-token",
                1,
                redactions=("api-token", "cookie=value"),
                request_timeout=(1.0, 2.0),
            )
            try:
                handler.emit(
                    _record("api-token cookie=value <invalid>&response")
                )
                self.assertTrue(delivered.wait(timeout=0.5))

                text = captured["payload"]["text"]
                self.assertNotIn("api-token", text)
                self.assertNotIn("cookie=value", text)
                self.assertIn("&lt;invalid&gt;&amp;response", text)
                self.assertEqual(captured["timeout"], (1.0, 2.0))
                self.assertIsNone(captured["proxies"])
            finally:
                handler.close()

    def test_proxy_is_used_and_automatically_redacted(self):
        delivered = threading.Event()
        captured = {}
        proxy_url = "socks5://proxy-user:proxy-password@127.0.0.1:1080"

        def post(url, *, json, timeout, proxies):
            captured.update(payload=json, proxies=proxies)
            delivered.set()
            return _Response()

        with patch("utils.logger.requests.post", side_effect=post):
            handler = APINotificationHandler(
                "bot-token",
                1,
                proxy_url=proxy_url,
            )
            try:
                handler.emit(_record(f"proxy failed: {proxy_url}"))
                self.assertTrue(delivered.wait(timeout=0.5))

                self.assertEqual(
                    captured["proxies"],
                    {"http": proxy_url, "https": proxy_url},
                )
                self.assertNotIn(proxy_url, captured["payload"]["text"])
            finally:
                handler.close()

    def test_overlapping_secrets_are_redacted_longest_first(self):
        delivered = threading.Event()
        captured = {}

        def post(url, *, json, timeout, proxies):
            captured["payload"] = json
            delivered.set()
            return _Response()

        with patch("utils.logger.requests.post", side_effect=post):
            handler = APINotificationHandler(
                "bot-token",
                1,
                redactions=("short", "short-with-secret-suffix"),
            )
            try:
                handler.emit(_record("short-with-secret-suffix"))
                self.assertTrue(delivered.wait(timeout=0.5))

                text = captured["payload"]["text"]
                self.assertNotIn("secret-suffix", text)
                self.assertIn("[REDACTED]", text)
            finally:
                handler.close()

    def test_long_html_message_stays_within_telegram_limit(self):
        delivered = threading.Event()
        captured = {}

        def post(url, *, json, timeout, proxies):
            captured["payload"] = json
            delivered.set()
            return _Response()

        with patch("utils.logger.requests.post", side_effect=post):
            handler = APINotificationHandler("bot-token", 1)
            try:
                handler.emit(_record(("<&😀" * 2000)))
                self.assertTrue(delivered.wait(timeout=0.5))

                text = captured["payload"]["text"]
                self.assertLessEqual(len(text), 3520)
                self.assertTrue(text.startswith("<code>"))
                self.assertTrue(text.endswith("</code>"))
                self.assertNotRegex(text, r"&(?:a|am|amp|l|lt|g|gt)?$")
            finally:
                handler.close()

    def test_shutdown_is_bounded_and_worker_exits_after_inflight_request(self):
        request_started = threading.Event()
        release_request = threading.Event()

        def blocked_post(*args, **kwargs):
            request_started.set()
            release_request.wait()
            return _Response()

        with patch("utils.logger.requests.post", side_effect=blocked_post):
            handler = APINotificationHandler(
                "token",
                1,
                shutdown_timeout=0.01,
            )
            handler.emit(_record("panel unavailable"))
            self.assertTrue(request_started.wait(timeout=0.5))

            started_at = time.monotonic()
            self.assertFalse(handler.shutdown(timeout=0.01))
            self.assertLess(time.monotonic() - started_at, 0.1)

            release_request.set()
            self.assertTrue(_wait_until(lambda: not handler._worker.is_alive()))
            handler.close()

    def test_queue_is_bounded_and_close_is_idempotent(self):
        request_started = threading.Event()
        release_request = threading.Event()

        def blocked_post(*args, **kwargs):
            request_started.set()
            release_request.wait(timeout=2.0)
            return _Response()

        with patch("utils.logger.requests.post", side_effect=blocked_post):
            handler = APINotificationHandler(
                "token",
                1,
                queue_size=1,
                dedupe_window=0,
                rate_limit=100,
            )
            try:
                handler.emit(_record("first"))
                self.assertTrue(request_started.wait(timeout=0.5))
                for index in range(20):
                    handler.emit(_record(f"queued-{index}"))

                self.assertLessEqual(handler._queue.qsize(), 1)
                self.assertGreater(handler.dropped_notifications, 0)
            finally:
                release_request.set()
                handler.close()
                handler.close()

            handler.emit(_record("after-close"))
            self.assertGreater(handler.dropped_notifications, 0)


if __name__ == "__main__":
    unittest.main()
