import assert from "node:assert/strict";
import { chromium } from "playwright";
import { HTML } from "../src/page.js";
import { APP_JS } from "../src/client.js";

const base = "https://drive.example.test";
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  {
    let phase = "";
    let method = "phone";
    let sentCount = 0;
    let needsPassword = true;
    const state = () => ({ status: phase, method, account_id: "a".repeat(24), phone_masked: "+15****0100", delivery: "telegram", retry_after: 1 });
    await context.route(base + "/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/") return route.fulfill({ contentType: "text/html", body: HTML });
      if (path === "/app.js") return route.fulfill({ contentType: "application/javascript", body: APP_JS });
      let status = 200;
      let data = { ok: true };
      if (path === "/api/tg/status") data = { accounts: phase === "completed" ? [{ id: "a".repeat(24), name: "测试账号", source_count: 0, status: "ready" }] : [], sources: [] };
      else if (path === "/api/tg/login-status") {
        if (!phase) { status = 404; data = { error: "login_expired" }; }
        else data = state();
      } else if (path === "/api/tg/send-code") {
        const body = route.request().postDataJSON();
        if (!sentCount) assert.equal(body.phone, "+15555550100");
        else if (sentCount === 1) assert.equal(body.phone, undefined, "resend after reload uses backend session");
        sentCount++;
        phase = "code_sent";
        method = "phone";
        data = state();
      } else if (path === "/api/tg/verify-code") {
        const code = route.request().postDataJSON().code;
        if (code === "00000") { status = 400; data = { error: "PhoneCodeInvalidError" }; }
        else if (code === "11111") { phase = "expired"; status = 400; data = { error: "PhoneCodeExpiredError" }; }
        else { assert.equal(code, "12345"); phase = needsPassword ? "password_required" : "completed"; data = state(); }
      } else if (path === "/api/tg/password") {
        if (route.request().postDataJSON().password !== "correct") { status = 400; data = { error: "PasswordHashInvalidError" }; }
        else { phase = "completed"; data = state(); }
      } else if (path === "/api/tg/qr") {
        method = "qr";
        phase = "password_required";
        data = { qr: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jQioAAAAASUVORK5CYII=", expires_in: 30 };
      } else if (path === "/api/dialogs") data = { items: [], page: 1, pages: 1, total: 0 };
      else if (path === "/api/catalog") data = { items: [], page: 1, pages: 1, page_size: 30, total: 0, sources: [], syncing: false };
      await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto(base);
    await page.locator("#phoneForm").waitFor({ state: "visible" });
    assert.equal(await page.locator("#qrPanel").isVisible(), false);
    await page.locator("#tgPhone").fill("+15555550100");
    await page.locator("#sendCode").click();
    await page.locator("#tgCode").waitFor({ state: "visible" });
    assert.equal(await page.locator("#tgCode").getAttribute("autocomplete"), "one-time-code");
    await page.goto("about:blank");
    await page.goto(base);
    await page.locator("#tgCode").waitFor({ state: "visible" });
    assert.equal(sentCount, 1);
    assert.match(await page.locator("#codeMsg").textContent(), /Telegram官方聊天/);
    await page.locator("#tgCode").fill("00000");
    await page.locator("#verifyCode").click();
    await page.waitForFunction(() => document.querySelector("#tgStatus").textContent.includes("验证码错误"));
    await page.locator("#tgCode").fill("11111");
    await page.locator("#verifyCode").click();
    await page.waitForFunction(() => document.querySelector("#tgStatus").textContent.includes("验证码已过期"));
    await page.reload();
    await page.locator("#resendCode").click();
    await page.waitForFunction(() => document.querySelector("#tgStatus").textContent.includes("验证码已发送"));
    assert.equal(sentCount, 2);
    await page.locator("#tgCode").fill("12345");
    await page.locator("#verifyCode").click();
    await page.locator("#tgPassword").waitFor({ state: "visible" });
    await page.reload();
    await page.locator("#tgPassword").waitFor({ state: "visible" });
    assert.equal(await page.locator("#codeArea").isVisible(), false);
    await page.locator("#tgPassword").fill("wrong");
    await page.locator("#submitPassword").click();
    await page.waitForFunction(() => document.querySelector("#tgStatus").textContent.includes("两步验证密码错误"));
    await page.locator("#tgPassword").fill("correct");
    await page.locator("#submitPassword").click();
    await page.locator("#qrArea").waitFor({ state: "hidden" });
    assert.equal(await page.locator("#tgPassword").inputValue(), "");
    phase = "";
    needsPassword = false;
    await page.reload();
    await page.locator("#tgPhone").fill("+15555550100");
    await page.locator("#sendCode").click();
    await page.locator("#tgCode").fill("12345");
    await page.locator("#verifyCode").click();
    await page.locator("#qrArea").waitFor({ state: "hidden" });
    phase = "";
    await page.reload();
    await page.locator("#useQr").click();
    await page.locator("#startQr").click();
    await page.locator("#tgPassword").waitFor({ state: "visible" });
    await page.locator("#tgPassword").fill("correct");
    await page.locator("#submitPassword").click();
    await page.locator("#qrArea").waitFor({ state: "hidden" });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(errors, []);
    console.log("MOCK: single-device page resume, code retry/expiry/resend, 2FA resume/retry, code-only login and QR 2FA passed");
  }
} finally { await browser.close(); }
