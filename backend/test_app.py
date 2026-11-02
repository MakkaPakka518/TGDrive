"""Regression checks with a fake Telegram client; no real account is used."""
import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet

TEMP = tempfile.TemporaryDirectory()
os.environ.update(TG_API_ID="123", TG_API_HASH="test", SERVICE_TOKEN="test", MEDIA_SECRET="test", SESSION_ENCRYPTION_KEY=Fernet.generate_key().decode(), PUBLIC_BASE="https://api.example.test", FRONTEND_ORIGIN="https://drive.example.test", DATA_DIR=TEMP.name)

import app
from starlette.requests import Request
from telethon.tl.types import DocumentAttributeFilename
from telethon.tl.types.auth import SentCode, SentCodeTypeApp
from telethon.errors import FloodWaitError, PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError
from storage import Store


class BackendCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(__import__("pathlib").Path(self.directory.name) / "test.db")
        self.addCleanup(self.store.close)
        for name, value in [("store", self.store), ("clients", {}), ("client_locks", {}), ("source_jobs", {}), ("account_jobs", {}), ("dialog_cache", {}), ("dialog_locks", {}), ("thumb_cache", {}), ("index_slots", asyncio.Semaphore(3))]:
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def add_account(self, account_id="a" * 24, user_id="1"):
        self.store.save_account(account_id, {"id": user_id, "name": "Test account"}, app.FERNET.encrypt(b"test-session"))
        return account_id

    def add_source(self, account_id=None, chat_id=-123):
        account_id = account_id or self.add_account()
        return self.store.add_source(account_id, chat_id, "Test group", "", "group", {"type": "chat", "id": abs(chat_id)})


class DriveTests(BackendCase):
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

    def test_signatures_bind_account_and_source(self):
        first = self.add_source()
        second = self.add_source(self.add_account("b" * 24, "2"))
        expires, signature = app.sign_media(first, 42)
        self.assertTrue(app.verify_media(first, 42, expires, signature))
        self.assertFalse(app.verify_media(second, 42, expires, signature))
        self.assertFalse(app.verify_media(first, 43, expires, signature))
        self.assertFalse(app.verify_media(first, 42, expires, signature, "thumb"))
        self.assertFalse(app.verify_media(first, 42, int(time.time()) - 1, signature))
        self.store.remove_source(first)
        self.assertFalse(app.verify_media(first, 42, expires, signature))

    async def test_media_bytes_head_and_unicode_filename(self):
        payload = bytes(range(256)) * 4000
        message = SimpleNamespace(id=42, document=SimpleNamespace(mime_type="video/mp4", size=len(payload), attributes=[DocumentAttributeFilename('中文\r\n".mp4')]), media=object(), message="  \n ", date=datetime.now(timezone.utc))
        offsets = []

        async def download(media, offset, request_size):
            offsets.append(offset)
            for start in range(offset, len(payload), request_size):
                yield payload[start:start + request_size]

        client = SimpleNamespace(iter_download=download)
        source_id = self.add_source()
        with patch.object(app, "get_message", AsyncMock(return_value=(client, message))):
            exp, sig = app.sign_media(source_id, 42)
            for method, range_value, expected in [("GET", "bytes=123-700123", payload[123:700124]), ("GET", "bytes=-10", payload[-10:]), ("GET", "bytes=0-0", payload[:1]), ("HEAD", "bytes=10-19", b"")]:
                request = Request({"type": "http", "method": method, "headers": [(b"range", range_value.encode())]})
                response = await app.media(source_id, 42, request, exp, sig)
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
        with patch.object(app, "clients", {"active": client}), patch.object(app, "login_sessions", {
            "completed": {"client": client, "expires": 0},
            "expired": {"client": pending, "expires": 0},
        }):
            app.cleanup_login_sessions()
            await asyncio.sleep(0)
            client.disconnect.assert_not_called()
            pending.disconnect.assert_awaited_once()
            self.assertFalse(app.login_sessions)

    async def test_download_attachment_range_and_separate_signature(self):
        source_id = self.add_source()
        payload = b"0123456789" * 100
        message = SimpleNamespace(id=42, document=SimpleNamespace(mime_type="video/mp4", size=len(payload), attributes=[DocumentAttributeFilename("测试视频.mp4")]), media=object(), message="", date=datetime.now(timezone.utc))

        async def chunks(media, offset, request_size):
            yield payload[offset:]

        client = SimpleNamespace(iter_download=chunks)
        request = Request({"type": "http", "method": "GET", "headers": [(b"range", b"bytes=100-199")]})
        with patch.object(app, "get_message", AsyncMock(return_value=(client, message))):
            media_exp, media_sig = app.sign_media(source_id, 42)
            with self.assertRaises(app.HTTPException) as forbidden:
                await app.download(source_id, 42, request, media_exp, media_sig)
            self.assertEqual(forbidden.exception.status_code, 403)
            link = await app.download_link(source_id, 42, authorization="test")
            params = parse_qs(urlparse(link["download_url"]).query)
            response = await app.download(source_id, 42, request, int(params["exp"][0]), params["sig"][0])
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.headers["content-type"], "application/octet-stream")
            self.assertTrue(response.headers["content-disposition"].startswith("attachment; filename*=UTF-8''"))
            self.assertEqual(response.headers["content-range"], "bytes 100-199/1000")
            self.assertEqual(b"".join([chunk async for chunk in response.body_iterator]), payload[100:200])


