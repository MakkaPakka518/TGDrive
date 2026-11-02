"""Regression checks with a fake Telegram client; no real account is used."""
import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

TEMP = tempfile.TemporaryDirectory()
os.environ.update(TG_API_ID="123", TG_API_HASH="test", SERVICE_TOKEN="test", MEDIA_SECRET="test", SESSION_ENCRYPTION_KEY=Fernet.generate_key().decode(), PUBLIC_BASE="https://api.example.test", FRONTEND_ORIGIN="https://drive.example.test", DATA_DIR=TEMP.name)

import app
from starlette.requests import Request
from telethon.tl.types import DocumentAttributeFilename
from telethon.tl.types.auth import SentCode, SentCodeTypeApp
from telethon.errors import FloodWaitError, PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError


class DriveTests(unittest.IsolatedAsyncioTestCase):
    async def test_cors_uses_configured_frontend(self):
        response = await app.secure_headers(None, AsyncMock(return_value=app.Response()))
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://drive.example.test")

    def test_ranges(self):
        for value, expected in [(None, (0, 99, False)), ("bytes=0-0", (0, 0, True)), ("bytes=42-", (42, 99, True)), ("bytes=-10", (90, 99, True)), ("bytes=90-200", (90, 99, True))]:
            self.assertEqual(app.range_bounds(value, 100), expected)
        for value in ["bytes=-", "bytes=-0", "bytes=100-", "bytes=20-10", "bytes=0-1,3-4", "bad"]:
            with self.assertRaises(app.HTTPException) as result:
                app.range_bounds(value, 100)
            self.assertEqual(result.exception.status_code, 416)
            self.assertEqual(result.exception.headers["Content-Range"], "bytes */100")

    def test_signatures_bind_channel(self):
        with patch.object(app, "selected_channel_id", return_value=-1001):
            expires, signature = app.sign_media(42)
            self.assertTrue(app.verify_media(42, expires, signature))
            self.assertFalse(app.verify_media(43, expires, signature))
            self.assertFalse(app.verify_media(42, expires, signature, "thumb"))
            self.assertFalse(app.verify_media(42, int(time.time()) - 1, signature))
        with patch.object(app, "selected_channel_id", return_value=-1002):
            self.assertFalse(app.verify_media(42, expires, signature))

    async def test_media_bytes_head_and_unicode_filename(self):
        payload = bytes(range(256)) * 4000
        message = SimpleNamespace(id=42, document=SimpleNamespace(mime_type="video/mp4", size=len(payload), attributes=[DocumentAttributeFilename('中文\r\n".mp4')]), media=object(), message="  \n ", date=datetime.now(timezone.utc))
        offsets = []

        async def download(media, offset, request_size):
            offsets.append(offset)
            for start in range(offset, len(payload), request_size):
                yield payload[start:start + request_size]

        client = SimpleNamespace(iter_download=download)
        with patch.object(app, "get_message", AsyncMock(return_value=(client, message))), patch.object(app, "selected_channel_id", return_value=-1001):
            exp, sig = app.sign_media(42)
            for method, range_value, expected in [("GET", "bytes=123-700123", payload[123:700124]), ("GET", "bytes=-10", payload[-10:]), ("GET", "bytes=0-0", payload[:1]), ("HEAD", "bytes=10-19", b"")]:
                request = Request({"type": "http", "method": method, "headers": [(b"range", range_value.encode())]})
                response = await app.media(42, request, exp, sig)
                self.assertEqual(response.status_code, 206)
                self.assertIn("filename*=UTF-8''", response.headers["content-disposition"])
                self.assertNotIn("\r", response.headers["content-disposition"])
                actual = b"" if method == "HEAD" else b"".join([chunk async for chunk in response.body_iterator])
                self.assertEqual(actual, expected)
                self.assertEqual(int(response.headers["content-length"]), 10 if method == "HEAD" else len(expected))
            self.assertEqual(offsets, [123, len(payload) - 10, 0])

    async def test_completed_login_cleanup_keeps_active_client(self):
        client = SimpleNamespace(disconnect=AsyncMock())
        pending = SimpleNamespace(disconnect=AsyncMock())
        with patch.object(app, "main_client", client), patch.object(app, "login_sessions", {
            "completed": {"client": client, "expires": 0},
            "expired": {"client": pending, "expires": 0},
        }):
            app.cleanup_login_sessions()
            await asyncio.sleep(0)
            client.disconnect.assert_not_called()
            pending.disconnect.assert_awaited_once()
            self.assertFalse(app.login_sessions)


class PhoneLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(),
            send_code_request=AsyncMock(return_value=SentCode(type=SentCodeTypeApp(length=5), phone_code_hash="test-hash-1", timeout=60)),
            sign_in=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=1, first_name="Test", last_name="User", username="test")),
            session=SimpleNamespace(save=lambda: "encrypted-test-session"),
        )
        for name, value in [("login_sessions", {}), ("phone_retry_at", {}), ("main_client", None), ("login_start_lock", asyncio.Lock()), ("client_lock", asyncio.Lock()), ("get_client", AsyncMock(return_value=None)), ("TelegramClient", lambda *args, **kwargs: self.client)]:
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def send(self):
        return await app.login_send_code(app.PhoneBody(phone="+1 (555) 555-0100"), authorization="Bearer test")

    async def test_code_retry_two_factor_and_encrypted_persistence(self):
        sent = await self.send()
        key = sent["login_id"]
        self.assertEqual(sent["status"], "code_sent")
        self.assertEqual(sent["delivery"], "telegram")
        self.assertNotIn("phone_code_hash", sent)
        self.client.send_code_request.assert_awaited_once_with("+15555550100")
        restored = await app.login_status(key, authorization="test")
        self.assertEqual(restored["method"], "phone")
        self.assertEqual(restored["phone_masked"], "+15****0100")
        self.client.sign_in.side_effect = [PhoneCodeInvalidError(None), SessionPasswordNeededError(None), PasswordHashInvalidError(None), None]
        with self.assertRaises(app.HTTPException) as invalid:
            await app.login_verify_code(key, app.CodeBody(code="00000"), authorization="test")
        self.assertEqual(invalid.exception.detail, "PhoneCodeInvalidError")
        self.assertEqual(app.login_sessions[key]["status"], "code_sent")
        step = await app.login_verify_code(key, app.CodeBody(code="12 345"), authorization="test")
        self.assertEqual(step["status"], "password_required")
        self.client.sign_in.assert_awaited_with(phone="+15555550100", code="12345", phone_code_hash="test-hash-1")
        with self.assertRaises(app.HTTPException) as wrong_password:
            await app.login_password(key, app.PasswordBody(password="wrong"), authorization="test")
        self.assertEqual(wrong_password.exception.detail, "PasswordHashInvalidError")
        result = await app.login_password(key, app.PasswordBody(password="correct"), authorization="test")
        self.assertEqual(result["status"], "completed")
        self.assertIs(app.main_client, self.client)
        self.assertEqual(app.load_session(), "encrypted-test-session")
        self.assertNotIn(b"encrypted-test-session", app.SESSION_FILE.read_bytes())

    async def test_code_only_login(self):
        sent = await self.send()
        result = await app.login_verify_code(sent["login_id"], app.CodeBody(code="12345"), authorization="test")
        self.assertEqual(result["status"], "completed")
        self.assertIs(app.main_client, self.client)

    async def test_resend_reuses_session_and_updates_code_hash(self):
        sent = await self.send()
        key = sent["login_id"]
        with self.assertRaises(app.HTTPException) as cooldown:
            await app.login_send_code(app.PhoneBody(), login_id=key, authorization="test")
        self.assertEqual(cooldown.exception.status_code, 429)
        self.assertIn("Retry-After", cooldown.exception.headers)
        self.assertEqual(self.client.send_code_request.await_count, 1)
        self.client.send_code_request.return_value = SentCode(type=SentCodeTypeApp(length=5), phone_code_hash="test-hash-2", timeout=60)
        later = time.time() + 62
        with patch.object(app.time, "time", return_value=later):
            resent = await app.login_send_code(app.PhoneBody(), login_id=key, authorization="test")
        self.assertEqual(resent["login_id"], key)
        self.assertEqual(len(app.login_sessions), 1)
        await app.login_verify_code(key, app.CodeBody(code="12345"), authorization="test")
        self.client.sign_in.assert_awaited_with(phone="+15555550100", code="12345", phone_code_hash="test-hash-2")

    async def test_expired_code_and_session(self):
        sent = await self.send()
        key = sent["login_id"]
        self.client.sign_in.side_effect = PhoneCodeExpiredError(None)
        with self.assertRaises(app.HTTPException) as expired:
            await app.login_verify_code(key, app.CodeBody(code="12345"), authorization="test")
        self.assertEqual(expired.exception.detail, "PhoneCodeExpiredError")
        self.assertEqual(app.login_sessions[key]["status"], "expired")
        app.login_sessions[key]["expires"] = 0
        with self.assertRaises(app.HTTPException) as missing:
            await app.login_verify_code(key, app.CodeBody(code="12345"), authorization="test")
        self.assertEqual(missing.exception.status_code, 404)
        await asyncio.sleep(0)
        self.client.disconnect.assert_awaited_once()

    async def test_flood_wait_and_invalid_phone_do_not_leak_clients(self):
        with self.assertRaises(app.HTTPException) as invalid:
            await app.login_send_code(app.PhoneBody(phone="++8612345678900"), authorization="test")
        self.assertEqual(invalid.exception.status_code, 400)
        self.client.connect.assert_not_awaited()
        self.client.send_code_request.side_effect = FloodWaitError(None, capture=120)
        with self.assertRaises(app.HTTPException) as flood:
            await self.send()
        self.assertEqual(flood.exception.status_code, 429)
        self.assertEqual(flood.exception.headers["Retry-After"], "120")
        self.assertFalse(app.login_sessions)
        self.client.disconnect.assert_awaited_once()
        with self.assertRaises(app.HTTPException):
            await self.send()
        self.assertEqual(self.client.send_code_request.await_count, 1)


if __name__ == "__main__":
    unittest.main()
