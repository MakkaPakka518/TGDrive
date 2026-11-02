#!/usr/bin/env python3
"""Check staged/tracked files for private artifacts before committing."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
blocked_names = {".env", ".secrets.json", ".admin-password", ".dev.vars", "credentials.env"}
blocked_parts = {"data", "snapshots", "backups", ".wrangler", ".venv", ".venvs", "node_modules", "__pycache__"}
patterns = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
]
failures = []
for name in filter(None, paths):
    path = Path(name)
    if path.name in blocked_names or any(part in blocked_parts for part in path.parts) or name.endswith((".session", ".session.enc", ".db", ".sqlite", ".pem", ".key", ".local.jsonc", ".local.json")):
        failures.append(f"Private artifact: {name}")
        continue
    contents = subprocess.check_output(["git", "show", ":" + name], cwd=ROOT)
    if any(pattern.search(contents) for pattern in patterns):
        failures.append(f"Credential-like content: {name}")
if failures:
    raise SystemExit("\n".join(failures))
print(f"Public-file checks passed for {len(list(filter(None, paths)))} staged/tracked files.")
