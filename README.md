# TGDrive

把 Telegram 频道变成可以在浏览器中播放的视频、音乐媒体库。

**Cloudflare Workers 前端 + Python / Telethon 后端，支持单设备验证码登录。**

## 功能

- **手机号验证码登录**：输入带国家码的手机号，在 Telegram 查看登录码后回网页填写，同一台设备即可完成。
- **二维码登录**：使用另一台已登录 Telegram 的设备扫描二维码。
- **两步验证**：支持密码输入、错误重试，以及刷新后恢复待验证状态。
- **多账号管理面板**：添加、重新登录或退出独立的 Telegram 账号，退出一个账号不影响其他账号。
- **多来源网盘**：同时选择多个账号下的频道、超级群和普通群，合并浏览或按来源筛选。
- **群频道搜索**：按名称、@用户名或 ID 搜索账号会话列表；跨搜索结果和分页勾选，批量添加。
- **历史资源分页**：后台建立可恢复的 SQLite 媒体索引，持续扫描可访问历史，每页固定 **30 个资源**，支持页码跳转。
- **搜索与播放**：按标题、文件名和消息内容搜索；视频和音乐内嵌播放，支持 HTTP Range 拖动。
- **原文件下载**：媒体卡片和播放器均提供下载入口，保留原文件名，支持浏览器下载及有效链接期间的 Range 续传。
- **独立部署**：管理密码保护网页，Telegram 会话加密保存到自己的服务器；服务间令牌通过 Worker secrets 配置。

## 架构

```text
浏览器 ── HTTPS ── Cloudflare Worker
                       │ 管理接口 + 服务令牌
                       ▼
                   Nginx HTTPS
                       │
                       ▼
                FastAPI / Telethon ── Telegram

浏览器 ── 带签名的媒体链接 / Range ── Nginx ── 后端
```

Worker 提供网页和管理 API；媒体直接从后端流向浏览器。SQLite 保存账号、加密会话、网盘来源与媒体元数据；索引不保存完整音视频文件，无需 D1 或 R2。

## 项目结构

```text
backend/                 FastAPI、Telethon 和后端测试
worker/src/              Worker 路由、网页和浏览器脚本
worker/tests/            Worker 单元测试和浏览器模拟测试
deploy/                  Nginx 和 systemd 示例
scripts/configure.py     生成本地配置与随机密钥
docs/deployment.md       完整部署说明
```

## 一键部署后端

在 **Debian / Ubuntu、使用 systemd 的服务器上，以 root 身份**运行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/MakkaPakka518/TGDrive/main/install.sh)
```

脚本直接从本仓库获取源码，依次询问：

1. 安装目录和已经解析到本机的后端域名；
2. 前端 HTTPS 地址、本地监听端口；
3. Telegram API ID / API hash；
4. 服务令牌、网页管理密码（均可留空自动生成）；
5. 证书通知邮箱（可留空）。

随后自动安装依赖，验证域名的 HTTP 可达性，申请 HTTPS 证书，配置 Nginx、systemd 和自动续期。请提前设置后端 DNS，并放行 TCP 80、443 端口。

完成后会生成 Worker 的本地配置和密钥文件；继续部署 Worker 即可使用网页。重复运行可更新后端源码，保留账号数据和原有密钥，并创建备份。

完整说明与仅查看部署计划的方法见 [部署文档](docs/deployment.md)。

## 手动部署

需要 Python 3.11、Node.js 22.17+、可访问 Telegram 的服务器、Telegram API ID/hash，以及 Cloudflare Workers 账号。

1. 将仓库下载或克隆到服务器，安装后端依赖：

   ```bash
   python3 -m venv backend/.venv
   backend/.venv/bin/pip install -r backend/requirements.txt
   ```

2. 在 [my.telegram.org](https://my.telegram.org/) 获取自己的 API ID/hash，准备前后端 HTTPS 地址，然后生成私有配置：

   ```bash
   backend/.venv/bin/python scripts/configure.py
   ```

   脚本交互式读取配置，生成随机管理密码和密钥，并写入已被 Git 忽略的本地文件。已有配置时拒绝覆盖。

3. 参考 [部署文档](docs/deployment.md) 启动后端、配置 Nginx HTTPS。

4. 部署 Worker：

   ```bash
   npm --prefix worker ci
   cd worker
   npx wrangler login
   npm run deploy
   ```

5. 打开自己的前端地址，在「管理面板」添加 Telegram 账号，搜索并勾选群／频道，点击「将勾选项设为网盘」。媒体库会显示索引进度和30条一页的分页结果。

所有文档域名均使用 `example.com` 或 `.test` 示例。部署时填写自己的配置。

## 配置对应关系

| 后端 | Worker | 作用 |
| --- | --- | --- |
| `SERVICE_TOKEN` | `BACKEND_TOKEN` | 服务间鉴权，二者必须一致 |
| `PUBLIC_BASE` | `BACKEND_URL` | 后端的 HTTPS 公网基址，包含反向代理前缀 |
| `FRONTEND_ORIGIN` | 实际网页来源 | 后端 CORS 允许的前端 origin |
| `SESSION_ENCRYPTION_KEY` | — | 加密 Telegram 会话 |
| `MEDIA_SECRET` | — | 签名音视频和缩略图链接 |
| — | `SESSION_SECRET` | 签名管理登录 cookie |
| — | `ADMIN_PASSWORD` | 网页管理密码 |

## 测试

```bash
backend/.venv/bin/python backend/test_app.py
backend/.venv/bin/python scripts/test_installer.py
bash -n install.sh
npm --prefix worker test
npm --prefix worker run build
```

浏览器模拟测试：

```bash
cd worker
npx playwright install --with-deps chromium
npm run test:browser
```

测试使用虚构数据和模拟 Telegram 客户端，覆盖验证码、重发、两步验证、页面恢复、会话加密、媒体字节范围及签名校验，不需要真实账号。

GitHub Actions 示例位于 `deploy/github-actions.example.yml`。需要自动运行测试时，将其复制到 `.github/workflows/tests.yml`；使用 Personal Access Token 推送工作流文件时，Token 需要相应的 `workflow` 权限。

## 当前边界

- 支持多账号、多群频道来源；后端需保持一个 Uvicorn worker，登录过程和同步任务保存在内存。
- 新来源首次扫描历史需要时间，索引完成前总数和页数会持续增加。扫描进度持久化，重启后继续。
- 群频道搜索覆盖账号已加入或已出现在会话列表中的群和频道。
- 使用浏览器原生播放器，无转码；可播放格式取决于浏览器的音视频编码支持。
- 管理 cookie 和媒体签名默认有效12小时；签名绑定账号、来源和消息，移除来源后链接失效。
- 「退出管理」清除网页管理会话；管理面板里的「退出账号」会退出对应 Telegram 会话并移除该账号的本地来源与索引。
- 常规刷新收录新增消息；需要更新历史编辑或删除记录时，可在来源管理中重建索引。
- 密码失败计数位于 Worker isolate 内存，不是跨所有边缘节点的全局限流。

## 开源与隐私

仓库仅包含源码、虚构测试数据和配置模板。真实手机号、账号会话、数据库、密钥、私有域名／IP 和运行日志应保留在自己的部署环境中。请勿把本地 `.env`、`.secrets.json` 或 `data/` 提交到版本库。

许可证：[MIT](LICENSE)。
