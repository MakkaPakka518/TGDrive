import { HTML } from "./page.js";
import { APP_JS } from "./client.js";

const encoder = new TextEncoder();
const authFails = new Map();
const COOKIE_OPTIONS = "Path=/; Secure; HttpOnly; SameSite=Strict";

class ApiError extends Error {
  constructor(status, message, retryAfter = 0) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

function responseHeaders(type, env = {}) {
  let backendOrigin = "";
  try {
    const url = new URL(env.BACKEND_URL);
    if (["https:", "http:"].includes(url.protocol)) backendOrigin = url.origin;
  } catch {}
  return {
    "Content-Type": type,
    "Cache-Control": "no-store",
    "Content-Security-Policy": `default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self' data: ${backendOrigin}; media-src ${backendOrigin || "'none'"}; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
  };
}

function json(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...responseHeaders("application/json; charset=utf-8"), ...extraHeaders },
  });
}

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let result = 0;
  for (let i = 0; i < a.length; i++) result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return result === 0;
}

async function sign(secret, value) {
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const bytes = new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(value)));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function cookie(req, name) {
  for (const part of (req.headers.get("Cookie") || "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i).trim() === name) return part.slice(i + 1).trim();
  }
  return "";
}

async function signedCookie(env, name, value, maxAge) {
  const raw = `${value}.${Date.now() + maxAge * 1000}`;
  return `${name}=${raw}.${await sign(env.SESSION_SECRET, raw)}; Max-Age=${maxAge}; ${COOKIE_OPTIONS}`;
}

async function cookieValue(req, env, name) {
  const parts = cookie(req, name).split(".");
  const expires = Number(parts[1]);
  if (parts.length !== 3 || !Number.isFinite(expires) || expires <= Date.now()) return "";
  return safeEqual(await sign(env.SESSION_SECRET, `${parts[0]}.${parts[1]}`), parts[2]) ? parts[0] : "";
}

async function body(req) {
  if (Number(req.headers.get("Content-Length")) > 4096) throw new ApiError(413, "请求内容过长");
  const text = await req.text();
  if (encoder.encode(text).length > 4096) throw new ApiError(413, "请求内容过长");
  try {
    const data = JSON.parse(text);
    if (!data || Array.isArray(data) || typeof data !== "object") throw new Error();
    return data;
  } catch {
    throw new ApiError(400, "请求格式错误");
  }
}

async function backend(env, path, data) {
  let response;
  try {
    response = await fetch(env.BACKEND_URL.replace(/\/+$/, "") + path, {
      method: data === undefined ? "GET" : "POST",
      headers: { Authorization: `Bearer ${env.BACKEND_TOKEN}`, "Content-Type": "application/json" },
      body: data === undefined ? undefined : JSON.stringify(data),
      signal: AbortSignal.timeout(60000),
      redirect: "manual",
    });
  } catch {
    throw new ApiError(502, "后端暂时无法连接，请稍后重试");
  }
  const result = await response.json().catch(() => null);
  if (!response.ok || result === null) {
    const detail = typeof result?.detail === "string" ? result.detail : "后端请求失败";
    throw new ApiError(response.status >= 400 && response.status < 500 ? response.status : 502, response.status < 500 ? detail : "后端请求失败，请稍后重试", Number(response.headers.get("Retry-After")) || 0);
  }
  return result;
}

async function handle(req, env) {
  const { pathname, origin, searchParams } = new URL(req.url);
  const method = req.method;
  if (pathname === "/" && method === "GET") return new Response(HTML, { headers: responseHeaders("text/html; charset=utf-8", env) });
  if (pathname === "/app.js" && method === "GET") return new Response(APP_JS, { headers: responseHeaders("application/javascript; charset=utf-8", env) });
  if (pathname === "/health" && method === "GET") return json({ ok: true });
  if (!pathname.startsWith("/api/")) return json({ error: "not_found" }, 404);
  if (!["ADMIN_PASSWORD", "SESSION_SECRET", "BACKEND_TOKEN", "BACKEND_URL"].every(key => env[key])) throw new ApiError(503, "服务尚未配置完成");
  if (!["GET", "HEAD"].includes(method)) {
    const suppliedOrigin = req.headers.get("Origin");
    if ((suppliedOrigin && suppliedOrigin !== origin) || req.headers.get("Sec-Fetch-Site") === "cross-site") throw new ApiError(403, "invalid_origin");
  }
  if (pathname === "/api/login" && method === "POST") {
    const ip = req.headers.get("CF-Connecting-IP") || "unknown";
    const now = Date.now();
    for (const [key, times] of authFails) if (now - times.at(-1) >= 900000) authFails.delete(key);
    const attempts = (authFails.get(ip) || []).filter(time => now - time < 900000);
    if (attempts.length >= 8) throw new ApiError(429, "尝试次数过多，请15分钟后重试");
    const data = await body(req);
    if (typeof data.password !== "string" || !safeEqual(data.password, env.ADMIN_PASSWORD)) {
      attempts.push(now);
      if (authFails.size >= 1024 && !authFails.has(ip)) authFails.delete(authFails.keys().next().value);
      authFails.set(ip, attempts);
      throw new ApiError(401, "密码错误");
    }
    authFails.delete(ip);
    return json({ ok: true }, 200, { "Set-Cookie": await signedCookie(env, "tgdrive", "admin", 43200) });
  }
  if (pathname === "/api/logout" && method === "POST") {
    const response = json({ ok: true });
    for (const name of ["tgdrive", "tg_login"]) response.headers.append("Set-Cookie", `${name}=; Max-Age=0; ${COOKIE_OPTIONS}`);
    return response;
  }
  if (await cookieValue(req, env, "tgdrive") !== "admin") throw new ApiError(401, "unauthorized");
  if (pathname === "/api/me" && method === "GET") return json({ ok: true });
  if (pathname === "/api/tg/status" && method === "GET") return json(await backend(env, "/v1/status"));
  if (pathname === "/api/tg/cancel" && method === "POST") {
    const id = await cookieValue(req, env, "tg_login");
    if (id) await backend(env, "/v1/login/cancel?login_id=" + encodeURIComponent(id), {});
    return json({ ok: true }, 200, { "Set-Cookie": `tg_login=; Max-Age=0; ${COOKIE_OPTIONS}` });
  }
  if (pathname === "/api/tg/qr" && method === "POST") {
    const previous = await cookieValue(req, env, "tg_login");
    const data = await backend(env, "/v1/login/qr" + (previous ? "?login_id=" + encodeURIComponent(previous) : ""), {});
    return json({ qr: data.qr, expires_in: data.expires_in }, 200, { "Set-Cookie": await signedCookie(env, "tg_login", data.login_id, 600) });
  }
  if (pathname === "/api/tg/send-code" && method === "POST") {
    const input = await body(req);
    const phone = typeof input.phone === "string" ? input.phone.replace(/[\s()-]/g, "").replace(/^\+/, "") : "";
    const previous = await cookieValue(req, env, "tg_login");
    if ((!phone && !previous) || (phone && !/^[1-9][0-9]{7,14}$/.test(phone))) throw new ApiError(400, "请输入 +国家码和完整手机号");
    const data = await backend(env, "/v1/login/send-code" + (previous ? "?login_id=" + encodeURIComponent(previous) : ""), { phone });
    const { login_id, ...result } = data;
    return json(result, 200, { "Set-Cookie": await signedCookie(env, "tg_login", login_id, 600) });
  }
  if ((pathname === "/api/tg/login-status" && method === "GET") || (["/api/tg/password", "/api/tg/verify-code"].includes(pathname) && method === "POST")) {
    const id = await cookieValue(req, env, "tg_login");
    if (!id) throw new ApiError(404, "登录会话已过期，请重新获取验证码或二维码");
    const suffix = "?login_id=" + encodeURIComponent(id);
    if (method === "GET") return json(await backend(env, "/v1/login/status" + suffix));
    const data = await body(req);
    if (pathname === "/api/tg/verify-code") {
      const code = typeof data.code === "string" ? data.code.replace(/[\s-]/g, "") : "";
      if (!/^[0-9]{4,10}$/.test(code)) throw new ApiError(400, "请输入收到的数字验证码");
      return json(await backend(env, "/v1/login/verify-code" + suffix, { code }), 200, { "Set-Cookie": await signedCookie(env, "tg_login", id, 600) });
    }
    if (typeof data.password !== "string" || !data.password) throw new ApiError(400, "请输入两步验证密码");
    return json(await backend(env, "/v1/login/password" + suffix, { password: data.password }));
  }
  if (pathname === "/api/accounts" && method === "GET") return json(await backend(env, "/v1/accounts"));
  const accountAction = pathname.match(/^\/api\/accounts\/([a-f0-9]{24})\/logout$/);
  if (accountAction && method === "POST") return json(await backend(env, "/v1/accounts/" + accountAction[1] + "/logout", {}));
  if (pathname === "/api/sources" && method === "GET") return json(await backend(env, "/v1/sources"));
  if (pathname === "/api/sources" && method === "POST") {
    const data = await body(req);
    if (typeof data.account_id !== "string" || !/^[a-f0-9]{24}$/.test(data.account_id) || !Array.isArray(data.chat_ids) || !data.chat_ids.length || data.chat_ids.length > 100 || data.chat_ids.some(id => !Number.isSafeInteger(id) || id >= 0)) throw new ApiError(400, "请选择有效账号及群组、频道");
    return json(await backend(env, "/v1/sources", { account_id: data.account_id, chat_ids: data.chat_ids }));
  }
  const sourceAction = pathname.match(/^\/api\/sources\/([a-f0-9]{24})\/(remove|sync)$/);
  if (sourceAction && method === "POST") {
    const data = await body(req);
    return json(await backend(env, "/v1/sources/" + sourceAction[1] + "/" + sourceAction[2], sourceAction[2] === "sync" ? { reset: data.reset === true } : {}));
  }
  if (["/api/dialogs", "/api/catalog"].includes(pathname) && method === "GET") {
    const page = Number(searchParams.get("page") || 1);
    if (!Number.isSafeInteger(page) || page < 1 || page > 1000000) throw new ApiError(400, "页码无效");
    const params = new URLSearchParams({ page: String(page), q: (searchParams.get("q") || "").slice(0, 200) });
    if (searchParams.get("refresh") === "true") params.set("refresh", "true");
    if (pathname === "/api/dialogs") {
      const account = searchParams.get("account_id") || "";
      if (!/^[a-f0-9]{24}$/.test(account)) throw new ApiError(400, "请选择Telegram账号");
      params.set("account_id", account);
      const kind = searchParams.get("kind") || "all";
      params.set("kind", ["all", "channel", "group"].includes(kind) ? kind : "all");
    } else if (searchParams.get("source_id")) {
      const id = searchParams.get("source_id");
      if (!/^[a-f0-9]{24}$/.test(id)) throw new ApiError(400, "网盘来源无效");
      params.set("source_id", id);
    }
    return json(await backend(env, pathname.replace("/api/", "/v1/") + "?" + params));
  }
  const media = pathname.match(/^\/api\/media-link\/([a-f0-9]{24})\/(\d+)$/);
  if (media && method === "GET") return json(await backend(env, "/v1/media-link/" + media[1] + "/" + media[2]));
  const download = pathname.match(/^\/api\/download\/([a-f0-9]{24})\/(\d+)$/);
  if (download && ["GET", "HEAD"].includes(method)) {
    const data = await backend(env, "/v1/download-link/" + download[1] + "/" + download[2]);
    const target = new URL(data.download_url);
    const base = new URL(env.BACKEND_URL);
    if (target.origin !== base.origin || !target.pathname.startsWith(base.pathname.replace(/\/$/, "") + "/v1/download/")) throw new ApiError(502, "下载链接无效");
    return new Response(null, { status: 302, headers: { ...responseHeaders("text/plain; charset=utf-8"), Location: target.href } });
  }
  return json({ error: "not_found" }, 404);
}

export default {
  async fetch(req, env) {
    try {
      return await handle(req, env);
    } catch (error) {
      const retryAfter = error instanceof ApiError ? error.retryAfter : 0;
      return json({ error: error instanceof ApiError ? error.message : "请求处理失败，请稍后重试", ...(retryAfter > 0 ? { retry_after: retryAfter } : {}) }, error instanceof ApiError ? error.status : 500, retryAfter > 0 ? { "Retry-After": String(retryAfter) } : {});
    }
  },
};
