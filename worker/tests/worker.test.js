import assert from "node:assert/strict";
import { test } from "node:test";
import vm from "node:vm";
import worker from "../src/index.js";
import { APP_JS } from "../src/client.js";

const env = { ADMIN_PASSWORD: "test-only-password", SESSION_SECRET: "test-only-session-key", BACKEND_TOKEN: "test-backend-token", BACKEND_URL: "https://backend.example/api" };
const request = (path, options = {}) => new Request("https://drive.example.test" + path, options);
const post = (path, data, headers = {}) => request(path, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(data) });

test("authentication, signed login session, errors, and backend proxy contract", async () => {
  new vm.Script(APP_JS);
  const page = await worker.fetch(request("/"), { ...env, BACKEND_URL: "https://media.example.test/custom-path" });
  assert.match(page.headers.get("Content-Security-Policy"), /media-src https:\/\/media\.example\.test;/);
  assert.equal((await worker.fetch(request("/api/me"), env)).status, 401);
  assert.equal((await worker.fetch(post("/api/login", { password: env.ADMIN_PASSWORD }), {})).status, 503);
  assert.equal((await worker.fetch(post("/api/login", { password: "wrong" }), env)).status, 401);
  const login = await worker.fetch(post("/api/login", { password: env.ADMIN_PASSWORD }), env);
  assert.equal(login.status, 200);
  const setCookie = login.headers.get("Set-Cookie");
  assert.match(setCookie, /Secure; HttpOnly; SameSite=Strict/);
  const admin = setCookie.split(";")[0];
  assert.equal((await worker.fetch(request("/api/me", { headers: { Cookie: admin } }), env)).status, 200);
  assert.equal((await worker.fetch(request("/api/me", { headers: { Cookie: admin + "x" } }), env)).status, 401);
  assert.equal((await worker.fetch(post("/api/tg/qr", {}, { Cookie: admin, Origin: "https://attacker.example" }), env)).status, 403);
  assert.equal((await worker.fetch(post("/api/channel", { channel_id: "bad" }, { Cookie: admin }), env)).status, 400);
  assert.equal((await worker.fetch(request("/api/tg/login-status", { headers: { Cookie: admin } }), env)).status, 404);
  assert.equal((await worker.fetch(request("/api/channel", { method: "POST", headers: { Cookie: admin }, body: "{" }), env)).status, 400);
  const original = globalThis.fetch;
  const calls = [];
  try {
    globalThis.fetch = async (url, options) => {
      calls.push({ url, options });
      return Response.json({ login_id: "opaque-login-id", qr: "data:image/png;base64,abc", expires_in: 30 });
    };
    const qr = await worker.fetch(post("/api/tg/qr", {}, { Cookie: admin }), env);
    assert.equal(qr.status, 200);
    assert.equal((await qr.json()).login_id, undefined);
    assert.equal(calls[0].url, env.BACKEND_URL + "/v1/login/qr");
    assert.equal(calls[0].options.headers.Authorization, "Bearer " + env.BACKEND_TOKEN);
    assert.equal(calls[0].options.redirect, "manual");
    const cookies = admin + "; " + qr.headers.get("Set-Cookie").split(";")[0];
    globalThis.fetch = async (url, options) => { calls.push({ url, options }); return Response.json({ ok: true }); };
    assert.equal((await worker.fetch(post("/api/tg/password", { password: "2fa" }, { Cookie: cookies }), env)).status, 200);
    assert.equal(calls.at(-1).url, env.BACKEND_URL + "/v1/login/password?login_id=opaque-login-id");
    assert.deepEqual(JSON.parse(calls.at(-1).options.body), { password: "2fa" });
    assert.equal((await worker.fetch(post("/api/channel", { channel_id: -1001234567890 }, { Cookie: admin }), env)).status, 200);
    assert.deepEqual(JSON.parse(calls.at(-1).options.body), { channel_id: -1001234567890 });
    globalThis.fetch = async () => Response.json({ detail: "telegram_login_required" }, { status: 401 });
    const unauthorized = await worker.fetch(request("/api/channels", { headers: { Cookie: admin } }), env);
    assert.equal(unauthorized.status, 401);
    assert.deepEqual(await unauthorized.json(), { error: "telegram_login_required" });
    globalThis.fetch = async () => { throw new Error("private upstream details"); };
    const unavailable = await worker.fetch(request("/api/tg/status", { headers: { Cookie: admin } }), env);
    assert.equal(unavailable.status, 502);
    assert.doesNotMatch(await unavailable.text(), /private upstream details/);
  } finally { globalThis.fetch = original; }
  const logout = await worker.fetch(post("/api/logout", {}, { Cookie: admin }), env);
  assert.equal(logout.headers.getSetCookie().length, 2);
  for (const value of logout.headers.getSetCookie()) assert.match(value, /Max-Age=0/);
});

