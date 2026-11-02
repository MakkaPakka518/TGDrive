#!/usr/bin/env python3
"""Interactive backend installer. No Cloudflare DNS credentials are needed."""
import argparse
import base64
import getpass
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

MARKER = "# Managed by TGDrive installer"


def domain_name(value):
    value = value.strip().lower()
    if value.startswith("https://") or value.startswith("http://"):
        parsed = urlsplit(value)
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.port:
            raise ValueError("后端域名不要包含路径、端口或账号信息")
        value = parsed.hostname or ""
    value = value.rstrip(".").encode("idna").decode("ascii")
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or not re.fullmatch(r"[a-z][a-z0-9-]*", labels[-1]) or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise ValueError("请输入有效域名，例如 api.example.com")
    return value


def frontend_origin(value):
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("前端地址必须是没有路径的HTTPS地址")
    host = domain_name(parsed.hostname or "")
    if parsed.port and parsed.port != 443:
        raise ValueError("前端地址请使用标准HTTPS端口")
    return "https://" + host


def installation_path(value):
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in Path(value).parts:
        raise ValueError("安装目录必须是绝对路径，不包含空格或特殊字符")
    path = Path(value).resolve()
    if str(path) in {"/", "/etc", "/usr", "/var", "/opt", "/home", "/root", "/srv"}:
        raise ValueError("请选择独立的应用目录，例如 /opt/tgdrive")
    if path.exists() and any(path.iterdir()) and not (path / "backend/app.py").exists():
        try:
            managed = json.loads((path / ".install.local.json").read_text()).get("managed_by") == "TGDrive"
        except (OSError, ValueError):
            managed = False
        if not managed:
            raise ValueError("目标目录非空且不是TGDrive安装，请选择其他目录")
    if any(parent.exists() and not parent.stat().st_mode & 0o001 for parent in path.parents):
        raise ValueError("父目录不允许服务用户访问，请使用 /opt/tgdrive 等可访问目录")
    return path


def read_env(path):
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            parts = shlex.split(value)
            values[key.strip()] = parts[0] if len(parts) == 1 else value
    return values


def ask(label, default="", secret=False):
    suffix = "（留空保留）" if secret and default else (" [" + str(default) + "]" if default else "")
    value = (getpass.getpass if secret else input)(label + suffix + ": ").strip()
    return value or default


def collect_settings():
    print("TGDrive 后端交互式部署；请先将后端域名解析到本机，并放行 TCP 80/443。")
    root = installation_path(ask("安装目录", "/opt/tgdrive"))
    existing = read_env(root / "backend/.env")
    metadata = root / ".install.local.json"
    old_settings = json.loads(metadata.read_text()) if metadata.exists() else {}
    previous_host = urlsplit(existing.get("PUBLIC_BASE", "")).hostname or old_settings.get("domain", "")
    domain = domain_name(ask("已经解析到本机的后端域名（仅DNS/灰云）", previous_host))
    try:
        socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError("后端域名尚未解析，请添加DNS记录后重试") from None
    frontend = frontend_origin(ask("前端HTTPS地址（自己的域名或 workers.dev）", existing.get("FRONTEND_ORIGIN", "") or old_settings.get("frontend", "")))
    port = int(ask("后端本地监听端口", str(old_settings.get("port", 3219))))
    if not 1024 <= port <= 65535:
        raise ValueError("端口必须在1024至65535之间")
    api_id = ask("Telegram API ID", existing.get("TG_API_ID", ""))
    api_hash = ask("Telegram API hash", existing.get("TG_API_HASH", ""), secret=True)
    if not api_id.isdigit() or int(api_id) <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ValueError("Telegram API ID/hash格式不正确")
    token = ask("后端服务令牌（首次留空自动生成）", existing.get("SERVICE_TOKEN", ""), secret=True) or secrets.token_hex(32)
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", token):
        raise ValueError("服务令牌须为24至128位字母、数字、下划线或连字符")
    secret_path = root / "worker/.secrets.json"
    worker_secrets = json.loads(secret_path.read_text()) if secret_path.exists() else {}
    password = ask("网页管理密码（首次留空自动生成）", worker_secrets.get("ADMIN_PASSWORD", ""), secret=True) or secrets.token_urlsafe(24)
    email = ask("证书通知邮箱（可留空）", old_settings.get("email", ""))
    if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("邮箱格式不正确")
    return {"root": root, "domain": domain, "frontend": frontend, "port": port, "api_id": api_id, "api_hash": api_hash, "token": token, "password": password, "email": email, "existing_env": existing, "worker_secrets": worker_secrets}


