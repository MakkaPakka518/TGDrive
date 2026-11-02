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
from urllib.parse import quote

import qrcode
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Header, HTTPException, Request
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
)


API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SERVICE_TOKEN = os.environ["SERVICE_TOKEN"]
MEDIA_SECRET = os.environ["MEDIA_SECRET"].encode()
FERNET = Fernet(os.environ["SESSION_ENCRYPTION_KEY"].encode())
PUBLIC_BASE = os.environ["PUBLIC_BASE"].rstrip("/")
FRONTEND_ORIGIN = os.environ["FRONTEND_ORIGIN"].rstrip("/")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
SESSION_FILE = DATA_DIR / "telegram.session.enc"
CONFIG_FILE = DATA_DIR / "config.json"
CATALOG_LIMIT = 200
URL_TTL = 12 * 60 * 60

DATA_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(DATA_DIR, 0o700)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
login_sessions = {}
login_start_lock = asyncio.Lock()
phone_retry_at = {}
client_lock = asyncio.Lock()
download_slots = asyncio.Semaphore(2)
main_client = None
catalog_cache = {"at": 0, "channel": None, "items": []}
thumb_cache = {}


class PasswordBody(BaseModel):
    password: str


class PhoneBody(BaseModel):
    phone: str = ""


class CodeBody(BaseModel):
    code: str


class ChannelBody(BaseModel):
    channel_id: int


def require_auth(authorization):
    supplied = str(authorization or "").strip()
    if supplied.lower().startswith("bearer "):
        supplied = supplied[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, SERVICE_TOKEN):
        raise HTTPException(401, "unauthorized")


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save_config(config):
    temp = CONFIG_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False))
    os.chmod(temp, 0o600)
    temp.replace(CONFIG_FILE)


def save_session(value):
    encrypted = FERNET.encrypt(value.encode())
    temp = SESSION_FILE.with_suffix(".tmp")
    temp.write_bytes(encrypted)
    os.chmod(temp, 0o600)
    temp.replace(SESSION_FILE)


def load_session():
    try:
        return FERNET.decrypt(SESSION_FILE.read_bytes()).decode()
    except (OSError, InvalidToken, UnicodeDecodeError):
        return ""


