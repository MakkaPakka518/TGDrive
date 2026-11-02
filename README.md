# TGDrive

把 Telegram 频道变成可以在浏览器中播放的视频、音乐媒体库。

**Cloudflare Workers 前端 + Python / Telethon 后端，支持单设备验证码登录。**

## 功能

- **手机号验证码登录**：输入带国家码的手机号，在 Telegram 查看登录码后回网页填写，同一台设备即可完成。
- **二维码登录**：使用另一台已登录 Telegram 的设备扫描二维码。
- **两步验证**：支持密码输入、错误重试，以及刷新后恢复待验证状态。
- **频道媒体库**：从账号可访问的频道／超级群组中选择媒体来源，读取最近 200 条消息中的音视频。
- **搜索与播放**：按标题、文件名和消息内容搜索；视频和音乐内嵌播放，支持 HTTP Range 拖动。
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

Worker 提供网页和管理 API；媒体直接从后端流向浏览器。后端使用内存中的 Telethon 登录过程和磁盘上的加密账号会话，无需 D1 或 R2。

## 项目结构

```text
backend/                 FastAPI、Telethon 和后端测试
worker/src/              Worker 路由、网页和浏览器脚本
worker/tests/            Worker 单元测试和浏览器模拟测试
deploy/                  Nginx 和 systemd 示例
scripts/configure.py     生成本地配置与随机密钥
docs/deployment.md       完整部署说明
```

## 快速开始

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

5. 打开自己的前端地址，输入生成的管理密码，登录 Telegram 后选择频道。

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

- 单实例、单个 Telegram 账号和单个选中频道；后端需保持一个 Uvicorn worker，登录中的状态保存在内存。
- 扫描范围是频道最近 **200 条消息**，并非完整历史索引。
- 使用浏览器原生播放器，无转码；可播放格式取决于浏览器的音视频编码支持。
- 管理 cookie 和媒体签名默认有效 12 小时；切换频道会使旧频道链接失效。
- 网页「退出」清除浏览器管理 cookie，后端 Telegram 会话继续保留。
- 密码失败计数位于 Worker isolate 内存，不是跨所有边缘节点的全局限流。

## 开源与隐私

仓库仅包含源码、虚构测试数据和配置模板。真实手机号、账号会话、数据库、密钥、私有域名／IP 和运行日志应保留在自己的部署环境中。请勿把本地 `.env`、`.secrets.json` 或 `data/` 提交到版本库。

许可证：[MIT](LICENSE)。
