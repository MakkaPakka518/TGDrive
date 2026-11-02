import asyncio
import base64
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import time
from datetime import timezone
from pathlib import Path
from typing import List
from urllib.parse import quote

import qrcode
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    RPCError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    InputPeerChannel,
    InputPeerChat,
)
from storage import PAGE_SIZE, Store


API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SERVICE_TOKEN = os.environ["SERVICE_TOKEN"]
MUSIC_SERVICE_TOKEN = os.environ.get("MUSIC_SERVICE_TOKEN", "")
MEDIA_SECRET = os.environ["MEDIA_SECRET"].encode()
FERNET = Fernet(os.environ["SESSION_ENCRYPTION_KEY"].encode())
PUBLIC_BASE = os.environ["PUBLIC_BASE"].rstrip("/")
FRONTEND_ORIGIN = os.environ["FRONTEND_ORIGIN"].rstrip("/")
MUSIC_FRONTEND_ORIGIN = os.environ.get("MUSIC_FRONTEND_ORIGIN", "").rstrip("/")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
SESSION_FILE = DATA_DIR / "telegram.session.enc"
CONFIG_FILE = DATA_DIR / "config.json"
URL_TTL = 12 * 60 * 60

DATA_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(DATA_DIR, 0o700)
store = Store(DATA_DIR / "tgdrive.db")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
login_sessions = {}
login_start_lock = asyncio.Lock()
phone_retry_at = {}
client_locks = {}
clients = {}
download_slots = asyncio.Semaphore(2)
index_slots = asyncio.Semaphore(3)
source_jobs = {}
account_jobs = {}
dialog_cache = {}
dialog_locks = {}
maintenance_job = None
thumb_cache = {}


class PasswordBody(BaseModel):
    password: str


class PhoneBody(BaseModel):
    phone: str = ""


class CodeBody(BaseModel):
    code: str


class SourcesBody(BaseModel):
    account_id: str
    chat_ids: List[int]


class SyncBody(BaseModel):
    reset: bool = False


def require_auth(authorization):
    supplied = str(authorization or "").strip()
    if supplied.lower().startswith("bearer "):
        supplied = supplied[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, SERVICE_TOKEN):
        raise HTTPException(401, "unauthorized")