async def get_client(required=True):
    global main_client
    async with client_lock:
        if main_client and main_client.is_connected():
            if await main_client.is_user_authorized():
                return main_client
            await main_client.disconnect()
            main_client = None
        raw = load_session()
        if not raw:
            if required:
                raise HTTPException(401, "telegram_login_required")
            return None
        client = TelegramClient(StringSession(raw), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            if required:
                raise HTTPException(401, "telegram_login_required")
            return None
        main_client = client
        return client


def cleanup_login_sessions():
    now = time.time()
    stale = [key for key, value in login_sessions.items() if value["expires"] < now and not value.get("lock", asyncio.Lock()).locked()]
    for key in stale:
        session = login_sessions.pop(key, None)
        if session:
            task = session.get("task")
            if task and not task.done():
                task.cancel()
            if session["client"] is not main_client:
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
        if session["client"] is not main_client:
            await session["client"].disconnect()


async def new_login_session(method, previous_id=None):
    cleanup_login_sessions()
    await discard_login(previous_id)
    if await get_client(required=False):
        raise HTTPException(409, "telegram_already_authorized")
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
    global main_client
    async with client_lock:
        previous = main_client
        save_session(client.session.save())
        main_client = client
        if previous and previous is not client:
            await previous.disconnect()
    me = await client.get_me()
    session = login_sessions.get(key)
    if session:
        session["status"] = "completed"
        session["user"] = {
            "id": me.id,
            "name": " ".join(filter(None, [me.first_name, me.last_name])) or me.username or str(me.id),
            "username": me.username or "",
        }


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


def sign_media(message_id, kind="media"):
    expires = int(time.time()) + URL_TTL
    payload = f"{selected_channel_id()}:{kind}:{message_id}:{expires}"
    signature = hmac.new(MEDIA_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return expires, signature


def verify_media(message_id, expires, signature, kind="media"):
    if expires < int(time.time()) or expires > int(time.time()) + URL_TTL + 60:
        return False
    expected = hmac.new(MEDIA_SECRET, f"{selected_channel_id()}:{kind}:{message_id}:{expires}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def media_links(message_id):
    expires, signature = sign_media(message_id)
    thumb_expires, thumb_signature = sign_media(message_id, "thumb")
    return {
        "stream_url": f"{PUBLIC_BASE}/v1/media/{message_id}?exp={expires}&sig={signature}",
        "thumb_url": f"{PUBLIC_BASE}/v1/thumb/{message_id}?exp={thumb_expires}&sig={thumb_signature}",
    }


def selected_channel_id():
    value = load_config().get("channel_id")
    if not value:
        raise HTTPException(400, "channel_not_selected")
    return int(value)


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


async def get_message(message_id):
    client = await get_client()
    channel = selected_channel_id()
    message = await client.get_messages(channel, ids=message_id)
    if not message or not message.document:
        raise HTTPException(404, "media_not_found")
    return client, message


@app.middleware("http")
async def secure_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Access-Control-Allow-Origin"] = FRONTEND_ORIGIN
    return response


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/v1/status")
async def status(authorization: str = Header(None)):
    require_auth(authorization)
    client = await get_client(required=False)
    if not client:
        return {"authorized": False}
    me = await client.get_me()
    return {
        "authorized": True,
        "user": {"id": me.id, "name": " ".join(filter(None, [me.first_name, me.last_name])) or me.username or str(me.id)},
        "channel_id": load_config().get("channel_id"),
    }


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
        if await get_client(required=False):
            raise HTTPException(409, "telegram_already_authorized")
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


@app.get("/v1/channels")
async def channels(authorization: str = Header(None)):
    require_auth(authorization)
    client = await get_client()
    result = []
    async for dialog in client.iter_dialogs():
        if not dialog.is_channel:
            continue
        entity = dialog.entity
        result.append({
            "id": int(dialog.id),
            "title": dialog.name or str(dialog.id),
            "username": getattr(entity, "username", None) or "",
            "broadcast": bool(getattr(entity, "broadcast", False)),
            "megagroup": bool(getattr(entity, "megagroup", False)),
        })
        if len(result) >= 300:
            break
    return {"channels": result}


@app.post("/v1/channel")
async def select_channel(body: ChannelBody, authorization: str = Header(None)):
    require_auth(authorization)
    client = await get_client()
    entity = await client.get_entity(body.channel_id)
    save_config({"channel_id": int(body.channel_id), "title": getattr(entity, "title", str(body.channel_id))})
    catalog_cache.update({"at": 0, "channel": None, "items": []})
    thumb_cache.clear()
    return {"ok": True, "channel_id": body.channel_id, "title": getattr(entity, "title", "")}


@app.get("/v1/catalog")
async def catalog(limit: int = 100, authorization: str = Header(None)):
    require_auth(authorization)
    channel = selected_channel_id()
    limit = max(1, min(int(limit), CATALOG_LIMIT))
    if catalog_cache["channel"] == channel and time.time() - catalog_cache["at"] < 30:
        return {"items": catalog_cache["items"][:limit], "channel": load_config()}
    client = await get_client()
    items = []
    async for message in client.iter_messages(channel, limit=CATALOG_LIMIT):
        info = document_info(message)
        if not info:
            continue
        info.update(media_links(message.id))
        items.append(info)
    catalog_cache.update({"at": time.time(), "channel": channel, "items": items})
    return {"items": items[:limit], "channel": load_config()}


@app.get("/v1/media-link/{message_id}")
async def media_link(message_id: int, authorization: str = Header(None)):
    require_auth(authorization)
    _, message = await get_message(message_id)
    if not document_info(message):
        raise HTTPException(404, "media_not_found")
    return media_links(message_id)


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


@app.api_route("/v1/media/{message_id}", methods=["GET", "HEAD"])
async def media(message_id: int, request: Request, exp: int, sig: str):
    if not verify_media(message_id, exp, sig):
        raise HTTPException(403, "invalid_signature")
    client, message = await get_message(message_id)
    info = document_info(message)
    if not info:
        raise HTTPException(404, "media_not_found")
    size = info["size"]
    start, end, partial = range_bounds(request.headers.get("Range"), size)
    length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Disposition": "inline; filename*=UTF-8''" + quote(info["file_name"], safe=""),
        "Cache-Control": "private, no-store",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        return Response(status_code=206 if partial else 200, media_type=info["mime"], headers=headers)

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

    return StreamingResponse(body(), status_code=206 if partial else 200, media_type=info["mime"], headers=headers)


@app.get("/v1/thumb/{message_id}")
async def thumb(message_id: int, exp: int, sig: str):
    if not verify_media(message_id, exp, sig, "thumb"):
        raise HTTPException(403, "invalid_signature")
    cached = thumb_cache.get(message_id)
    if cached and time.time() - cached[0] < 600:
        return Response(cached[1], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
    client, message = await get_message(message_id)
    data = await client.download_media(message, file=bytes, thumb=-1)
    if not data:
        raise HTTPException(404, "thumbnail_not_found")
    thumb_cache[message_id] = (time.time(), data)
    if len(thumb_cache) > 200:
        oldest = min(thumb_cache, key=lambda key: thumb_cache[key][0])
        thumb_cache.pop(oldest, None)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@app.on_event("shutdown")
async def shutdown():
    for session in list(login_sessions.values()):
        await session["client"].disconnect()
    if main_client and main_client.is_connected():
        await main_client.disconnect()