test("phone code sessions, resends, two-factor forwarding and flood feedback", async () => {
  const original = globalThis.fetch;
  const calls = [];
  const login = await worker.fetch(post("/api/login", { password: env.ADMIN_PASSWORD }), env);
  const admin = login.headers.get("Set-Cookie").split(";")[0];
  assert.equal((await worker.fetch(post("/api/tg/send-code", { phone: "+15555550100" }), env)).status, 401);
  assert.equal((await worker.fetch(post("/api/tg/send-code", { phone: "invalid" }, { Cookie: admin }), env)).status, 400);
  assert.equal((await worker.fetch(post("/api/tg/verify-code", { code: "12345" }, { Cookie: admin }), env)).status, 404);
  try {
    globalThis.fetch = async (url, options) => {
      calls.push({ url, options });
      return Response.json({ login_id: "phone-session-id", status: "code_sent", method: "phone", phone_masked: "+15****0100", retry_after: 60 });
    };
    const sent = await worker.fetch(post("/api/tg/send-code", { phone: "+1 (555) 555-0100" }, { Cookie: admin }), env);
    assert.equal(sent.status, 200);
    assert.equal((await sent.json()).login_id, undefined);
    assert.deepEqual(JSON.parse(calls.at(-1).options.body), { phone: "15555550100" });
    const cookies = admin + "; " + sent.headers.get("Set-Cookie").split(";")[0];
    const resend = await worker.fetch(post("/api/tg/send-code", {}, { Cookie: cookies }), env);
    assert.equal(resend.status, 200);
    assert.equal(calls.at(-1).url, env.BACKEND_URL + "/v1/login/send-code?login_id=phone-session-id");
    assert.deepEqual(JSON.parse(calls.at(-1).options.body), { phone: "" });
    globalThis.fetch = async (url, options) => { calls.push({ url, options }); return Response.json({ status: "password_required", method: "phone" }); };
    const verified = await worker.fetch(post("/api/tg/verify-code", { code: "12 345" }, { Cookie: cookies }), env);
    assert.equal(verified.status, 200);
    assert.equal((await verified.json()).status, "password_required");
    assert.equal(calls.at(-1).url, env.BACKEND_URL + "/v1/login/verify-code?login_id=phone-session-id");
    assert.deepEqual(JSON.parse(calls.at(-1).options.body), { code: "12345" });
    assert.match(verified.headers.get("Set-Cookie"), /tg_login=phone-session-id/);
    await worker.fetch(post("/api/tg/password", { password: "two-factor" }, { Cookie: cookies }), env);
    assert.equal(calls.at(-1).url, env.BACKEND_URL + "/v1/login/password?login_id=phone-session-id");
    globalThis.fetch = async () => Response.json({ detail: "请求太频繁" }, { status: 429, headers: { "Retry-After": "120" } });
    const flood = await worker.fetch(post("/api/tg/send-code", {}, { Cookie: cookies }), env);
    assert.equal(flood.status, 429);
    assert.equal(flood.headers.get("Retry-After"), "120");
    assert.equal((await flood.json()).retry_after, 120);
  } finally { globalThis.fetch = original; }
});