def require_music_auth(authorization):
    value = str(authorization or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not MUSIC_SERVICE_TOKEN or not hmac.compare_digest(value, MUSIC_SERVICE_TOKEN):
        raise HTTPException(401, "unauthorized")


def require_audio(info):
    if not info or info["kind"] != "audio" or not info["mime"].lower().startswith("audio/"):
        raise HTTPException(415, "audio_only")
    return info


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


store.import_legacy(SESSION_FILE.read_bytes() if SESSION_FILE.exists() else None, load_config())


def profile(me):
    phone = getattr(me, "phone", "") or ""
    return {
        "id": str(me.id),
        "name": " ".join(filter(None, [me.first_name, me.last_name])) or me.username or str(me.id),
        "username": me.username or "",
        "phone_masked": ("+" + phone[:2] + "****" + phone[-4:]) if phone else "",
    }


def managed_client(client):
    return any(value is client for value in clients.values())


async def get_client(account_id, required=True):
    async with client_locks.setdefault(account_id, asyncio.Lock()):
        account = store.account(account_id)
        if not account:
            raise HTTPException(404, "account_not_found")
        client = clients.get(account_id)
        if client and client.is_connected():
            return client
        try:
            if not client:
                raw = FERNET.decrypt(account["session"]).decode()
                client = TelegramClient(StringSession(raw), API_ID, API_HASH, flood_sleep_threshold=0, request_retries=1)
            await asyncio.wait_for(client.connect(), timeout=20)
            if not await asyncio.wait_for(client.is_user_authorized(), timeout=20):
                raise HTTPException(401, "telegram_login_required")
            me = await asyncio.wait_for(client.get_me(), timeout=20)
            store.save_account(account_id, profile(me), FERNET.encrypt(client.session.save().encode()))
            clients[account_id] = client
            return client
        except Exception as error:
            if client:
                await client.disconnect()
            clients.pop(account_id, None)
            store.account_error(account_id, "登录已失效，请重新添加账号" if isinstance(error, (HTTPException, InvalidToken)) else "连接失败，请重试")
            if required:
                if isinstance(error, HTTPException):
                    raise
                raise login_error(error) from None
            return None


def cleanup_login_sessions():
    now = time.time()
    stale = [key for key, value in login_sessions.items() if value["expires"] < now and not value.get("lock", asyncio.Lock()).locked()]
    for key in stale:
        session = login_sessions.pop(key, None)
        if session:
            task = session.get("task")
            if task and not task.done():
                task.cancel()
            if not managed_client(session["client"]):
                asyncio.create_task(session["client"].disconnect())
    for phone, retry_at in list(phone_retry_at.items()):
        if retry_at <= now:
            phone_retry_at.pop(phone, None)


async def discard_login(key):
    session = login_sessions.get(key)
    if not session:
        return
    async with session.setdefault("lock", asyncio.Lock()):
        login_sessions.pop(key, None)
        task = session.get("task")
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not managed_client(session["client"]):
            await session["client"].disconnect()


async def new_login_session(method, previous_id=None):
    cleanup_login_sessions()
    await discard_login(previous_id)
    if len(login_sessions) >= 3:
        raise HTTPException(429, "too_many_login_sessions")
    client = TelegramClient(StringSession(), API_ID, API_HASH, flood_sleep_threshold=0, request_retries=1)
    key = secrets.token_urlsafe(24)
    session = {"client": client, "method": method, "status": "starting", "expires": time.time() + 600, "error": "", "user": None, "lock": asyncio.Lock()}
    login_sessions[key] = session
    try:
        await asyncio.wait_for(client.connect(), timeout=20)
    except Exception:
        await discard_login(key)
        raise HTTPException(502, "Telegram连接失败，请稍后重试") from None
    return key, session


def login_error(error):
    if isinstance(error, FloodWaitError):
        return HTTPException(429, f"请求太频繁，请等 {error.seconds} 秒后重试", headers={"Retry-After": str(error.seconds)})
    if isinstance(error, RPCError):
        return HTTPException(400, type(error).__name__)
    return HTTPException(502, "Telegram暂时无法连接，请稍后重试")


def login_info(session):
    result = {"status": session["status"], "method": session.get("method", "qr"), "error": session["error"], "user": session["user"]}
    if session.get("account_id"):
        result["account_id"] = session["account_id"]
    if session.get("phone"):
        phone = session["phone"]
        result.update(phone_masked=phone[:3] + "****" + phone[-4:], delivery=session.get("delivery", "telegram"), retry_after=max(0, int(session.get("retry_at", 0) - time.time()) + 1))
    return result


def qr_data_uri(value):
    qr = qrcode.QRCode(version=None, box_size=7, border=3)
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


async def finish_login(key, client):
    me = await client.get_me()
    user = profile(me)
    existing = store.account_by_user(user["id"])
    account_id = existing["id"] if existing else secrets.token_hex(12)
    async with client_locks.setdefault(account_id, asyncio.Lock()):
        previous = clients.get(account_id)
        if previous and previous is not client:
            await cancel_account_jobs(account_id)
        store.save_account(account_id, user, FERNET.encrypt(client.session.save().encode()))
        clients[account_id] = client
        if previous and previous is not client:
            await previous.disconnect()
    dialog_cache.pop(account_id, None)
    session = login_sessions.get(key)
    if session:
        session["status"] = "completed"
        session["user"] = user
        session["account_id"] = account_id
    for source in store.sources():
        if source["account_id"] == account_id:
            schedule_sync(source["id"], force=True)


async def wait_qr(key, qr_login, client):
    try:
        await qr_login.wait()
        await finish_login(key, client)
    except SessionPasswordNeededError:
        session = login_sessions.get(key)
        if session:
            session["status"] = "password_required"
            session["expires"] = time.time() + 300
    except asyncio.TimeoutError:
        session = login_sessions.get(key)
        if session:
            session["status"] = "expired"
    except Exception as error:
        session = login_sessions.get(key)
        if session:
            session["status"] = "expired" if "expired" in str(error).lower() else "failed"
            session["error"] = type(error).__name__


def sign_media(source_id, message_id, kind="media"):
    source = store.source(source_id)
    if not source:
        raise HTTPException(404, "source_not_found")
    expires = int(time.time()) + URL_TTL
    payload = f"{source['account_id']}:{source_id}:{source['token']}:{kind}:{message_id}:{expires}"
    signature = hmac.new(MEDIA_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return expires, signature


def verify_media(source_id, message_id, expires, signature, kind="media"):
    if expires < int(time.time()) or expires > int(time.time()) + URL_TTL + 60:
        return False
    source = store.source(source_id)
    if not source:
        return False
    expected = hmac.new(MEDIA_SECRET, f"{source['account_id']}:{source_id}:{source['token']}:{kind}:{message_id}:{expires}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def media_links(source_id, message_id):
    expires, signature = sign_media(source_id, message_id)
    thumb_expires, thumb_signature = sign_media(source_id, message_id, "thumb")
    return {
        "stream_url": f"{PUBLIC_BASE}/v1/media/{source_id}/{message_id}?exp={expires}&sig={signature}",
        "thumb_url": f"{PUBLIC_BASE}/v1/thumb/{source_id}/{message_id}?exp={thumb_expires}&sig={thumb_signature}",
    }


def document_info(message):
    document = message.document
    if not document:
        return None
    mime = document.mime_type or "application/octet-stream"
    kind = "video" if mime.startswith("video/") else "audio" if mime.startswith("audio/") else ""
    file_name = ""
    duration = 0
    width = 0
    height = 0
    performer = ""
    for attr in document.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            file_name = attr.file_name or ""
        elif isinstance(attr, DocumentAttributeVideo):
            kind = "video"
            duration = int(attr.duration or 0)
            width = int(attr.w or 0)
            height = int(attr.h or 0)
        elif isinstance(attr, DocumentAttributeAudio):
            kind = "audio"
            duration = int(attr.duration or 0)
            performer = attr.performer or ""
            if not file_name and attr.title:
                file_name = attr.title
    if kind not in {"video", "audio"}:
        return None
    if not file_name:
        file_name = f"telegram-{message.id}{mimetypes.guess_extension(mime) or ''}"
    lines = (message.message or "").strip().splitlines()
    title = lines[0][:160] if lines else file_name
    return {
        "id": message.id,
        "kind": kind,
        "title": title or file_name,
        "description": (message.message or "").strip()[:500],
        "file_name": file_name,
        "mime": mime,
        "size": int(document.size or 0),
        "duration": duration,
        "width": width,
        "height": height,
        "performer": performer,
        "date": message.date.astimezone(timezone.utc).isoformat() if message.date else "",
    }


async def get_message(source_id, message_id):
    source = store.source(source_id)
    if not source:
        raise HTTPException(404, "source_not_found")
    client = await get_client(source["account_id"])
    peer = await source_peer(source, client)
    message = await client.get_messages(peer, ids=message_id)
    if not message or not message.document:
        store.remove_media(source_id, message_id)
        raise HTTPException(404, "media_not_found")
    return client, message


@app.middleware("http")
async def secure_headers(request: Request, call_next):
    origin = request.headers.get("origin", "") if request else ""
    allowed = {value for value in [FRONTEND_ORIGIN, MUSIC_FRONTEND_ORIGIN] if value}
    if request and request.method == "OPTIONS" and origin in allowed:
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "Range",
            "Access-Control-Max-Age": "600", "Vary": "Origin",
        })
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    if origin in allowed or not origin:
        response.headers["Access-Control-Allow-Origin"] = origin or FRONTEND_ORIGIN
        response.headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Accept-Ranges"
    response.headers["Vary"] = "Origin"
    return response


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/v1/status")
async def status(authorization: str = Header(None)):
    require_auth(authorization)
    accounts = store.accounts()
    return {"authorized": bool(accounts), "accounts": accounts, "sources": source_list(), "page_size": PAGE_SIZE}


@app.post("/v1/login/qr")
async def login_qr(login_id: str = None, authorization: str = Header(None)):
    require_auth(authorization)
    async with login_start_lock:
        key, session = await new_login_session("qr", login_id)
        try:
            qr_login = await asyncio.wait_for(session["client"].qr_login(), timeout=30)
        except Exception as error:
            await discard_login(key)
            raise login_error(error) from None
        session.update(qr=qr_login, status="waiting", expires=time.time() + 150)
        session["task"] = asyncio.create_task(wait_qr(key, qr_login, session["client"]))
        expires_in = max(1, int(qr_login.expires.timestamp() - time.time()))
        return {"login_id": key, "qr": qr_data_uri(qr_login.url), "expires_in": expires_in}


@app.post("/v1/login/send-code")
async def login_send_code(body: PhoneBody, login_id: str = None, authorization: str = Header(None)):
    require_auth(authorization)
    async with login_start_lock:
        cleanup_login_sessions()
        previous = login_sessions.get(login_id)
        phone = re.sub(r"[\s()-]", "", body.phone)
        if phone.startswith("+"):
            phone = phone[1:]
        if not phone and previous and previous.get("method") == "phone":
            phone = previous["phone"].lstrip("+")
        if not re.fullmatch(r"[1-9][0-9]{7,14}", phone):
            raise HTTPException(400, "手机号格式不对，请填写 +国家码和完整手机号")
        phone = "+" + phone
        retry_key = hashlib.sha256(phone.encode()).hexdigest()
        retry_after = int(phone_retry_at.get(retry_key, 0) - time.time()) + 1
        if retry_after > 0:
            raise HTTPException(429, f"请等 {retry_after} 秒后重新获取验证码", headers={"Retry-After": str(retry_after)})
        reuse = previous and previous.get("method") == "phone" and previous.get("phone") == phone and previous["status"] == "code_sent"
        if reuse:
            key, session = login_id, previous
        else:
            key, session = await new_login_session("phone", login_id)
        try:
            async with session["lock"]:
                sent = await asyncio.wait_for(session["client"].send_code_request(phone), timeout=35)
                if not getattr(sent, "phone_code_hash", None):
                    raise HTTPException(400, "Telegram未返回登录验证码，请在Telegram App中检查账号状态")
                delivery = {"SentCodeTypeApp": "telegram", "SentCodeTypeSms": "sms", "SentCodeTypeCall": "call", "SentCodeTypeEmailCode": "email"}.get(type(sent.type).__name__, "telegram")
                retry_at = time.time() + max(60, sent.timeout or 0)
                phone_retry_at[retry_key] = retry_at
                session.update(phone=phone, phone_code_hash=sent.phone_code_hash, status="code_sent", expires=time.time() + 600, retry_at=retry_at, delivery=delivery)
        except Exception as error:
            if not reuse:
                await discard_login(key)
            if isinstance(error, FloodWaitError):
                phone_retry_at[retry_key] = time.time() + error.seconds
            if isinstance(error, HTTPException):
                raise
            raise login_error(error) from None
        return {"login_id": key, **login_info(session)}


@app.post("/v1/login/verify-code")
async def login_verify_code(login_id: str, body: CodeBody, authorization: str = Header(None)):
    require_auth(authorization)
    cleanup_login_sessions()
    session = login_sessions.get(login_id)
    if not session:
        raise HTTPException(404, "login_expired")
    code = re.sub(r"[\s-]", "", body.code)
    if not re.fullmatch(r"[0-9]{4,10}", code):
        raise HTTPException(400, "请输入收到的数字验证码")
    async with session.setdefault("lock", asyncio.Lock()):
        if session.get("method") != "phone" or session["status"] != "code_sent":
            raise HTTPException(409, "code_not_required")
        try:
            await asyncio.wait_for(session["client"].sign_in(phone=session["phone"], code=code, phone_code_hash=session["phone_code_hash"]), timeout=35)
            await finish_login(login_id, session["client"])
        except SessionPasswordNeededError:
            session.update(status="password_required", expires=time.time() + 300)
        except PhoneCodeInvalidError:
            raise HTTPException(400, "PhoneCodeInvalidError") from None
        except PhoneCodeExpiredError:
            session["status"] = "expired"
            raise HTTPException(400, "PhoneCodeExpiredError") from None
        except Exception as error:
            raise login_error(error) from None
        return {"ok": True, **login_info(session)}


@app.get("/v1/login/status")
async def login_status(login_id: str, authorization: str = Header(None)):
    require_auth(authorization)
    cleanup_login_sessions()
    session = login_sessions.get(login_id)
    if not session:
        raise HTTPException(404, "login_expired")
    return login_info(session)


@app.post("/v1/login/cancel")
async def login_cancel(login_id: str, authorization: str = Header(None)):
    require_auth(authorization)
    await discard_login(login_id)
    return {"ok": True}


@app.post("/v1/login/password")
async def login_password(login_id: str, body: PasswordBody, authorization: str = Header(None)):
    require_auth(authorization)
    cleanup_login_sessions()
    session = login_sessions.get(login_id)
    if not session:
        raise HTTPException(404, "login_expired")
    async with session.setdefault("lock", asyncio.Lock()):
        if session["status"] != "password_required":
            raise HTTPException(400, "password_not_required")
        try:
            await asyncio.wait_for(session["client"].sign_in(password=body.password), timeout=35)
            await finish_login(login_id, session["client"])
            return {"ok": True, **login_info(session)}
        except Exception as error:
            raise login_error(error) from None


def peer_record(peer):
    if isinstance(peer, InputPeerChannel):
        return {"type": "channel", "id": peer.channel_id, "access_hash": str(peer.access_hash)}
    if isinstance(peer, InputPeerChat):
        return {"type": "chat", "id": peer.chat_id}
    raise HTTPException(400, "请选择群组或频道")


async def get_dialog_entries(account_id, refresh=False):
    async with dialog_locks.setdefault(account_id, asyncio.Lock()):
        cached = dialog_cache.get(account_id)
        if cached and not refresh and time.time() - cached["at"] < 300:
            return cached["items"]
        client = await get_client(account_id)

        async def collect():
            items = []
            async for dialog in client.iter_dialogs():
                if not (dialog.is_group or dialog.is_channel):
                    continue
                items.append({
                    "id": int(dialog.id), "title": dialog.name or str(dialog.id),
                    "username": getattr(dialog.entity, "username", None) or "",
                    "kind": "channel" if getattr(dialog.entity, "broadcast", False) else "supergroup" if dialog.is_channel else "group",
                    "peer": peer_record(dialog.input_entity),
                })
            return items

        try:
            items = await asyncio.wait_for(collect(), timeout=50)
        except Exception as error:
            if isinstance(error, HTTPException):
                raise
            raise login_error(error) from None
        dialog_cache[account_id] = {"at": time.time(), "items": items}
        return items


async def source_peer(source, client):
    peer = source["peer"]
    if not peer:
        entries = await get_dialog_entries(source["account_id"], refresh=True)
        entry = next((entry for entry in entries if entry["id"] == source["chat_id"]), None)
        if not entry:
            raise HTTPException(404, "该群组或频道已不可访问")
        peer = entry["peer"]
        store.add_source(source["account_id"], entry["id"], entry["title"], entry["username"], entry["kind"], peer)
    if peer["type"] == "channel":
        return InputPeerChannel(int(peer["id"]), int(peer["access_hash"]))
    return InputPeerChat(int(peer["id"]))


def source_list():
    return [{**source, "syncing": source["id"] in source_jobs and not source_jobs[source["id"]].done()} for source in store.sources()]


async def message_batch(client, peer, offset_id=0, min_id=0):
    async def collect():
        return [message async for message in client.iter_messages(peer, limit=100, offset_id=offset_id, min_id=min_id)]
    async with index_slots:
        return await asyncio.wait_for(collect(), timeout=45)


def index_messages(source_id, messages, cursor=None, done=None):
    items = []
    for message in messages:
        info = document_info(message)
        if info:
            items.append(info)
    store.index_batch(source_id, items, max((message.id for message in messages), default=0), cursor, done)


async def sync_source(source_id):
    try:
        source = store.source(source_id)
        if not source:
            return
        client = await get_client(source["account_id"])
        peer = await source_peer(source, client)

        async def recent():
            current = store.source(source_id)
            if not current or not current["latest_id"]:
                return
            boundary, offset = current["latest_id"], 0
            while True:
                messages = await message_batch(client, peer, offset, boundary)
                index_messages(source_id, messages)
                if len(messages) < 100:
                    break
                offset = messages[-1].id
                await asyncio.sleep(0.5)

        await recent()
        last_recent = time.time()
        while True:
            source = store.source(source_id)
            if not source or source["history_done"]:
                break
            messages = await message_batch(client, peer, source["cursor"])
            cursor = messages[-1].id if messages else source["cursor"]
            if messages and source["cursor"] and cursor >= source["cursor"]:
                raise RuntimeError("history_cursor_did_not_advance")
            index_messages(source_id, messages, cursor=cursor, done=len(messages) < 100)
            if time.time() - last_recent > 60:
                await recent()
                last_recent = time.time()
            await asyncio.sleep(0.5)
        store.source_error(source_id, "")
    except asyncio.CancelledError:
        raise
    except FloodWaitError as error:
        store.source_error(source_id, f"Telegram要求等待 {error.seconds} 秒后继续同步", time.time() + error.seconds)
    except Exception as error:
        detail = error.detail if isinstance(error, HTTPException) else "同步失败，请点击刷新重试（" + type(error).__name__ + "）"
        store.source_error(source_id, detail)


def schedule_sync(source_id, force=False):
    running = source_jobs.get(source_id)
    if running and not running.done():
        return
    source = store.source(source_id)
    if not source or source["retry_at"] > time.time():
        return
    if not force and not source["retry_at"] and source["last_sync"] > time.time() - 60:
        return
    task = asyncio.create_task(sync_source(source_id))
    source_jobs[source_id] = task

    def finished(done):
        if source_jobs.get(source_id) is done:
            source_jobs.pop(source_id, None)
    task.add_done_callback(finished)


def sync_tick():
    for source in store.sources():
        if source["account_status"] == "ready":
            schedule_sync(source["id"])


async def maintain_index():
    while True:
        sync_tick()
        await asyncio.sleep(5)


async def cancel_source_job(source_id):
    task = source_jobs.pop(source_id, None)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    for key in list(thumb_cache):
        if key[0] == source_id:
            thumb_cache.pop(key, None)


async def cancel_account_jobs(account_id):
    for source in store.sources():
        if source["account_id"] == account_id:
            await cancel_source_job(source["id"])


@app.get("/v1/accounts")
async def accounts(authorization: str = Header(None)):
    require_auth(authorization)
    return {"accounts": store.accounts()}


@app.post("/v1/accounts/{account_id}/logout")
async def account_logout(account_id: str, authorization: str = Header(None)):
    require_auth(authorization)
    if not store.account(account_id):
        raise HTTPException(404, "account_not_found")
    warm = account_jobs.pop(account_id, None)
    if warm and not warm.done():
        warm.cancel()
        await asyncio.gather(warm, return_exceptions=True)
    client = await get_client(account_id, required=False)
    if not client and store.account(account_id)["error"] == "连接失败，请重试":
        raise HTTPException(502, "账号暂时无法连接，请稍后重试退出")
    async with client_locks.setdefault(account_id, asyncio.Lock()):
        await cancel_account_jobs(account_id)
        if client:
            try:
                if not await asyncio.wait_for(client.log_out(), timeout=30):
                    raise HTTPException(502, "退出Telegram失败，请重试")
            except HTTPException:
                raise
            except Exception as error:
                raise login_error(error) from None
        clients.pop(account_id, None)
        dialog_cache.pop(account_id, None)
        store.remove_account(account_id)
    return {"ok": True}


@app.get("/v1/dialogs")
async def dialogs(account_id: str, q: str = "", page: int = 1, kind: str = "all", refresh: bool = False, authorization: str = Header(None)):
    require_auth(authorization)
    entries = await get_dialog_entries(account_id, refresh)
    query = q.strip().casefold().lstrip("@")
    selected = {source["chat_id"] for source in store.sources() if source["account_id"] == account_id}
    filtered = [entry for entry in entries if (not query or query in (entry["title"] + " " + entry["username"] + " " + str(entry["id"])).casefold()) and (kind == "all" or (kind == "group" and entry["kind"] != "channel") or entry["kind"] == kind)]
    pages = max(1, (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    items = [{key: value for key, value in entry.items() if key != "peer"} for entry in filtered[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]]
    for item in items:
        item["selected"] = item["id"] in selected
    return {"items": items, "total": len(filtered), "page": page, "pages": pages}


@app.get("/v1/sources")
async def sources(authorization: str = Header(None)):
    require_auth(authorization)
    return {"sources": source_list()}


@app.post("/v1/sources")
async def add_sources(body: SourcesBody, authorization: str = Header(None)):
    require_auth(authorization)
    ids = set(body.chat_ids)
    if not ids or len(ids) > 100:
        raise HTTPException(400, "每次请选择1至100个群组或频道")
    entries = {entry["id"]: entry for entry in await get_dialog_entries(body.account_id)}
    if not ids.issubset(entries):
        raise HTTPException(400, "部分群组或频道不可访问，请刷新列表")
    created = []
    for chat_id in ids:
        entry = entries[chat_id]
        source_id = store.add_source(body.account_id, chat_id, entry["title"], entry["username"], entry["kind"], entry["peer"])
        schedule_sync(source_id, force=True)
        created.append(source_id)
    return {"ok": True, "ids": created, "sources": source_list()}


@app.post("/v1/sources/{source_id}/remove")
async def remove_source(source_id: str, authorization: str = Header(None)):
    require_auth(authorization)
    if not store.source(source_id):
        raise HTTPException(404, "source_not_found")
    await cancel_source_job(source_id)
    store.remove_source(source_id)
    return {"ok": True}


@app.post("/v1/sources/{source_id}/sync")
async def resync_source(source_id: str, body: SyncBody, authorization: str = Header(None)):
    require_auth(authorization)
    if not store.source(source_id):
        raise HTTPException(404, "source_not_found")
    if body.reset:
        await cancel_source_job(source_id)
        store.reset_source(source_id)
    schedule_sync(source_id, force=True)
    return {"ok": True}


@app.get("/v1/catalog")
async def catalog(page: int = 1, source_id: str = "", q: str = "", refresh: bool = False, authorization: str = Header(None)):
    require_auth(authorization)
    if source_id and not store.source(source_id):
        raise HTTPException(404, "source_not_found")
    for source in store.sources():
        if not source_id or source["id"] == source_id:
            schedule_sync(source["id"], force=refresh)
    result = store.catalog(max(1, page), source_id, q[:200])
    for item in result["items"]:
        item.update(media_links(item["source_id"], item["id"]))
    result["sources"] = source_list()
    result["syncing"] = any(source["syncing"] for source in result["sources"] if not source_id or source["id"] == source_id)
    return result


@app.get("/v1/media-link/{source_id}/{message_id}")
async def media_link(source_id: str, message_id: int, authorization: str = Header(None)):
    require_auth(authorization)
    _, message = await get_message(source_id, message_id)
    if not document_info(message):
        raise HTTPException(404, "media_not_found")
    return media_links(source_id, message_id)


@app.get("/v1/download-link/{source_id}/{message_id}")
async def download_link(source_id: str, message_id: int, authorization: str = Header(None)):
    require_auth(authorization)
    _, message = await get_message(source_id, message_id)
    info = document_info(message)
    if not info:
        raise HTTPException(404, "media_not_found")
    expires, signature = sign_media(source_id, message_id, "download")
    return {"download_url": f"{PUBLIC_BASE}/v1/download/{source_id}/{message_id}?exp={expires}&sig={signature}", "file_name": info["file_name"]}


# Read-only, audio-only integration for a separate music frontend.
@app.get("/v1/music/sources")
async def music_sources(authorization: str = Header(None)):
    require_music_auth(authorization)
    return {"sources": store.audio_sources()}


@app.get("/v1/music/catalog")
async def music_catalog(page: int = 1, source_id: str = "", q: str = "", authorization: str = Header(None)):
    require_music_auth(authorization)
    result = store.catalog(page=max(1, page), source_id=source_id, query=q[:200], audio_only=True)
    for item in result["items"]:
        item.pop("account_id", None)
    return result


@app.get("/v1/music/item/{source_id}/{message_id}")
async def music_item(source_id: str, message_id: int, authorization: str = Header(None)):
    require_music_auth(authorization)
    _, message = await get_message(source_id, message_id)
    info = require_audio(document_info(message))
    info.update(source_id=source_id, has_artwork=bool(getattr(message.document, "thumbs", None)))
    for attribute in message.document.attributes:
        if isinstance(attribute, DocumentAttributeAudio) and attribute.title:
            info["title"] = attribute.title[:200]
    expires, signature = sign_media(source_id, message_id, "music")
    info["stream_url"] = f"{PUBLIC_BASE}/v1/music/media/{source_id}/{message_id}?exp={expires}&sig={signature}"
    if info["has_artwork"]:
        expires, signature = sign_media(source_id, message_id, "music_thumb")
        info["thumb_url"] = f"{PUBLIC_BASE}/v1/music/thumb/{source_id}/{message_id}?exp={expires}&sig={signature}"
    return info


@app.api_route("/v1/music/media/{source_id}/{message_id}", methods=["GET", "HEAD"])
async def music_media(source_id: str, message_id: int, request: Request, exp: int, sig: str):
    return await serve_media(source_id, message_id, request, exp, sig, music=True)


@app.get("/v1/music/thumb/{source_id}/{message_id}")
async def music_thumb(source_id: str, message_id: int, exp: int, sig: str):
    if not verify_media(source_id, message_id, exp, sig, "music_thumb"):
        raise HTTPException(403, "invalid_signature")
    _, message = await get_message(source_id, message_id)
    require_audio(document_info(message))
    thumb_exp, thumb_sig = sign_media(source_id, message_id, "thumb")
    return await thumb(source_id, message_id, thumb_exp, thumb_sig)


def range_bounds(value, size):
    if size <= 0:
        raise HTTPException(416, "range_not_satisfiable", headers={"Content-Range": f"bytes */{size}"})
    if not value:
        return 0, size - 1, False
    match = re.match(r"^bytes=(\d*)-(\d*)$", value.strip())
    if not match:
        raise HTTPException(416, "invalid_range", headers={"Content-Range": f"bytes */{size}"})
    left, right = match.groups()
    if not left:
        length = min(int(right or 0), size)
        if length <= 0:
            raise HTTPException(416, "range_not_satisfiable", headers={"Content-Range": f"bytes */{size}"})
        return size - length, size - 1, True
    start = int(left)
    end = min(int(right), size - 1) if right else size - 1
    if start < 0 or start >= size or end < start:
        raise HTTPException(416, "range_not_satisfiable", headers={"Content-Range": f"bytes */{size}"})
    return start, end, True


@app.api_route("/v1/media/{source_id}/{message_id}", methods=["GET", "HEAD"])
async def media(source_id: str, message_id: int, request: Request, exp: int, sig: str):
    return await serve_media(source_id, message_id, request, exp, sig)


@app.api_route("/v1/download/{source_id}/{message_id}", methods=["GET", "HEAD"])
async def download(source_id: str, message_id: int, request: Request, exp: int, sig: str):
    return await serve_media(source_id, message_id, request, exp, sig, attachment=True)


async def serve_media(source_id, message_id, request, exp, sig, attachment=False, music=False):
    if not verify_media(source_id, message_id, exp, sig, "music" if music else "download" if attachment else "media"):
        raise HTTPException(403, "invalid_signature")
    client, message = await get_message(source_id, message_id)
    info = document_info(message)
    if music:
        require_audio(info)
    if not info:
        raise HTTPException(404, "media_not_found")
    size = info["size"]
    start, end, partial = range_bounds(request.headers.get("Range"), size)
    length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Disposition": ("attachment" if attachment else "inline") + "; filename*=UTF-8''" + quote(info["file_name"], safe=""),
        "Cache-Control": "private, no-store",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    media_type = "application/octet-stream" if attachment else info["mime"]
    if music:
        aliases = {"audio/m4a": "audio/mp4", "audio/x-flac": "audio/flac", "audio/vnd.wave": "audio/wav", "audio/x-wav": "audio/wav"}
        media_type = aliases.get(media_type.lower(), media_type.lower())
    if request.method == "HEAD":
        return Response(status_code=206 if partial else 200, media_type=media_type, headers=headers)

    async def body():
        remaining = length
        async with download_slots:
            async for chunk in client.iter_download(message.media, offset=start, request_size=512 * 1024):
                if not chunk or remaining <= 0:
                    break
                part = chunk[:remaining]
                remaining -= len(part)
                yield part
                if remaining <= 0:
                    break

    return StreamingResponse(body(), status_code=206 if partial else 200, media_type=media_type, headers=headers)


@app.get("/v1/thumb/{source_id}/{message_id}")
async def thumb(source_id: str, message_id: int, exp: int, sig: str):
    if not verify_media(source_id, message_id, exp, sig, "thumb"):
        raise HTTPException(403, "invalid_signature")
    key = (source_id, message_id)
    cached = thumb_cache.get(key)
    if cached and time.time() - cached[0] < 600:
        return Response(cached[1], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
    client, message = await get_message(source_id, message_id)
    data = await client.download_media(message, file=bytes, thumb=-1)
    if not data:
        raise HTTPException(404, "thumbnail_not_found")
    thumb_cache[key] = (time.time(), data)
    if len(thumb_cache) > 200:
        oldest = min(thumb_cache, key=lambda key: thumb_cache[key][0])
        thumb_cache.pop(oldest, None)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@app.on_event("startup")
async def startup():
    global maintenance_job
    async def warm(account_id):
        try:
            await get_client(account_id, required=False)
        except Exception:
            pass
    for account in store.accounts():
        account_jobs[account["id"]] = asyncio.create_task(warm(account["id"]))
    for source in store.sources():
        schedule_sync(source["id"], force=True)
    maintenance_job = asyncio.create_task(maintain_index())


@app.on_event("shutdown")
async def shutdown():
    tasks = list(source_jobs.values()) + list(account_jobs.values())
    if maintenance_job:
        tasks.append(maintenance_job)
    for session in login_sessions.values():
        if session.get("task"):
            tasks.append(session["task"])
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for session in list(login_sessions.values()):
        await session["client"].disconnect()
    for client in list(clients.values()):
        await client.disconnect()
    store.close()
