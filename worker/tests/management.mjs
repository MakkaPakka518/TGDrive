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
  page.on("dialog", dialog => dialog.accept());
  let accounts = [{ id: "a".repeat(24), name: "First account", status: "ready" }, { id: "b".repeat(24), name: "Second account", status: "ready" }];
  let sources = [{ id: "c".repeat(24), account_id: accounts[0].id, chat_id: -1001, title: "Initial source", kind: "channel", media_count: 235, history_done: true }];
  let submitted = [];
  let loggedOut = "";
  const allDialogs = Array.from({ length: 65 }, (_, index) => ({ id: -index - 1, title: index === 64 ? "目标群" : "普通群 " + (index + 1), username: "", kind: "group" }));
  const allMedia = Array.from({ length: 235 }, (_, index) => ({ id: index + 1, source_id: "c".repeat(24), source_title: "Initial source", title: index === 220 ? "needle video" : "Video " + (index + 1), kind: "video", duration: 30, size: 1024, download_url: base + "/signed-download/" + (index + 1) + "?exp=123&sig=test" }));
  const sourceList = () => sources.map(source => ({ ...source, account_name: accounts.find(account => account.id === source.account_id)?.name, syncing: false, error: "" }));
  await context.route(base + "/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: HTML });
    if (url.pathname === "/app.js") return route.fulfill({ contentType: "application/javascript", body: APP_JS });
    if (url.pathname.startsWith("/signed-download/")) return route.fulfill({ contentType: "application/octet-stream", headers: { "Content-Disposition": "attachment; filename*=UTF-8''" + encodeURIComponent("测试视频.mp4") }, body: "fake-media-file" });
    let data = { ok: true }, status = 200;
    if (url.pathname === "/api/tg/status") data = { accounts: accounts.map(account => ({ ...account, source_count: sources.filter(source => source.account_id === account.id).length })), sources: sourceList() };
    else if (url.pathname === "/api/tg/login-status") { status = 404; data = { error: "login_expired" }; }
    else if (url.pathname === "/api/catalog") {
      const query = url.searchParams.get("q") || "";
      const source = url.searchParams.get("source_id") || "";
      const filtered = allMedia.filter(item => sources.some(source => source.id === item.source_id) && (!source || item.source_id === source) && item.title.includes(query));
      const pages = Math.max(1, Math.ceil(filtered.length / 30));
      const current = Math.min(pages, Number(url.searchParams.get("page") || 1));
      data = { items: filtered.slice((current - 1) * 30, current * 30), total: filtered.length, page: current, pages, page_size: 30, has_next: current < pages, sources: sourceList(), syncing: false };
    } else if (url.pathname === "/api/dialogs") {
      const query = url.searchParams.get("q") || "";
      const filtered = allDialogs.filter(item => item.title.includes(query));
      const current = Number(url.searchParams.get("page") || 1);
      data = { items: filtered.slice((current - 1) * 30, current * 30).map(item => ({ ...item, selected: sources.some(source => source.account_id === url.searchParams.get("account_id") && source.chat_id === item.id) })), total: filtered.length, page: current, pages: Math.max(1, Math.ceil(filtered.length / 30)) };
    } else if (url.pathname === "/api/sources") {
      const body = route.request().postDataJSON(); submitted = body.chat_ids;
      for (const id of submitted) sources.push({ id: String(sources.length + 1).padStart(24, "0"), account_id: body.account_id, chat_id: id, title: allDialogs.find(item => item.id === id).title, kind: "group", media_count: 0, history_done: true });
    } else if (url.pathname.includes("/accounts/") && url.pathname.endsWith("/logout")) {
      loggedOut = url.pathname.split("/")[3]; accounts = accounts.filter(account => account.id !== loggedOut); sources = sources.filter(source => source.account_id !== loggedOut);
    } else if (url.pathname.includes("/sources/") && url.pathname.endsWith("/remove")) sources = sources.filter(source => source.id !== url.pathname.split("/")[3]);
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
  });
  await page.goto(base);
  await page.waitForFunction(() => document.querySelectorAll(".item").length === 30);
  assert.match(await page.locator("#pageLabel").textContent(), /235项.*30条/);
  assert.match(await page.locator(".downloadLink").first().getAttribute("href"), /\/signed-download\/1\?exp=123&sig=test$/);
  const downloadPromise = page.waitForEvent("download");
  await page.locator(".downloadLink").first().click();
  const downloaded = await downloadPromise;
  assert.equal(downloaded.suggestedFilename(), "测试视频.mp4");
  assert.equal(await downloaded.failure(), null);
  assert.equal(await page.locator("#modal").isVisible(), false, "Downloading must not start playback");
  await page.locator("#nextPage").click();
  await page.waitForFunction(() => document.querySelector("#pageLabel").textContent.includes("第2/8页"));
  await page.locator("#pageNumber").fill("8");
  await page.locator("#jumpPage").click();
  await page.waitForFunction(() => document.querySelectorAll(".item").length === 25);
  assert.equal(await page.locator("#nextPage").isDisabled(), true);
  await page.locator("#search").fill("needle");
  await page.waitForFunction(() => document.querySelectorAll(".item").length === 1);
  await page.locator("#search").fill("");
  await page.locator("#showAdmin").click();
  await page.locator("#dialogList input").first().waitFor();
  await page.locator("#dialogList input").first().check();
  await page.locator("#dialogNext").click();
  await page.waitForFunction(() => document.querySelector("#dialogPageLabel").textContent.includes("第2/3页"));
  await page.locator("#dialogList input").first().check();
  await page.locator("#dialogSearch").fill("目标群");
  await page.waitForFunction(() => document.querySelectorAll("#dialogList input").length === 1);
  await page.locator("#dialogList input").check();
  assert.match(await page.locator("#selectedCount").textContent(), /3个/);
  await page.locator("#addSources").click();
  await page.waitForFunction(() => document.querySelector("#globalMsg").textContent.includes("网盘来源已添加"));
  assert.deepEqual(submitted, [-1, -31, -65]);
  assert.equal(await page.locator("#addSources").isDisabled(), true);
  await page.locator("#accountList .listRow").filter({ hasText: "Second account" }).getByRole("button", { name: "退出账号" }).click();
  await page.waitForFunction(() => document.querySelectorAll("#accountList .listRow").length === 1);
  assert.equal(loggedOut, "b".repeat(24));
  assert.match(await page.locator("#accountList").textContent(), /First account/);
  await page.locator("#addAccount").click();
  await page.locator("#phoneForm").waitFor({ state: "visible" });
  await page.locator("#closeLogin").click();
  await page.locator("#loginBox").waitFor({ state: "hidden" });
  await page.locator("#sourceList .listRow").filter({ hasText: "Initial source" }).getByRole("button", { name: "移除", exact: true }).click();
  await page.waitForFunction(() => !document.querySelector("#sourceList").textContent.includes("Initial source"));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(errors, []);
  console.log("Management UI: 30-item pagination past 200, global media search, cross-page group selection, account logout isolation and adding accounts passed");
} finally { await browser.close(); }
