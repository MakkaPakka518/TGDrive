# 部署 TGDrive

## 一键交互式部署

适用于 Debian / Ubuntu（Python 3.8+、systemd）。先将自己的后端域名通过 A / AAAA 记录解析到服务器，Cloudflare 使用「仅 DNS」，并放行 TCP 80 / 443。

以 root 登录服务器后运行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/MakkaPakka518/TGDrive/main/install.sh)
```

安装脚本来自本仓库，会获取 `main` 分支源码。通过 SSH 终端运行即可；所有询问从终端读取，API hash、服务令牌和管理密码输入时隐藏。

| 询问项 | 说明 |
| --- | --- |
| 安装目录 | 默认 `/opt/tgdrive` |
| 已解析的后端域名 | 例如 `api.example.com`，脚本不会自行选择第三方域名 |
| 前端地址 | 自己的 HTTPS 域名或完整 `workers.dev` 地址 |
| 本地端口 | 默认3219，仅监听 `127.0.0.1` |
| Telegram API ID/hash | 使用自己的开发者 API 配置 |
| 服务令牌、管理密码 | 首次留空自动生成；升级留空保留 |
| 证书邮箱 | 可留空 |

脚本自动完成 Python 环境和依赖安装、HTTP挑战校验、Certbot证书签发、Nginx流式代理、systemd服务和证书自动续期。已有冲突端口或其他站点占用该域名时会停止并给出提示。

完成后的文件：

- `backend/.env`：后端配置；
- `backend/data/`：默认的数据目录；
- `worker/.secrets.json`、`worker/wrangler.local.jsonc`：部署 Worker 所需的私有配置；
- `.venvs/`：各次安装的独立 Python 环境；
- `backups/`：升级前的源码、配置及数据备份；
- `.install.local.json`：当前安装参数。

这些私有配置和数据目录都已被 Git 忽略。后端服务名为 `tgdrive.service`；证书通过 `certbot.timer` 自动续期，更新后会检查并重载 Nginx。

后端部署完成后，安装 Node.js 22.17+，到安装目录的 `worker/` 下执行 `npm ci`、`npx wrangler login`、`npm run deploy` 部署前端。脚本不需要 Cloudflare DNS Token。

只询问并查看计划、不修改系统：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/MakkaPakka518/TGDrive/main/install.sh) --dry-run
```

此模式需要本机已有 `git` 和 `python3`。重复正式运行安装命令时，填写相同安装目录即可升级；已经保存的密钥默认保留，防止已登录账号的加密会话失效。

## 以下为手动部署步骤

## 1. 准备

- 一台可连接 Telegram 的 Linux 服务器，Python 3.11，Nginx。
- Cloudflare Workers 账号和 Node.js 22.17+。
- 自己的 Telegram API ID/hash，可在 `https://my.telegram.org/` 申请。
- 后端 HTTPS 地址，例如 `https://api.example.com/tgdrive-api`。
- 前端 HTTPS 地址，例如 `https://drive.example.com`，也可以使用自己的 `workers.dev` 子域名。

以下采用安装路径 `/opt/tgdrive`；这是通用示例，可按实际目录修改。

## 2. 安装后端并生成配置

将源码置于 `/opt/tgdrive`，在仓库根目录运行：

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
backend/.venv/bin/python scripts/configure.py
```

按提示输入 API 配置、后端公网基址和前端 origin。脚本创建：

- `backend/.env`：后端环境变量；
- `worker/.secrets.json`：管理密码、cookie 签名密钥、服务令牌；
- `worker/wrangler.local.jsonc`：当前部署专用的 Worker 配置。

文件权限为 `0600`，均被 `.gitignore` 排除。脚本输出管理密码，并避免覆盖已有密钥。

如手动配置，可参考 `backend/.env.example`、`worker/.secrets.example.json` 和 `worker/wrangler.jsonc`。Fernet 会话密钥必须由 `cryptography.fernet.Fernet.generate_key()` 生成；其他密钥使用密码学安全随机数。确保后端 `SERVICE_TOKEN` 与 Worker `BACKEND_TOKEN` 完全一致。

`FRONTEND_ORIGIN` 只填 origin，不含路径，例如 `https://drive.example.com`。`PUBLIC_BASE` 与 `BACKEND_URL` 则要包含 Nginx 的 `/tgdrive-api` 前缀。浏览器的媒体 CSP 会从 `BACKEND_URL` 自动生成。

## 3. 启动后端

临时运行（在 `backend/` 目录）：

```bash
set -a
. ./.env
set +a
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 3219
```

长期运行可使用提供的 systemd 单元：

```bash
sudo useradd --system --home /opt/tgdrive --shell /usr/sbin/nologin tgdrive
sudo chown -R tgdrive:tgdrive /opt/tgdrive/backend
sudo cp deploy/tgdrive.service /etc/systemd/system/tgdrive.service
sudo systemctl daemon-reload
sudo systemctl enable --now tgdrive.service
curl -fsS http://127.0.0.1:3219/health
```

若用户 `tgdrive` 已存在，跳过创建用户命令。安装目录不同则修改 service 中的路径。保持单进程运行，避免把同一个加密会话同时交给多个实例。

