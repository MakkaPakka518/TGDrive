#!/usr/bin/env python3
"""Create private configuration for a fresh installation. Never overwrites keys."""
import getpass
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]


def https_url(value, origin_only=False):
    if any(character.isspace() for character in value):
        raise ValueError("URL cannot contain whitespace")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Enter a complete HTTPS URL without credentials, query or fragment")
    if origin_only and parsed.path not in {"", "/"}:
        raise ValueError("Frontend URL must be an origin without a path")
    return value.rstrip("/")


def exclusive_write(path, contents):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as output:
        output.write(contents)


def main():
    env_path = ROOT / "backend" / ".env"
    secrets_path = ROOT / "worker" / ".secrets.json"
    config_path = ROOT / "worker" / "wrangler.local.jsonc"
    if any(path.exists() for path in (env_path, secrets_path, config_path)):
        raise SystemExit("Local configuration already exists. Edit it in place to preserve your keys and sessions.")
    api_id = input("Telegram API ID: ").strip()
    api_hash = getpass.getpass("Telegram API hash (hidden): ").strip()
    if not api_id.isdigit() or int(api_id) <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise SystemExit("Invalid Telegram API ID/hash")
    try:
        public_base = https_url(input("Backend public base URL, including /tgdrive-api: ").strip())
        frontend = https_url(input("Frontend URL (custom domain or workers.dev): ").strip(), origin_only=True)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    service_token = secrets.token_hex(32)
    worker_secrets = {
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "SESSION_SECRET": secrets.token_hex(32),
        "BACKEND_TOKEN": service_token,
    }
    backend_env = {
        "TG_API_ID": api_id,
        "TG_API_HASH": api_hash,
        "SERVICE_TOKEN": service_token,
        "MEDIA_SECRET": secrets.token_hex(32),
        "SESSION_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "PUBLIC_BASE": public_base,
        "FRONTEND_ORIGIN": frontend,
        "DATA_DIR": "./data",
    }
    config = json.loads((ROOT / "worker" / "wrangler.jsonc").read_text())
    config["vars"]["BACKEND_URL"] = public_base
    hostname = urlsplit(frontend).hostname
    if hostname.endswith(".workers.dev"):
        # A workers.dev hostname is <worker-name>.<account-subdomain>.workers.dev.
        if len(hostname.split(".")) != 4:
            raise SystemExit("Use a worker hostname: <worker-name>.<account-subdomain>.workers.dev")
        config["name"] = hostname.split(".")[0]
    else:
        config["workers_dev"] = False
        config["routes"] = [{"pattern": hostname, "custom_domain": True}]
    exclusive_write(env_path, "".join(f"{key}={value}\n" for key, value in backend_env.items()))
    exclusive_write(secrets_path, json.dumps(worker_secrets, indent=2) + "\n")
    exclusive_write(config_path, json.dumps(config, indent=2) + "\n")
    print("Created backend/.env, worker/.secrets.json and worker/wrangler.local.jsonc with mode 0600.")
    print("Admin password: " + worker_secrets["ADMIN_PASSWORD"])


if __name__ == "__main__":
    main()
