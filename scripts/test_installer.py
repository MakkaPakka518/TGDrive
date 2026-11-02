import contextlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("installer", Path(__file__).with_name("install_backend.py"))
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.base.chmod(0o755)
        self.source = self.base / "source"
        (self.source / "backend").mkdir(parents=True)
        (self.source / "worker").mkdir()
        (self.source / "backend/app.py").write_text("# test application\n")
        (self.source / "backend/storage.py").write_text("# test storage\n")
        (self.source / "backend/requirements.txt").write_text("")
        (self.source / "backend/.env").write_text("DO_NOT_COPY_SOURCE_SECRETS=true\n")
        (self.source / "worker/wrangler.jsonc").write_text(json.dumps({"name": "tgdrive", "main": "src/index.js", "vars": {}}))
        self.settings = {
            "root": self.base / "install", "domain": "api.example.test", "frontend": "https://drive.example.test", "port": 3219,
            "api_id": "123", "api_hash": "a" * 32, "token": "t" * 32, "password": "test-admin-password", "email": "",
            "existing_env": {}, "worker_secrets": {},
        }
        self.commands = []
        self.system = self.base / "system"

    def runner(self, command, check=True, capture=False):
        command = list(map(str, command))
        self.commands.append(command)
        if command[:3] == ["python3", "-m", "venv"]:
            (Path(command[3]) / "bin").mkdir(parents=True, exist_ok=True)
            (Path(command[3]) / "bin/python").write_text("# fake interpreter\n")
        if command[0] == "certbot":
            cert = self.system / "etc/letsencrypt/live" / self.settings["domain"]
            cert.mkdir(parents=True, exist_ok=True)
            for name in ("fullchain.pem", "privkey.pem"):
                (cert / name).write_text("test fixture only")
        code = 1 if command[:3] in [["id", "-u", "tgdrive"], ["systemctl", "is-active", "--quiet"]] else 0
        return subprocess.CompletedProcess(command, code, stdout="", stderr="")

    def fetch(self, url):
        if "/.well-known/acme-challenge/" in url:
            name = url.rsplit("/", 1)[1]
            return (self.system / "var/lib/tgdrive-acme/.well-known/acme-challenge" / name).read_bytes()
        return b'{"ok":true}'

    def instance(self, fetcher=None):
        return installer.Installer(self.source, self.settings, self.system, self.runner, fetcher or self.fetch)

    def test_domain_validation_and_nginx_generation(self):
        self.assertEqual(installer.domain_name("https://api.example.com/"), "api.example.com")
        for value in ["api.example.com; include /etc/passwd", "api.example.com\nserver", "https://api.example.com/path", "127.0.0.1"]:
            with self.assertRaises(ValueError):
                installer.domain_name(value)
        with self.assertRaises(ValueError):
            installer.frontend_origin("http://drive.example.com")
        config = installer.https_config("api.example.com", Path("/certs/example"), 3333)
        self.assertIn("proxy_pass http://127.0.0.1:3333/", config)
        self.assertIn("proxy_buffering off", config)

    def test_interactive_prompts_and_hidden_credentials(self):
        answers = [str(self.settings["root"]), "api.example.test", "https://drive.example.test", "3219", "123", ""]
        output = io.StringIO()
        with patch("builtins.input", side_effect=answers), patch.object(installer.getpass, "getpass", side_effect=["a" * 32, "t" * 32, "test-admin-password"]), patch.object(installer.socket, "getaddrinfo", return_value=[object()]), contextlib.redirect_stdout(output):
            settings = installer.collect_settings()
        self.assertEqual(settings["domain"], "api.example.test")
        self.assertEqual(settings["port"], 3219)
        self.assertNotIn("a" * 32, output.getvalue())
        self.assertFalse(settings["root"].exists())

    def test_fresh_install_runs_complete_plan_and_private_configuration(self):
        deploy = self.instance()
        with patch.object(installer.socket, "socket"), contextlib.redirect_stdout(io.StringIO()):
            deploy.install()
        env = installer.read_env(self.settings["root"] / "backend/.env")
        self.assertEqual(env["PUBLIC_BASE"], "https://api.example.test/tgdrive-api")
        self.assertNotIn("DO_NOT_COPY_SOURCE_SECRETS", env)
        secrets_file = self.settings["root"] / "worker/.secrets.json"
        self.assertEqual(json.loads(secrets_file.read_text())["BACKEND_TOKEN"], env["SERVICE_TOKEN"])
        self.assertEqual(secrets_file.stat().st_mode & 0o777, 0o600)
        self.assertIn(".venvs/", deploy.unit.read_text())
        self.assertIn("server_name api.example.test", deploy.https.read_text())
        self.assertTrue(any(command[0] == "certbot" and "--webroot" in command for command in self.commands))
        self.assertIn(["systemctl", "enable", "--now", "certbot.timer"], self.commands)
        self.assertEqual(list((deploy.webroot / ".well-known/acme-challenge").iterdir()), [])

    def test_upgrade_preserves_keys_data_and_other_worker_bindings(self):
        root = self.settings["root"]
        (root / "backend/data").mkdir(parents=True)
        (root / "backend/data/tgdrive.db").write_bytes(b"existing private database fixture")
        (root / "backend/.venv/bin").mkdir(parents=True)
        (root / "backend/.venv/bin/python").write_text("old interpreter")
        (root / "worker").mkdir()
        self.settings["existing_env"] = {"MEDIA_SECRET": "old-media", "SESSION_ENCRYPTION_KEY": "existing-encryption-key", "DATA_DIR": "./data"}
        self.settings["worker_secrets"] = {"SESSION_SECRET": "existing-cookie-key"}
        (root / "worker/wrangler.local.jsonc").write_text(json.dumps({"name": "custom-name", "vars": {"KEEP": "value"}, "routes": [{"pattern": "extra.example.test", "custom_domain": True}]}))
        deploy = self.instance()
        with patch.object(installer.socket, "socket"), contextlib.redirect_stdout(io.StringIO()):
            deploy.install()
        env = installer.read_env(root / "backend/.env")
        self.assertEqual(env["SESSION_ENCRYPTION_KEY"], "existing-encryption-key")
        self.assertEqual((root / "backend/data/tgdrive.db").read_bytes(), b"existing private database fixture")
        self.assertEqual((root / "backend/.venv/bin/python").read_text(), "old interpreter")
        self.assertTrue((deploy.backup / "data.tar.gz").exists())
        config = json.loads((root / "worker/wrangler.local.jsonc").read_text())
        self.assertEqual(config["vars"]["KEEP"], "value")
        self.assertEqual(len(config["routes"]), 2)

    def test_failed_http_validation_restores_existing_nginx(self):
        deploy = self.instance(fetcher=lambda url: b"wrong host")
        deploy.http.parent.mkdir(parents=True)
        original = installer.MARKER + "\n# prior configuration\n"
        deploy.http.write_text(original)
        with patch.object(installer.socket, "socket"), patch.object(installer.os, "chown"), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            deploy.install()
        self.assertEqual(deploy.http.read_text(), original)
        self.assertFalse(any(command[0] == "certbot" for command in self.commands))
        self.assertEqual(installer.installation_path(str(self.settings["root"])), self.settings["root"])

    def test_domain_conflict_stops_before_writing(self):
        deploy = self.instance()
        previous_runner = deploy.run

        def runner(command, **options):
            result = previous_runner(command, **options)
            if command == ["nginx", "-T"]:
                result.stdout = "# configuration file /etc/nginx/conf.d/other.conf:\nserver { server_name api.example.test; }\n"
            return result

        deploy.run = runner
        with patch.object(installer.socket, "socket"), self.assertRaises(RuntimeError):
            deploy.install()
        self.assertFalse(self.settings["root"].exists())


if __name__ == "__main__":
    unittest.main()