## 4. Nginx 和 HTTPS

为自己的后端域名配置 DNS，并申请有效的 TLS 证书。将 `deploy/nginx.conf.example` 中的 `api.example.com` 和证书路径替换为自己的值，再安装：

```bash
sudo cp deploy/nginx.conf.example /etc/nginx/conf.d/tgdrive.conf
sudo nginx -t
sudo systemctl reload nginx
```

示例已设置关闭代理缓冲和较长读取超时，支持音视频持续传输以及客户端 Range 请求。

验证自己的公网地址：

```bash
curl -fsS https://api.example.com/tgdrive-api/health
```

应返回 `{"ok":true}`。`/v1/*` 管理 API 要求服务令牌，媒体 API 要求有效签名。

## 5. 部署 Cloudflare Worker

在 `worker/` 目录运行：

```bash
npm ci
npx wrangler login
npm test
npm run build
npm run deploy
```

部署使用 `wrangler.local.jsonc` 和 `.secrets.json`。`configure.py` 会根据前端 URL 设置自定义域名，或使用 `<worker-name>.<account-subdomain>.workers.dev`。

多 Cloudflare 账号时，在本地配置填写自己的 `account_id` 或设置 `CLOUDFLARE_ACCOUNT_ID`。Token 部署时通过 `CLOUDFLARE_API_TOKEN` 环境变量提供，不写入仓库。自定义域应属于所选账号；若需要明确指定区域，可在本地 `routes` 条目添加自己的 `zone_id`。

若前端域名有调整，同步更新后端 `FRONTEND_ORIGIN` 并重启服务。若后端域名有调整，同步更新 `PUBLIC_BASE`、`BACKEND_URL`、Nginx 和证书。

## 6. 首次登录

1. 打开前端，输入管理密码。
2. 默认选择验证码登录：输入国家码和手机号，发送验证码。
3. 按网页提示，从 Telegram 官方聊天、短信或实际发送方式读取登录码。
4. 回到网页输入验证码；如启用了两步验证，继续输入密码。
5. 打开「管理面板」，选择账号，搜索群／频道，勾选多个结果并点击「将勾选项设为网盘」。可跨分页和搜索结果累计勾选。
6. 到媒体库查看资源：每页30项，可按来源筛选、跨已索引资源搜索、前后翻页或输入页码跳转。

管理面板可继续添加其他账号，也可退出指定账号。账号会话相互隔离；普通群、超级群、频道均可添加。

验证码流程支持切换应用后返回、刷新恢复、重发倒计时及错误重试。验证码、两步验证密码仅用于当前请求，不写入配置文件；等待登录的手机号和 code hash 暂存于服务内存。

二维码登录在另一个标签入口，需要另一台已登录 Telegram 的设备扫描。

## 7. 维护与备份

```bash
sudo systemctl status tgdrive.service
sudo journalctl -u tgdrive.service --since "10 minutes ago"
```

持久化文件位于后端 `DATA_DIR`：

- `tgdrive.db`：SQLite 账号注册表、加密 Telegram 会话、网盘来源及媒体索引；
- `tgdrive.db-wal`、`tgdrive.db-shm`：SQLite 运行时文件。

备份数据库时先停止服务，或使用 SQLite 的在线备份 API，以保证一致性。旧版的 `telegram.session.enc` 和 `config.json` 会在首次升级时自动迁移，并保留原文件；数据库记录迁移完成状态，退出账号后不会从旧文件重新导入。

同步任务按批次记录历史游标，后台读取全部可访问历史，重启会继续。首次添加大频道时，界面显示同步状态，媒体总数随索引增加。移除网盘来源仅移除本地索引，不删除 Telegram 消息。

备份时同时保留后端 `.env` 中的 `SESSION_ENCRYPTION_KEY`。丢失该密钥将无法解密原会话。升级源码应保留 `.env`、本地 Worker 配置、Worker secrets 和 `data/`，然后重新安装依赖、运行测试并重启服务。

管理密码可在本地 `.secrets.json` 修改后重新部署。需要使已有管理 cookie 失效时，可同步更换 `SESSION_SECRET`。

## 常见问题

| 现象 | 检查 |
| --- | --- |
| 网页提示后端不可连接 | 后端健康接口、有效 HTTPS 证书、Nginx 前缀、`BACKEND_URL` |
| 服务间请求被拒绝 | `BACKEND_TOKEN` 与 `SERVICE_TOKEN` 是否一致 |
| 验证码未收到 | Telegram 官方聊天、页面显示的发送方式、国家码，以及 Telegram 的频率限制 |
| 刷新后登录会话失效 | 等待验证码的会话已超时，或后端被重启；重新开始登录 |
| 媒体显示但无法播放 | 浏览器编码兼容性、签名有效期、后端媒体链接和 Range 响应 |
| 尚未找到旧媒体 | 查看来源同步进度与错误提示；首次历史索引可能仍在进行 |
| 历史媒体信息有变动 | 在管理面板对对应来源执行「重建索引」 |
| 下载文件 | 点击媒体卡片「下载」或播放器「下载原文件」；由后端直接流式发送，文件名保持原名 |
| 续传链接失效 | 下载链接默认12小时有效，过期后回网页重新点击下载 |
