# 部署 TGDrive

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
5. 选择频道，点击「设为网盘频道」。

验证码流程支持切换应用后返回、刷新恢复、重发倒计时及错误重试。验证码、两步验证密码仅用于当前请求，不写入配置文件；等待登录的手机号和 code hash 暂存于服务内存。

二维码登录在另一个标签入口，需要另一台已登录 Telegram 的设备扫描。

## 7. 维护与备份

```bash
sudo systemctl status tgdrive.service
sudo journalctl -u tgdrive.service --since "10 minutes ago"
```

持久化文件位于后端 `DATA_DIR`：

- `telegram.session.enc`：加密的 Telegram 会话；
- `config.json`：选中频道的配置。

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
| 无法找到旧媒体 | 当前只扫描最近200条消息 |
