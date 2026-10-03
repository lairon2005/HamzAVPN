"""Email остаётся опциональным; браузер не получает секрет подтверждения."""
import importlib.util
import os
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jose import jwt
from starlette.requests import Request

from payment_test_support import load_models


PROJECT = Path(__file__).resolve().parents[1]
TEST_KEY = "hamzavpn-email-security-test-key-only"


def load_mail(values=None):
    spec = importlib.util.spec_from_file_location("optional_mail_under_test", PROJECT / "webapp/core/mail.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, values or {}, clear=True):
        spec.loader.exec_module(module)
    return module


def load_auth():
    models = load_models()
    mail = load_mail()
    mail.send_verification_email = AsyncMock()
    mail.send_reset_code = AsyncMock()
    security = types.ModuleType("webapp.core.security")
    security.SECRET_KEY = TEST_KEY
    security.ALGORITHM = "HS256"
    security.ACCESS_TOKEN_EXPIRE_MINUTES = 60
    security.get_password_hash = lambda password: "test-password-hash"
    security.verify_password = lambda plain, digest: plain == "correct-password"
    security.create_access_token = lambda data, **kwargs: jwt.encode(data, TEST_KEY, algorithm="HS256")
    dependencies = types.ModuleType("webapp.dependencies")
    dependencies.get_current_user = AsyncMock(return_value=None)
    spec = importlib.util.spec_from_file_location("email_auth_under_test", PROJECT / "webapp/routers/auth.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"db": models, "webapp.core.mail": mail,
                                  "webapp.core.security": security, "webapp.dependencies": dependencies}):
        spec.loader.exec_module(module)
    return module, mail


def request():
    return Request({"type": "http", "method": "POST", "path": "/register", "headers": [],
                    "query_string": b"", "scheme": "https", "server": ("testserver", 443)})


def session_with_results(*users):
    session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), add=Mock())
    session.execute.side_effect = [SimpleNamespace(scalar_one_or_none=lambda user=user: user) for user in users]
    return session


class OptionalMailTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_smtp_imports_and_fails_only_when_sending(self):
        module = load_mail({"MAIL_FROM": "", "MAIL_SERVER": "", "MAIL_PORT": "587"})
        self.assertIsNone(module.conf)
        with patch.object(module, "FastMail") as transport:
            with self.assertRaisesRegex(module.MailSendError, "почтовый сервер не настроен"):
                await module.send_verification_email("user@example.com", "654321")
            transport.assert_not_called()

    async def test_partial_credentials_do_not_invent_sender(self):
        module = load_mail({"MAIL_SERVER": "smtp.example.com", "MAIL_USERNAME": "sender", "MAIL_PASSWORD": "test"})
        self.assertIsNone(module.conf)

    async def test_invalid_complete_smtp_does_not_break_import(self):
        with self.assertLogs("optional_mail_under_test", level="WARNING"):
            module = load_mail({"MAIL_SERVER": "smtp.example.com", "MAIL_FROM": "invalid-address",
                                "MAIL_USERNAME": "sender", "MAIL_PASSWORD": "test-password"})
        self.assertIsNone(module.conf)

    async def test_configured_smtp_keeps_tls_and_certificate_validation(self):
        values = {"MAIL_SERVER": "smtp.example.com", "MAIL_FROM": "sender@example.com",
                  "MAIL_USERNAME": "sender", "MAIL_PASSWORD": "test-password"}
        for port, starttls in ((587, True), (465, False)):
            with self.subTest(port=port):
                module = load_mail(dict(values, MAIL_PORT=str(port)))
                self.assertIsNotNone(module.conf)
                self.assertEqual(module.conf.MAIL_STARTTLS, starttls)
                self.assertEqual(module.conf.MAIL_SSL_TLS, not starttls)
                self.assertTrue(module.conf.VALIDATE_CERTS)
                self.assertTrue(module.conf.USE_CREDENTIALS)


class RegistrationCodeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth, self.mail = load_auth()

    async def register(self):
        with patch.object(self.auth, "_new_email_code", return_value="654321"):
            response = await self.auth.register_user(request(), email="user@example.com", password="correct-password",
                                                     full_name="Test User", ref=None, db=session_with_results(None))
        token = response.context["registration_token"]
        return token, jwt.get_unverified_claims(token)

    async def test_registration_token_cannot_disclose_or_offline_test_code(self):
        token, payload = await self.register()
        self.assertNotIn("code", payload)
        self.assertNotIn("654321", str(payload))
        self.assertEqual(payload["registration_version"], 2)
        self.mail.send_verification_email.assert_awaited_once_with("user@example.com", "654321")
        self.assertTrue(self.auth._registration_code_matches(payload, "654321"))
        self.assertFalse(self.auth._registration_code_matches(payload, "654322"))
        with patch.object(self.auth, "SECRET_KEY", "another-secret-key-for-test-only"):
            self.assertFalse(self.auth._registration_code_matches(payload, "654321"))
        changed_email = dict(payload, email="other@example.com")
        self.assertFalse(self.auth._registration_code_matches(changed_email, "654321"))

    async def test_registration_without_smtp_returns_error_without_creating_account(self):
        smtp = load_mail()
        session = session_with_results(None)
        with patch.object(self.auth, "send_verification_email", smtp.send_verification_email), \
                patch.object(self.auth, "MailSendError", smtp.MailSendError):
            response = await self.auth.register_user(request(), email="user@example.com", password="correct-password",
                                                     full_name="Test User", ref=None, db=session)
        self.assertIn("почтовый сервер не настроен", response.context["error"])
        self.assertNotIn("registration_token", response.context)
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    async def test_wrong_code_does_not_create_user_and_digest_uses_constant_time_comparison(self):
        token, payload = await self.register()
        session = session_with_results()
        with patch.object(self.auth.hmac, "compare_digest", wraps=self.auth.hmac.compare_digest) as comparison:
            response = await self.auth.verify_email(request(), token, "000000", session)
        comparison.assert_called_once()
        self.assertIn("Неверный код", response.context["error"])
        self.assertNotIn("code", jwt.get_unverified_claims(response.context["registration_token"]))
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    async def test_old_plaintext_tokens_are_rejected_by_verify_and_resend(self):
        old = jwt.encode({"type": "registration", "email": "user@example.com", "code": "654321",
                          "exp": datetime.utcnow() + timedelta(hours=1)}, TEST_KEY, algorithm="HS256")
        session = session_with_results()
        verify = await self.auth.verify_email(request(), old, "654321", session)
        resend = await self.auth.resend_code(request(), old)
        self.assertIn("Недействительный", verify.context["error"])
        self.assertIn("Недействительный", resend.context["error"])
        session.add.assert_not_called()
        self.mail.send_verification_email.assert_not_awaited()

    async def test_resend_only_publishes_new_digest(self):
        _, payload = await self.register()
        payload["code_sent_at"] = (datetime.utcnow() - timedelta(minutes=3)).isoformat()
        token = jwt.encode(payload, TEST_KEY, algorithm="HS256")
        with patch.object(self.auth, "_new_email_code", return_value="123456"):
            response = await self.auth.resend_code(request(), token)
        renewed = jwt.get_unverified_claims(response.context["registration_token"])
        self.assertNotIn("code", renewed)
        self.assertNotIn("123456", str(renewed))
        self.assertNotEqual(payload["code_nonce"], renewed["code_nonce"])
        self.assertFalse(self.auth._registration_code_matches(renewed, "654321"))
        self.assertTrue(self.auth._registration_code_matches(renewed, "123456"))
        self.mail.send_verification_email.assert_awaited_with("user@example.com", "123456")

    async def test_verified_registration_sets_secure_cookie(self):
        token, _ = await self.register()
        session = session_with_results(None)
        with patch.object(self.auth, "generate_web_user_id", AsyncMock(return_value=-42)):
            response = await self.auth.verify_email(request(), token, "654321", session)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(session.add.call_args.args[0].email, "user@example.com")
        cookie = response.headers["set-cookie"]
        for expected in ("HttpOnly", "Secure", "SameSite=lax"):
            self.assertIn(expected, cookie)

    async def test_password_login_sets_secure_cookie(self):
        session = session_with_results(SimpleNamespace(user_id=-42, email="user@example.com", password_hash="hash"))
        response = await self.auth.login_user(request(), "user@example.com", "correct-password", session)
        self.assertEqual(response.status_code, 302)
        for expected in ("HttpOnly", "Secure", "SameSite=lax"):
            self.assertIn(expected, response.headers["set-cookie"])