def private_configuration(settings, template):
    env = dict(settings["existing_env"])
    env.update(TG_API_ID=settings["api_id"], TG_API_HASH=settings["api_hash"], SERVICE_TOKEN=settings["token"], PUBLIC_BASE="https://" + settings["domain"] + "/tgdrive-api", FRONTEND_ORIGIN=settings["frontend"])
    if not env.get("MEDIA_SECRET"):
        env["MEDIA_SECRET"] = secrets.token_hex(32)
    if not env.get("SESSION_ENCRYPTION_KEY"):
        env["SESSION_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    if not env.get("DATA_DIR"):
        env["DATA_DIR"] = "./data"
    if any("\n" in value or "\r" in value or "\x00" in value for value in env.values()):
        raise ValueError("环境变量不能包含换行或空字符")
    worker = dict(settings["worker_secrets"])
    worker.update(ADMIN_PASSWORD=settings["password"], BACKEND_TOKEN=settings["token"])
    if not worker.get("SESSION_SECRET"):
        worker["SESSION_SECRET"] = secrets.token_hex(32)
    config = json.loads(json.dumps(template))
    config.setdefault("vars", {})["BACKEND_URL"] = env["PUBLIC_BASE"]
    hostname = urlsplit(settings["frontend"]).hostname
    if hostname.endswith(".workers.dev"):
        if len(hostname.split(".")) != 4:
            raise ValueError("workers.dev地址应为 <Worker名称>.<账号子域名>.workers.dev")
        config["name"] = hostname.split(".")[0]
        config["workers_dev"] = True
    else:
        config["workers_dev"] = False
        routes = config.setdefault("routes", [])
        if not any(isinstance(route, dict) and route.get("pattern") == hostname for route in routes):
            routes.append({"pattern": hostname, "custom_domain": True})
    return env, worker, config


def http_config(domain, webroot):
    return f"""{MARKER}
server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    location ^~ /.well-known/acme-challenge/ {{ root {webroot}; }}
    location / {{ return 301 https://$host$request_uri; }}
}}
"""


def https_config(domain, certificates, port):
    return f"""{MARKER}
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain};
    ssl_certificate {certificates}/fullchain.pem;
    ssl_certificate_key {certificates}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    location /tgdrive-api/ {{
        proxy_pass http://127.0.0.1:{port}/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        client_max_body_size 64k;
    }}
}}
"""


def service_config(root, port, python=None):
    python = python or root / "backend/.venv/bin/python"
    return f"""{MARKER}
[Unit]
Description=TGDrive Telegram media backend
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=tgdrive
Group=tgdrive
WorkingDirectory={root}/backend
EnvironmentFile={root}/backend/.env
ExecStart={python} -m uvicorn app:app --host 127.0.0.1 --port {port}
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
[Install]
WantedBy=multi-user.target
"""


def run(command, check=True, capture=False):
    return subprocess.run([str(value) for value in command], check=check, text=True, capture_output=capture)


def fetch(url):
    with urllib.request.urlopen(url, timeout=15) as response:
        return response.read()


class Installer:
    def __init__(self, source, settings, system_root=Path("/"), runner=run, fetcher=fetch):
        self.source, self.settings = Path(source), settings
        self.root, self.system = settings["root"], system_root
        self.run, self.fetch = runner, fetcher
        self.http = self.system / "etc/nginx/conf.d/tgdrive-acme.conf"
        self.https = self.system / "etc/nginx/conf.d/tgdrive.conf"
        self.unit = self.system / "etc/systemd/system/tgdrive.service"
        self.webroot = self.system / "var/lib/tgdrive-acme"
        self.certificates = self.system / "etc/letsencrypt/live" / settings["domain"]
        self.backup = self.root / "backups" / ("install-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
        self.originals = {}

    def write(self, path, contents, mode=0o644):
        path = Path(path)
        if path not in self.originals:
            previous = path.read_bytes() if path.exists() else None
            attributes = path.stat() if previous is not None else None
            self.originals[path] = (previous, attributes.st_mode & 0o777 if attributes else mode, attributes.st_uid if attributes else os.getuid(), attributes.st_gid if attributes else os.getgid())
            if previous is not None:
                saved = self.backup / (str(len(self.originals)) + "-" + path.name)
                saved.write_bytes(previous)
                saved.chmod(0o600)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tgdrive-tmp")
        temp.write_bytes(contents.encode() if isinstance(contents, str) else contents)
        temp.chmod(mode)
        temp.replace(path)

    def preflight(self):
        for path in (self.http, self.https):
            if path.exists() and MARKER not in path.read_text():
                raise RuntimeError("已有非本脚本管理的Nginx配置，请先检查：" + str(path))
        if self.unit.exists() and ("WorkingDirectory=" + str(self.root / "backend")) not in self.unit.read_text():
            raise RuntimeError("tgdrive.service已用于其他目录，停止覆盖")
        active = self.run(["systemctl", "is-active", "--quiet", "tgdrive.service"], check=False).returncode == 0
        if active and not self.unit.exists():
            raise RuntimeError("已有其他位置的tgdrive.service正在运行，请先检查服务配置")
        own_port = active and re.search(r"--port\s+" + str(self.settings["port"]) + r"(?:\s|$)", self.unit.read_text())
        if not own_port:
            with socket.socket() as listener:
                try:
                    listener.bind(("127.0.0.1", self.settings["port"]))
                except OSError:
                    raise RuntimeError("监听端口已被其他服务占用，请重新选择端口") from None
        result = self.run(["nginx", "-T"], capture=True)
        blocks = re.finditer(r"(?ms)^# configuration file ([^\n]+):\n(.*?)(?=^# configuration file |\Z)", result.stdout)
        for block in blocks:
            if block[1] in {str(self.http), str(self.https)}:
                continue
            content = re.sub(r"(?m)#.*$", "", block[2])
            if any(self.settings["domain"] in names.split() for names in re.findall(r"\bserver_name\s+([^;]+);", content)):
                raise RuntimeError("后端域名已经由其他Nginx配置使用：" + block[1])

    def copy_source(self):
        excluded = {".git", ".venv", ".venvs", "venv", "node_modules", "data", "backups", "__pycache__", ".wrangler", "dist", "test-results", "playwright-report"}
        for path in self.source.rglob("*"):
            relative = path.relative_to(self.source)
            if path.is_symlink() or not path.is_file() or any(part in excluded for part in relative.parts):
                continue
            if path.name in {".env", ".secrets.json", ".admin-password", ".dev.vars"} or path.name.endswith((".local.json", ".local.jsonc")):
                continue
            self.write(self.root / relative, path.read_bytes(), 0o755 if path.name == "install.sh" else 0o644)

    def install(self):
        self.preflight()
        existing_config = self.root / "worker/wrangler.local.jsonc"
        template = json.loads((existing_config if existing_config.exists() else self.source / "worker/wrangler.jsonc").read_text())
        env, worker, config = private_configuration(self.settings, template)
        self.backup.mkdir(parents=True, mode=0o700)
        (self.root / "backups").chmod(0o700)
        # Keep previous interpreters intact so a failed upgrade can restart the old service.
        python = self.root / ".venvs" / self.backup.name / "bin/python"
        try:
            installation = {"managed_by": "TGDrive", "state": "preparing", **{key: self.settings[key] for key in ["domain", "frontend", "port", "email"]}}
            self.write(self.root / ".install.local.json", json.dumps(installation) + "\n", 0o600)
            python.parent.parent.parent.mkdir(parents=True, exist_ok=True)
            self.run(["python3", "-m", "venv", python.parent.parent])
            self.run([python, "-m", "pip", "install", "-r", self.source / "backend/requirements.txt"])
            self.write(self.http, http_config(self.settings["domain"], self.webroot))
            self.run(["nginx", "-t"])
            self.run(["systemctl", "enable", "--now", "nginx"])
            self.run(["systemctl", "reload", "nginx"])
            challenge = secrets.token_hex(20)
            challenge_file = self.webroot / ".well-known/acme-challenge" / ("tgdrive-" + challenge)
            challenge_file.parent.mkdir(parents=True, exist_ok=True)
            for directory in [self.webroot, self.webroot / ".well-known", challenge_file.parent]:
                directory.chmod(0o755)
            challenge_file.write_text(challenge)
            challenge_file.chmod(0o644)
            try:
                actual = self.fetch("http://" + self.settings["domain"] + "/.well-known/acme-challenge/" + challenge_file.name).decode()
                if actual != challenge:
                    raise RuntimeError("HTTP验证未到达本机，请检查DNS、80端口和Cloudflare灰云设置")
            finally:
                challenge_file.unlink(missing_ok=True)
            command = ["certbot", "certonly", "--webroot", "-w", self.webroot, "-d", self.settings["domain"], "--cert-name", self.settings["domain"], "--non-interactive", "--agree-tos", "--keep-until-expiring"]
            command += ["--email", self.settings["email"]] if self.settings["email"] else ["--register-unsafely-without-email"]
            self.run(command)
            for name in ("fullchain.pem", "privkey.pem"):
                if not (self.certificates / name).exists():
                    raise RuntimeError("证书文件未生成")
            self.run(["systemctl", "stop", "tgdrive.service"], check=False)
            data_path = Path(env["DATA_DIR"])
            if not data_path.is_absolute():
                data_path = self.root / "backend" / data_path
            if data_path.exists():
                with tarfile.open(self.backup / "data.tar.gz", "w:gz") as archive:
                    archive.add(data_path, arcname="data")
                (self.backup / "data.tar.gz").chmod(0o600)
            self.copy_source()
            self.write(self.root / "backend/.env", "".join(key + "=" + shlex.quote(value) + "\n" for key, value in env.items()), 0o600)
            self.write(self.root / "worker/.secrets.json", json.dumps(worker, indent=2) + "\n", 0o600)
            self.write(existing_config, json.dumps(config, indent=2) + "\n", 0o600)
            if self.run(["id", "-u", "tgdrive"], check=False, capture=True).returncode != 0:
                self.run(["useradd", "--system", "--home", self.root, "--shell", "/usr/sbin/nologin", "tgdrive"])
            self.run(["chown", "-R", "tgdrive:tgdrive", self.root / "backend"])
            data_path.mkdir(parents=True, exist_ok=True)
            self.run(["chown", "-R", "tgdrive:tgdrive", data_path])
            self.write(self.unit, service_config(self.root, self.settings["port"], python))
            self.write(self.https, https_config(self.settings["domain"], self.certificates, self.settings["port"]))
            self.write(self.system / "etc/letsencrypt/renewal-hooks/deploy/tgdrive-nginx-reload", "#!/bin/sh\nnginx -t && systemctl reload nginx\n", 0o755)
            self.run(["nginx", "-t"])
            self.run(["systemctl", "reload", "nginx"])
            self.run(["systemctl", "daemon-reload"])
            self.run(["systemctl", "enable", "tgdrive.service"])
            self.run(["systemctl", "restart", "tgdrive.service"])
            for attempt in range(30):
                try:
                    if not json.loads(self.fetch("http://127.0.0.1:" + str(self.settings["port"]) + "/health"))["ok"]:
                        raise RuntimeError("后端尚未就绪")
                    break
                except Exception:
                    if attempt == 29:
                        raise RuntimeError("后端健康检查失败，请检查 journalctl -u tgdrive.service") from None
                    time.sleep(1)
            if not json.loads(self.fetch(env["PUBLIC_BASE"] + "/health"))["ok"]:
                raise RuntimeError("公网HTTPS健康检查失败")
            self.run(["systemctl", "enable", "--now", "certbot.timer"])
            self.write(self.root / ".install.local.json", json.dumps({**installation, "state": "ready", "python": str(python)}) + "\n", 0o600)
        except Exception:
            # Restore pre-existing files and remove only newly created system configs.
            self.run(["systemctl", "stop", "tgdrive.service"], check=False)
            for path, (contents, mode, uid, gid) in reversed(list(self.originals.items())):
                if contents is not None:
                    path.write_bytes(contents)
                    path.chmod(mode)
                    os.chown(path, uid, gid)
                elif str(path).startswith(str(self.system / "etc") + "/") and path.exists():
                    path.unlink()
            self.run(["systemctl", "daemon-reload"], check=False)
            self.run(["systemctl", "reload", "nginx"], check=False)
            if self.unit.exists():
                self.run(["systemctl", "start", "tgdrive.service"], check=False)
            print("安装未完成，原有配置已恢复；备份目录：" + str(self.backup))
            raise
        print("\n后端部署成功：" + env["PUBLIC_BASE"])
        print("HTTPS自动续期已启用。")
        print("Worker私有配置：" + str(existing_config))
        print("Worker密钥文件：" + str(self.root / "worker/.secrets.json"))
        print("网页管理密码：" + worker["ADMIN_PASSWORD"])
        print("下一步：在 worker 目录安装Node.js依赖并运行 npm run deploy 部署网页。")


def main():
    parser = argparse.ArgumentParser(description="TGDrive交互式后端部署")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dry-run", action="store_true", help="只显示计划，不修改系统")
    args = parser.parse_args()
    try:
        settings = collect_settings()
        if args.dry_run:
            print(json.dumps({key: str(settings[key]) for key in ["root", "domain", "frontend", "port"]}, ensure_ascii=False, indent=2))
            print("计划：安装Python依赖 → HTTP域名验证 → HTTPS证书 → Nginx → systemd → 健康检查。")
            return
        if os.geteuid() != 0:
            raise RuntimeError("正式安装需要root权限")
        Installer(args.source.resolve(), settings).install()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit("部署失败：" + str(error)) from None


if __name__ == "__main__":
    main()
