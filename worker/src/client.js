export const APP_JS = String.raw`(() => {
  const $ = id => document.getElementById(id);
  let pollTimer = null;
  let items = [];
  let playback = 0;
  let loginGeneration = 0;
  let pendingLogin = null;
  let loginMethod = "phone";
  let resendUntil = 0;
  let resendTimer = null;
  let accounts = [];
  let sources = [];
  let currentPage = 1;
  let catalogGeneration = 0;
  let catalogKey = "";
  let dialogPage = 1;
  let dialogGeneration = 0;
  let dialogItems = [];
  const chosen = new Set();
  let searchTimer = null;
  let dialogTimer = null;
  let stateTimer = null;
  const kindLabel = { group: "普通群", supergroup: "超级群", channel: "频道" };
  const errors = {
    unauthorized: "管理登录已过期，请刷新页面重新登录",
    telegram_login_required: "请先登录Telegram",
    account_not_found: "该账号已移除，请刷新页面",
    source_not_found: "该网盘来源已移除，请重新选择",
    login_expired: "登录会话已过期，请重新获取验证码或二维码",
    too_many_login_sessions: "登录请求较多，请稍后重试",
    PasswordHashInvalidError: "两步验证密码错误，请重试",
    password_not_required: "登录会话已失效，请重新开始登录",
    code_not_required: "请重新获取验证码后登录",
    PhoneCodeInvalidError: "验证码错误，请检查后重试",
    PhoneCodeExpiredError: "验证码已过期，请重新发送",
    PhoneNumberInvalidError: "手机号无效，请填写国家码和完整手机号",
    PhoneNumberBannedError: "该手机号被Telegram限制登录",
    PhoneNumberUnoccupiedError: "该手机号尚未注册Telegram",
    ApiIdInvalidError: "Telegram API配置无效",
    SendCodeUnavailableError: "Telegram暂时无法发送验证码，请稍后重试",
  };
  async function api(path, options) {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(errors[data.error] || data.error || "请求失败，请稍后重试");
      error.status = response.status;
      error.retryAfter = data.retry_after || 0;
      throw error;
    }
    return data;
  }
  const post = (path, data = {}) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
  const notice = text => { $("globalMsg").textContent = text; };
  function status(text, kind = "") {
    $("tgStatus").firstElementChild.className = "dot " + kind;
    $("tgStatus").lastElementChild.textContent = text;
  }
  async function busy(button, action, onError) {
    button.disabled = true;
    try { await action(); } catch (error) {
      if (error.retryAfter) startResendTimer(error.retryAfter);
      onError(error.message);
    } finally { button.disabled = false; if (button.id === "resendCode") updateResend(); if (button.id === "addSources") updateChosen(); }
  }
  function empty(text) {
    const node = document.createElement("div");
    node.className = "empty";
    node.textContent = text;
    $("grid").replaceChildren(node);
  }
  function fmtSize(value) {
    if (value >= 1073741824) return (value / 1073741824).toFixed(1) + " GB";
    if (value >= 1048576) return (value / 1048576).toFixed(1) + " MB";
    return Math.round(value / 1024) + " KB";
  }
  function fmtTime(value) {
    const seconds = Math.floor(Number(value) || 0);
    return Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
  }
  async function boot() {
    try { await api("/api/me"); } catch {
      $("login").classList.remove("hidden");
      $("app").classList.add("hidden");
      return;
    }
    $("login").classList.add("hidden");
    $("app").classList.remove("hidden");
    try {
      await loadStatus();
      if (!accounts.length) { showPanel(true); openLogin(); }
      await restoreLogin();
      await loadCatalog(1);
      refreshStateLater();
    } catch (error) { notice(error.message); }
  }
  async function loadStatus() {
    const data = await api("/api/tg/status");
    accounts = data.accounts || [];
    sources = data.sources || [];
    renderAccounts();
    renderSources();
  }
  function showPanel(admin) {
    $("adminPanel").classList.toggle("hidden", !admin);
    $("libraryPanel").classList.toggle("hidden", admin);
    $("showAdmin").classList.toggle("ghost", !admin);
    $("showLibrary").classList.toggle("ghost", admin);
  }
  function textRow(title, subtitle) {
    const row = document.createElement("div"); row.className = "listRow";
    const text = document.createElement("div"); text.className = "listText";
    const name = document.createElement("b"); name.textContent = title;
    const hint = document.createElement("small"); hint.textContent = subtitle;
    text.append(name, hint); row.append(text);
    return row;
  }
  function actionButton(text, action, className = "ghost") {
    const button = document.createElement("button"); button.textContent = text; button.className = className;
    button.onclick = () => busy(button, action, notice);
    return button;
  }
  function renderAccounts() {
    $("accountList").replaceChildren();
    for (const account of accounts) {
      const subtitle = [account.phone_masked, account.username ? "@" + account.username : "", account.status === "ready" ? "已登录" : account.error, account.source_count + "个来源"].filter(Boolean).join(" · ");
      const row = textRow(account.name, subtitle);
      row.append(actionButton("退出账号", async () => {
        if (!confirm("退出账号“" + account.name + "”并移除它的网盘来源？")) return;
        await post("/api/accounts/" + account.id + "/logout");
        closePlayer(); chosen.clear();
        await loadStatus(); await loadCatalog(1); await loadDialogs(1);
        notice("账号已退出");
        if (!accounts.length) openLogin();
      }, "danger"));
      $("accountList").append(row);
    }
    if (!accounts.length) $("accountList").append(textRow("尚未添加Telegram账号", "点击添加账号，可使用单设备验证码登录"));
    const selected = $("sourceAccount").value;
    $("sourceAccount").replaceChildren(new Option("选择Telegram账号", ""));
    for (const account of accounts) $("sourceAccount").append(new Option(account.name + (account.phone_masked ? " · " + account.phone_masked : ""), account.id));
    $("sourceAccount").value = accounts.some(account => account.id === selected) ? selected : accounts[0]?.id || "";
    if (selected !== $("sourceAccount").value) { chosen.clear(); updateChosen(); dialogGeneration++; }
  }
  function renderSources() {
    $("sourceList").replaceChildren();
    for (const source of sources) {
      const progress = source.error || (source.syncing ? "同步中" : source.history_done ? "已同步" : "等待同步");
      const row = textRow(source.title, [kindLabel[source.kind], source.account_name, source.media_count + "项", progress].join(" · "));
      const actions = document.createElement("div"); actions.className = "listActions";
      actions.append(actionButton("重建索引", async () => {
        if (!confirm("重新扫描“" + source.title + "”的历史媒体？")) return;
        await post("/api/sources/" + source.id + "/sync", { reset: true });
        await loadCatalog(1); notice("已开始重建索引");
      }));
      actions.append(actionButton("移除", async () => {
        await post("/api/sources/" + source.id + "/remove");
        closePlayer(); await loadStatus(); await loadCatalog(1); await loadDialogs(dialogPage);
      }, "danger"));
      row.append(actions); $("sourceList").append(row);
    }
    if (!sources.length) $("sourceList").append(textRow("还没有网盘来源", "在下方搜索并勾选群组和频道"));
    const selected = $("sourceFilter").value;
    $("sourceFilter").replaceChildren(new Option("全部网盘来源", ""));
    for (const source of sources) $("sourceFilter").append(new Option(source.title + " · " + source.account_name, source.id));
    $("sourceFilter").value = sources.some(source => source.id === selected) ? selected : "";
  }
  function refreshStateLater() {
    clearTimeout(stateTimer);
    stateTimer = setTimeout(async () => {
      try {
        if (!document.hidden) {
          const countBefore = sources.reduce((sum, source) => sum + source.media_count, 0);
          await loadStatus();
          const countAfter = sources.reduce((sum, source) => sum + source.media_count, 0);
          if (countBefore !== countAfter || sources.some(source => source.syncing) || $("syncStatus").dataset.syncing === "true") await loadCatalog(currentPage, false, true);
        }
      } catch (error) { notice(error.message); }
      refreshStateLater();
    }, 5000);
  }
  async function loadCatalog(page = 1, refresh = false, quiet = false) {
    const generation = ++catalogGeneration;
    currentPage = page;
    if (!quiet) empty("正在加载媒体…");
    try {
      const params = new URLSearchParams({ page: String(page), q: $("search").value.trim(), source_id: $("sourceFilter").value });
      if (refresh) params.set("refresh", "true");
      const data = await api("/api/catalog?" + params);
      if (generation !== catalogGeneration) return;
      items = data.items || [];
      currentPage = data.page;
      const key = JSON.stringify(items.map(item => [item.source_id, item.id, item.title, item.source_title]));
      if (!quiet || key !== catalogKey) render();
      catalogKey = key;
      $("pageLabel").textContent = "第" + data.page + "/" + data.pages + "页 · 共" + data.total + "项 · 每页30条";
      $("pageNumber").value = data.page;
      $("pageNumber").max = data.pages;
      $("prevPage").disabled = data.page <= 1;
      $("nextPage").disabled = !data.has_next;
      $("syncStatus").dataset.syncing = String(data.syncing);
      const failures = (data.sources || []).filter(source => source.error).map(source => source.title + "：" + (errors[source.error] || source.error));
      $("syncStatus").textContent = (data.syncing ? "正在后台读取历史媒体，索引完成前资源数量会持续增加。" : "已索引" + data.total + "个匹配资源。") + (failures.length ? " " + failures.join("；") : "");
      if (data.sources) { sources = data.sources; renderSources(); }
    } catch (error) { if (generation === catalogGeneration) { if (!quiet) empty(error.message); else notice(error.message); } }
  }
  function updateChosen() {
    $("selectedCount").textContent = "已勾选" + chosen.size + "个（可跨搜索和分页选择）";
    $("addSources").disabled = !chosen.size;
  }
  function renderDialogs() {
    $("dialogList").replaceChildren();
    for (const entry of dialogItems) {
      const row = document.createElement("label"); row.className = "listRow dialogRow";
      const checkbox = document.createElement("input"); checkbox.type = "checkbox";
      checkbox.checked = entry.selected || chosen.has(entry.id); checkbox.disabled = entry.selected;
      checkbox.setAttribute("aria-label", entry.title);
      checkbox.onchange = () => { if (checkbox.checked) chosen.add(entry.id); else chosen.delete(entry.id); updateChosen(); };
      const text = textRow(entry.title, [kindLabel[entry.kind], entry.username ? "@" + entry.username : "", String(entry.id), entry.selected ? "已设为网盘" : ""].filter(Boolean).join(" · ")).firstChild;
      row.append(checkbox, text); $("dialogList").append(row);
    }
    updateChosen();
  }
  async function loadDialogs(page = 1, refresh = false) {
    const generation = ++dialogGeneration;
    const account = $("sourceAccount").value;
    if (!account) { dialogItems = []; renderDialogs(); $("dialogMsg").textContent = "请先添加Telegram账号"; return; }
    $("dialogMsg").textContent = "正在读取群组和频道…";
    try {
      const params = new URLSearchParams({ account_id: account, page: String(page), q: $("dialogSearch").value.trim(), kind: $("dialogKind").value });
      if (refresh) params.set("refresh", "true");
      const data = await api("/api/dialogs?" + params);
      if (generation !== dialogGeneration || account !== $("sourceAccount").value) return;
      dialogItems = data.items; dialogPage = data.page; renderDialogs();
      $("dialogPageLabel").textContent = "第" + data.page + "/" + data.pages + "页";
      $("dialogPrev").disabled = data.page <= 1;
      $("dialogNext").disabled = data.page >= data.pages;
      $("dialogMsg").textContent = "找到" + data.total + "个群组或频道";
    } catch (error) { if (generation === dialogGeneration) $("dialogMsg").textContent = error.message; }
  }
  function render() {
    const list = items;
    $("grid").replaceChildren();
    if (!list.length) { empty(sources.length ? "暂未找到媒体，同步中的来源会持续添加资源" : "到管理面板添加群组和频道作为网盘"); return; }
    list.forEach(item => {
      const card = document.createElement("article");
      card.className = "card item";
      card.tabIndex = 0;
      card.setAttribute("role", "group");
      card.setAttribute("aria-label", item.title);
      const cover = document.createElement("div");
      cover.className = "cover";
      function fallback() {
        const icon = document.createElement("span");
        icon.className = "type";
        icon.textContent = item.kind === "video" ? "▶" : "♫";
        cover.replaceChildren(icon);
      }
      if (item.thumb_url) {
        const image = document.createElement("img");
        image.loading = "lazy";
        image.alt = "";
        image.src = item.thumb_url;
        image.onerror = fallback;
        cover.appendChild(image);
      } else fallback();
      const meta = document.createElement("div");
      meta.className = "meta";
      const title = document.createElement("div");
      title.className = "title";
      title.textContent = item.title;
      const small = document.createElement("div");
      small.className = "small";
      const duration = document.createElement("span");
      duration.textContent = fmtTime(item.duration);
      const size = document.createElement("span");
      size.textContent = fmtSize(item.size);
      small.append(duration, size);
      const sourceTag = document.createElement("div"); sourceTag.className = "sourceTag"; sourceTag.textContent = item.source_title;
      const actions = document.createElement("div"); actions.className = "mediaActions";
      const playButton = document.createElement("button"); playButton.className = "ghost"; playButton.textContent = "播放"; playButton.onclick = () => play(item);
      const download = document.createElement("a"); download.className = "buttonLink ghost downloadLink"; download.textContent = "下载";
      download.href = downloadPath(item); download.rel = "noreferrer";
      actions.append(playButton, download);
      meta.append(title, small, sourceTag, actions);
      card.append(cover, meta);
      card.onclick = event => { if (!event.target.closest("a,button")) play(item); };
      card.onkeydown = event => { if (event.target === card && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); play(item); } };
      $("grid").appendChild(card);
    });
  }
  function downloadPath(item) { return item.download_url || "/api/download/" + item.source_id + "/" + item.id; }
  function closePlayer() {
    playback++;
    for (const id of ["video", "audio"]) {
      $(id).pause();
      $(id).removeAttribute("src");
      $(id).load();
      $(id).classList.add("hidden");
    }
    $("modal").classList.add("hidden");
    $("downloadCurrent").removeAttribute("href");
    $("downloadCurrent").classList.add("hidden");
  }
  async function play(item) {
    closePlayer();
    const current = playback;
    $("playerTitle").textContent = item.title;
    $("downloadCurrent").href = downloadPath(item);
    $("downloadCurrent").classList.remove("hidden");
    $("playerMsg").textContent = "正在获取播放链接…";
    $("modal").classList.remove("hidden");
    $("closePlayer").focus();
    try {
      const data = await api("/api/media-link/" + item.source_id + "/" + item.id);
      if (current !== playback) return;
      const player = $(item.kind === "video" ? "video" : "audio");
      player.classList.remove("hidden");
      player.src = data.stream_url;
      $("playerMsg").textContent = "";
      await player.play().catch(() => {});
    } catch (error) { if (current === playback) $("playerMsg").textContent = error.message; }
  }
  function stopPolling() { clearTimeout(pollTimer); pollTimer = null; loginGeneration++; }
  function openLogin() {
    showPanel(true);
    $("loginBox").classList.remove("hidden");
    $("qrArea").classList.remove("hidden");
    pendingLogin = null;
    $("tgPhone").value = ""; $("tgCode").value = ""; $("tgPassword").value = "";
    showLoginMethod("phone");
    status("添加账号：使用手机号验证码或二维码登录");
  }
  async function restoreLogin() {
    try {
      const pending = await api("/api/tg/login-status");
      if (pending.status !== "completed" && (pending.method === "phone" || pending.status === "password_required")) { openLogin(); await applyLoginState(pending); }
    } catch (error) { if (error.status !== 404) notice(error.message); }
  }
  function showLoginMethod(method) {
    stopPolling();
    loginMethod = method;
    $("phoneArea").classList.toggle("hidden", method !== "phone");
    $("qrPanel").classList.toggle("hidden", method !== "qr");
    for (const [id, active] of [["usePhone", method === "phone"], ["useQr", method === "qr"]]) {
      $(id).classList.toggle("ghost", !active);
      $(id).setAttribute("aria-pressed", String(active));
    }
    const state = pendingLogin?.method === method ? pendingLogin.status : "";
    $("phoneForm").classList.toggle("hidden", method === "phone" && ["code_sent", "password_required", "expired"].includes(state));
    $("codeArea").classList.toggle("hidden", method !== "phone" || !["code_sent", "expired"].includes(state));
    $("passwordArea").classList.toggle("hidden", state !== "password_required");
    if (state === "waiting" && method === "qr") pollTimer = setTimeout(pollLogin, 1500);
  }
  function updateResend() {
    const remaining = Math.max(0, Math.ceil((resendUntil - Date.now()) / 1000));
    $("resendCode").disabled = remaining > 0;
    $("resendCode").textContent = remaining > 0 ? remaining + "秒后可重发" : "重新发送";
    if (!remaining) clearInterval(resendTimer);
  }
  function startResendTimer(seconds) {
    clearInterval(resendTimer);
    resendUntil = Date.now() + seconds * 1000;
    updateResend();
    if (seconds > 0) resendTimer = setInterval(updateResend, 1000);
  }
  async function applyLoginState(data) {
    pendingLogin = { ...data, method: data.method || loginMethod };
    if (data.status === "completed") {
      stopPolling();
      pendingLogin = null;
      $("passwordArea").classList.add("hidden");
      $("qrArea").classList.add("hidden");
      $("loginBox").classList.add("hidden");
      $("tgPhone").value = ""; $("tgCode").value = ""; $("tgPassword").value = "";
      await loadStatus();
      if (data.account_id) $("sourceAccount").value = data.account_id;
      showPanel(true);
      await loadDialogs(1);
      await loadCatalog(1);
      notice("Telegram账号已添加，可以选择群组和频道作为网盘");
      return;
    }
    showLoginMethod(pendingLogin.method);
    if (pendingLogin.method === "phone") {
      const delivery = { telegram: "Telegram官方聊天", sms: "手机短信", call: "电话语音", email: "Telegram提示的邮箱" }[data.delivery] || "Telegram官方聊天";
      $("codeMsg").textContent = "已向 " + (data.phone_masked || "你的账号") + " 发送验证码，请查看" + delivery + "。";
      startResendTimer(data.retry_after || 0);
    }
    if (data.status === "code_sent") status("验证码已发送，查看Telegram后返回此页输入", "busy");
    else if (data.status === "password_required") status("需要两步验证密码", "busy");
    else if (["expired", "failed"].includes(data.status)) status(pendingLogin.method === "phone" ? "验证码已失效，请重新发送" : "二维码已失效，请重新生成");
  }
  async function pollLogin() {
    const generation = loginGeneration;
    try {
      const data = await api("/api/tg/login-status");
      if (generation !== loginGeneration) return;
      if (data.status !== "waiting") await applyLoginState(data);
      else { status("等待Telegram App扫码", "busy"); pollTimer = setTimeout(pollLogin, 1500); }
    } catch (error) { if (generation === loginGeneration) { stopPolling(); status(error.message); } }
  }
  $("adminForm").onsubmit = event => {
    event.preventDefault();
    busy($("adminLogin"), async () => {
      $("loginMsg").textContent = "";
      await post("/api/login", { password: $("adminPassword").value });
      $("adminPassword").value = "";
      await boot();
    }, message => { $("loginMsg").textContent = message; });
  };
  $("logout").onclick = () => busy($("logout"), async () => { await post("/api/logout"); stopPolling(); clearTimeout(stateTimer); closePlayer(); location.reload(); }, notice);
  $("showLibrary").onclick = () => { showPanel(false); loadCatalog(currentPage); };
  $("showAdmin").onclick = () => { showPanel(true); loadDialogs(1); };
  $("addAccount").onclick = openLogin;
  $("closeLogin").onclick = () => busy($("closeLogin"), async () => { stopPolling(); await post("/api/tg/cancel"); pendingLogin = null; $("loginBox").classList.add("hidden"); }, notice);
  $("usePhone").onclick = () => showLoginMethod("phone");
  $("useQr").onclick = () => showLoginMethod("qr");
  $("phoneForm").onsubmit = event => {
    event.preventDefault();
    busy($("sendCode"), async () => {
      stopPolling();
      status("正在发送登录验证码…", "busy");
      const data = await post("/api/tg/send-code", { phone: $("tgPhone").value });
      $("tgCode").value = "";
      await applyLoginState(data);
      $("tgCode").focus();
    }, status);
  };
  $("resendCode").onclick = () => busy($("resendCode"), async () => {
    const data = await post("/api/tg/send-code");
    $("tgCode").value = "";
    await applyLoginState(data);
  }, status);
  $("changePhone").onclick = () => {
    pendingLogin = null;
    $("tgCode").value = "";
    $("tgPassword").value = "";
    showLoginMethod("phone");
    $("tgPhone").focus();
  };
  $("codeForm").onsubmit = event => {
    event.preventDefault();
    busy($("verifyCode"), async () => {
      const data = await post("/api/tg/verify-code", { code: $("tgCode").value });
      $("tgCode").value = "";
      await applyLoginState(data);
    }, status);
  };
  $("startQr").onclick = () => busy($("startQr"), async () => {
    stopPolling();
    $("passwordArea").classList.add("hidden");
    $("tgPassword").value = "";
    status("正在生成二维码…", "busy");
    const data = await post("/api/tg/qr");
    pendingLogin = { method: "qr", status: "waiting" };
    const image = document.createElement("img");
    image.alt = "Telegram登录二维码";
    image.src = data.qr;
    $("qr").replaceChildren(image);
    status("请在" + data.expires_in + "秒内使用Telegram App扫码", "busy");
    pollTimer = setTimeout(pollLogin, 1500);
  }, status);
  $("passwordForm").onsubmit = event => {
    event.preventDefault();
    busy($("submitPassword"), async () => { const data = await post("/api/tg/password", { password: $("tgPassword").value }); $("tgPassword").value = ""; if (data.status) await applyLoginState(data); else await pollLogin(); }, status);
  };
  $("addSources").onclick = () => busy($("addSources"), async () => {
    await post("/api/sources", { account_id: $("sourceAccount").value, chat_ids: [...chosen] });
    chosen.clear(); await loadStatus(); await loadDialogs(dialogPage); await loadCatalog(1);
    notice("网盘来源已添加，正在后台索引历史媒体");
  }, notice);
  $("sourceAccount").onchange = () => { chosen.clear(); updateChosen(); loadDialogs(1); };
  $("dialogKind").onchange = () => loadDialogs(1);
  $("dialogSearch").oninput = () => { dialogGeneration++; clearTimeout(dialogTimer); dialogTimer = setTimeout(() => loadDialogs(1), 300); };
  $("refreshDialogs").onclick = () => busy($("refreshDialogs"), () => loadDialogs(1, true), notice);
  $("dialogPrev").onclick = () => loadDialogs(dialogPage - 1);
  $("dialogNext").onclick = () => loadDialogs(dialogPage + 1);
  $("selectPage").onclick = () => { for (const entry of dialogItems) if (!entry.selected) chosen.add(entry.id); renderDialogs(); };
  $("sourceFilter").onchange = () => loadCatalog(1);
  $("searchForm").onsubmit = event => { event.preventDefault(); clearTimeout(searchTimer); loadCatalog(1); };
  $("search").oninput = () => { catalogGeneration++; clearTimeout(searchTimer); searchTimer = setTimeout(() => loadCatalog(1), 400); };
  $("refresh").onclick = () => busy($("refresh"), () => loadCatalog(1, true), notice);
  $("prevPage").onclick = () => loadCatalog(currentPage - 1);
  $("nextPage").onclick = () => loadCatalog(currentPage + 1);
  $("jumpPage").onclick = () => loadCatalog(Math.max(1, Number($("pageNumber").value) || 1));
  $("closePlayer").onclick = closePlayer;
  $("modal").onclick = event => { if (event.target === $("modal")) closePlayer(); };
  document.addEventListener("keydown", event => { if (event.key === "Escape") closePlayer(); });
  for (const id of ["video", "audio"]) $(id).onerror = () => { $("playerMsg").textContent = "播放失败，可重试；部分格式或编码需要浏览器支持。"; };
  boot();
})();`;
