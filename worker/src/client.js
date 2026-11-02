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
  const errors = {
    unauthorized: "管理登录已过期，请刷新页面重新登录",
    telegram_login_required: "请先登录Telegram",
    telegram_already_authorized: "Telegram已经登录，请刷新页面",
    channel_not_selected: "请先选择网盘频道",
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
  function status(text, kind = "") {
    $("tgStatus").firstElementChild.className = "dot " + kind;
    $("tgStatus").lastElementChild.textContent = text;
  }
  async function busy(button, action, onError) {
    button.disabled = true;
    try { await action(); } catch (error) {
      if (error.retryAfter) startResendTimer(error.retryAfter);
      onError(error.message);
    } finally { button.disabled = false; if (button.id === "resendCode") updateResend(); }
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
    try { await loadStatus(); } catch (error) { status(error.message); }
  }
  async function loadStatus() {
    const data = await api("/api/tg/status");
    $("qrArea").classList.toggle("hidden", data.authorized);
    if (!data.authorized) {
      status("Telegram尚未登录，可使用手机号验证码登录");
      try {
        const pending = await api("/api/tg/login-status");
        if (pending.status !== "completed" && (pending.method === "phone" || pending.status === "password_required")) await applyLoginState(pending);
      } catch (error) { if (error.status !== 404) status(error.message); }
      return;
    }
    stopPolling();
    clearInterval(resendTimer);
    $("tgPhone").value = "";
    $("tgCode").value = "";
    $("tgPassword").value = "";
    status("已登录：" + data.user.name, "ok");
    const result = await api("/api/channels");
    $("channels").replaceChildren(new Option("选择频道", ""));
    result.channels.forEach(channel => $("channels").appendChild(new Option(channel.title + (channel.username ? " (@" + channel.username + ")" : ""), String(channel.id))));
    $("channels").disabled = false;
    $("saveChannel").disabled = false;
    if (data.channel_id) {
      $("channels").value = String(data.channel_id);
      $("refresh").disabled = false;
      await loadCatalog();
    }
  }
  async function loadCatalog() {
    empty("正在同步频道媒体…");
    try {
      const data = await api("/api/catalog");
      items = data.items || [];
      if (data.channel?.title) $("channelMsg").textContent = "已选择：" + data.channel.title;
      render();
    } catch (error) { items = []; empty(error.message); }
  }
  function render() {
    const query = $("search").value.trim().toLowerCase();
    const list = items.filter(item => !query || [item.title, item.file_name, item.description, item.performer].join(" ").toLowerCase().includes(query));
    $("grid").replaceChildren();
    if (!list.length) { empty("没有找到视频或音乐"); return; }
    list.forEach(item => {
      const card = document.createElement("article");
      card.className = "card item";
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.setAttribute("aria-label", "播放 " + item.title);
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
      meta.append(title, small);
      card.append(cover, meta);
      card.onclick = () => play(item);
      card.onkeydown = event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); play(item); } };
      $("grid").appendChild(card);
    });
  }
  function closePlayer() {
    playback++;
    for (const id of ["video", "audio"]) {
      $(id).pause();
      $(id).removeAttribute("src");
      $(id).load();
      $(id).classList.add("hidden");
    }
    $("modal").classList.add("hidden");
  }
  async function play(item) {
    closePlayer();
    const current = playback;
    $("playerTitle").textContent = item.title;
    $("playerMsg").textContent = "正在获取播放链接…";
    $("modal").classList.remove("hidden");
    $("closePlayer").focus();
    try {
      const data = await api("/api/media-link/" + item.id);
      if (current !== playback) return;
      const player = $(item.kind === "video" ? "video" : "audio");
      player.classList.remove("hidden");
      player.src = data.stream_url;
      $("playerMsg").textContent = "";
      await player.play().catch(() => {});
    } catch (error) { if (current === playback) $("playerMsg").textContent = error.message; }
  }
  function stopPolling() { clearTimeout(pollTimer); pollTimer = null; loginGeneration++; }
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
      await loadStatus();
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
  $("logout").onclick = () => busy($("logout"), async () => { await post("/api/logout"); stopPolling(); closePlayer(); location.reload(); }, status);
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
  $("saveChannel").onclick = () => busy($("saveChannel"), async () => {
    if (!$("channels").value) throw new Error("请选择频道");
    await post("/api/channel", { channel_id: Number($("channels").value) });
    closePlayer();
    $("refresh").disabled = false;
    await loadCatalog();
  }, message => { $("channelMsg").textContent = message; });
  $("refresh").onclick = () => busy($("refresh"), loadCatalog, empty);
  $("search").oninput = render;
  $("closePlayer").onclick = closePlayer;
  $("modal").onclick = event => { if (event.target === $("modal")) closePlayer(); };
  document.addEventListener("keydown", event => { if (event.key === "Escape") closePlayer(); });
  for (const id of ["video", "audio"]) $(id).onerror = () => { $("playerMsg").textContent = "播放失败，可重试；部分格式或编码需要浏览器支持。"; };
  boot();
})();`;