class PhoneLoginTests(BackendCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(),
            send_code_request=AsyncMock(return_value=SentCode(type=SentCodeTypeApp(length=5), phone_code_hash="test-hash-1", timeout=60)),
            sign_in=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=1, first_name="Test", last_name="User", username="test")),
            session=SimpleNamespace(save=lambda: "encrypted-test-session"),
        )
        for name, value in [("login_sessions", {}), ("phone_retry_at", {}), ("login_start_lock", asyncio.Lock()), ("TelegramClient", lambda *args, **kwargs: self.client)]:
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
        self.assertIs(app.clients[result["account_id"]], self.client)
        encrypted = self.store.account(result["account_id"])["session"]
        self.assertEqual(app.FERNET.decrypt(encrypted), b"encrypted-test-session")
        self.assertNotIn(b"encrypted-test-session", encrypted)

    async def test_code_only_login(self):
        sent = await self.send()
        result = await app.login_verify_code(sent["login_id"], app.CodeBody(code="12345"), authorization="test")
        self.assertEqual(result["status"], "completed")
        self.assertIs(app.clients[result["account_id"]], self.client)

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


class MultiSourceTests(BackendCase):
    async def test_music_catalog_filters_audio_before_pagination(self):
        source_id = self.add_source()
        items = [{"id": number, "kind": "audio" if number % 2 else "video", "mime": "audio/WAV" if number % 2 else "video/mp4", "title": "item", "date": "2026-01-01"} for number in range(1, 101)]
        self.store.index_batch(source_id, items)
        with patch.object(app, "MUSIC_SERVICE_TOKEN", "audio-scope"):
            response = await app.music_catalog(page=2, authorization="Bearer audio-scope")
            self.assertEqual(response["total"], 50)
            self.assertEqual(len(response["items"]), 20)
            self.assertTrue(all(item["kind"] == "audio" and "account_id" not in item for item in response["items"]))
            sources = await app.music_sources(authorization="audio-scope")
            self.assertEqual(sources["sources"][0]["audio_count"], 50)
            self.assertNotIn("account_id", sources["sources"][0])
            with self.assertRaises(app.HTTPException):
                app.require_auth("audio-scope")
            with self.assertRaises(app.HTTPException):
                await app.music_catalog(authorization="test")

    async def test_music_stream_rejects_video_and_other_signatures(self):
        source_id = self.add_source()
        message = SimpleNamespace(id=1, document=SimpleNamespace(mime_type="video/mp4", size=10, attributes=[]), message="", date=None)
        request = Request({"type": "http", "method": "HEAD", "headers": []})
        with patch.object(app, "get_message", AsyncMock(return_value=(object(), message))):
            expires, signature = app.sign_media(source_id, 1, "music")
            with self.assertRaises(app.HTTPException) as rejected:
                await app.music_media(source_id, 1, request, expires, signature)
            self.assertEqual(rejected.exception.status_code, 415)
            message.document.mime_type = "audio/WAV"
            response = await app.music_media(source_id, 1, request, expires, signature)
            self.assertEqual(response.headers["content-type"], "audio/wav")
            other_exp, other_sig = app.sign_media(source_id, 1)
            with self.assertRaises(app.HTTPException) as invalid:
                await app.music_media(source_id, 1, request, other_exp, other_sig)
            self.assertEqual(invalid.exception.status_code, 403)

    async def test_music_cors_only_allows_configured_origins(self):
        with patch.object(app, "MUSIC_FRONTEND_ORIGIN", "https://music.example.test"):
            for origin, allowed in [("https://music.example.test", True), ("https://drive.example.test", True), ("https://other.example.test", False)]:
                request = Request({"type": "http", "method": "GET", "headers": [(b"origin", origin.encode())]})
                response = await app.secure_headers(request, AsyncMock(return_value=app.Response()))
                self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), origin if allowed else None)

    def test_legacy_migration_is_idempotent(self):
        encrypted = app.FERNET.encrypt(b"legacy-test-session")
        self.store.import_legacy(encrypted, {"channel_id": -100123, "title": "Legacy source"})
        self.store.import_legacy(encrypted, {"channel_id": -100123})
        accounts = self.store.accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(self.store.account(accounts[0]["id"])["session"], encrypted)
        self.assertEqual(len(self.store.sources()), 1)
        self.store.remove_account(accounts[0]["id"])
        self.store.import_legacy(encrypted, {"channel_id": -100123})
        self.assertEqual(self.store.accounts(), [], "Logged-out legacy session must not be imported again")

    def test_pagination_beyond_200_and_search_across_sources(self):
        first = self.add_source()
        second = self.add_source(self.add_account("b" * 24, "2"))
        items = [{"id": number, "title": "video " + str(number), "date": f"2026-01-01T00:{number // 60:02}:{number % 60:02}+00:00", "file_name": "test.mp4"} for number in range(1, 236)]
        self.store.index_batch(first, items)
        self.store.index_batch(second, [{"id": 1, "title": "unique song", "date": "2027-01-01T00:00:00+00:00", "file_name": "100%_song.mp3"}])
        page = self.store.catalog(8)
        self.assertEqual(page["page_size"], 30)
        self.assertEqual(page["total"], 236)
        self.assertEqual(len(page["items"]), 26)
        self.assertFalse(page["has_next"])
        self.assertEqual(self.store.catalog(1, query="100%_")["total"], 1)
        self.assertEqual(self.store.catalog(1, source_id=first)["total"], 235)
        self.assertEqual(len(self.store.catalog(1)["items"]), 30)
        self.store.remove_account("b" * 24)
        self.assertEqual(self.store.catalog()["total"], 235)

    async def test_index_resumes_and_reads_entire_history(self):
        source_id = self.add_source()
        document = SimpleNamespace(mime_type="video/mp4", size=100, attributes=[DocumentAttributeFilename("test.mp4")])
        messages = [SimpleNamespace(id=number, document=document, message="Video", date=datetime.now(timezone.utc)) for number in range(350, 0, -1)]
        calls = []
        interrupt = True

        async def iterate(peer, limit, offset_id=0, min_id=0):
            calls.append((offset_id, min_id))
            if interrupt and len(calls) == 3:
                raise asyncio.CancelledError()
            eligible = [message for message in messages if (not offset_id or message.id < offset_id) and message.id > min_id]
            for message in eligible[:limit]:
                yield message

        with patch.object(app, "get_client", AsyncMock(return_value=SimpleNamespace(iter_messages=iterate))):
            with self.assertRaises(asyncio.CancelledError):
                await app.sync_source(source_id)
            self.assertEqual(self.store.catalog()["total"], 200)
            self.assertEqual(self.store.source(source_id)["cursor"], 151)
            interrupt = False
            await app.sync_source(source_id)
        self.assertEqual(self.store.catalog()["total"], 350)
        self.assertTrue(self.store.source(source_id)["history_done"])
        self.assertEqual(len(self.store.catalog(12)["items"]), 20)

    async def test_background_tick_honors_and_recovers_from_flood_wait(self):
        source_id = self.add_source()
        self.store.source_error(source_id, "wait", time.time() + 30)
        with patch.object(app, "sync_source", AsyncMock()) as sync:
            app.sync_tick()
            self.assertNotIn(source_id, app.source_jobs)
            self.store.source_error(source_id, "wait", time.time() - 1)
            app.sync_tick()
            await app.source_jobs[source_id]
            sync.assert_awaited_once_with(source_id)

    async def test_dialog_search_and_basic_groups(self):
        account = self.add_account()
        entries = [{"id": -number, "title": "普通群 " + str(number), "username": "", "kind": "group", "peer": {"type": "chat", "id": number}} for number in range(1, 351)]
        with patch.object(app, "get_dialog_entries", AsyncMock(return_value=entries)), patch.object(app, "schedule_sync"):
            result = await app.dialogs(account, q="350", authorization="test")
            self.assertEqual(result["total"], 1)
            self.assertNotIn("peer", result["items"][0])
            result = await app.add_sources(app.SourcesBody(account_id=account, chat_ids=[-1, -350]), authorization="test")
            self.assertEqual(len(result["sources"]), 2)
            self.assertTrue(all(source["kind"] == "group" for source in result["sources"]))

    async def test_logout_only_removes_target_account(self):
        first, second = self.add_account(), self.add_account("b" * 24, "2")
        source_id = self.add_source(first)
        self.add_source(second)
        client = SimpleNamespace(is_connected=lambda: True, log_out=AsyncMock(return_value=True))
        other = SimpleNamespace(is_connected=lambda: True, log_out=AsyncMock())
        app.clients.update({first: client, second: other})
        await app.account_logout(first, authorization="test")
        client.log_out.assert_awaited_once()
        other.log_out.assert_not_awaited()
        self.assertIsNone(self.store.account(first))
        self.assertIsNone(self.store.source(source_id))
        self.assertIs(app.clients[second], other)

    async def test_adding_account_preserves_existing_client(self):
        first = self.add_account()
        other = SimpleNamespace(disconnect=AsyncMock())
        app.clients[first] = other
        new = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=2, first_name="Second", last_name=None, username="second")), session=SimpleNamespace(save=lambda: "new-test-session"))
        with patch.object(app, "schedule_sync"):
            await app.finish_login("new-login", new)
        self.assertEqual(len(self.store.accounts()), 2)
        self.assertIs(app.clients[first], other)
        other.disconnect.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
