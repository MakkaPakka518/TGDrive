import contextlib
import importlib.util
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("configure", Path(__file__).with_name("configure.py"))
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class ConfigureTests(unittest.TestCase):
    def test_https_validation(self):
        for value in ["http://api.example.test", "https://user:password@api.example.test", "https://api.example.test?key=value", "https://api.example.test\nINJECTED=value"]:
            with self.assertRaises(ValueError):
                configure.https_url(value)
        with self.assertRaises(ValueError):
            configure.https_url("https://drive.example.test/path", origin_only=True)

    def test_private_files_and_preserved_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "backend").mkdir()
            (root / "worker").mkdir()
            (root / "worker" / "wrangler.jsonc").write_text(json.dumps({"name": "tgdrive", "vars": {}}))
            with patch.object(configure, "ROOT", root), patch("builtins.input", side_effect=["123", "https://api.example.test/tgdrive-api", "https://drive.example.test"]), patch.object(configure.getpass, "getpass", return_value="ab" * 16), contextlib.redirect_stdout(io.StringIO()):
                configure.main()
                env_file = root / "backend" / ".env"
                values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
                worker_secrets = json.loads((root / "worker" / ".secrets.json").read_text())
                config = json.loads((root / "worker" / "wrangler.local.jsonc").read_text())
                self.assertEqual(values["SERVICE_TOKEN"], worker_secrets["BACKEND_TOKEN"])
                self.assertEqual(values["PUBLIC_BASE"], config["vars"]["BACKEND_URL"])
                self.assertEqual(config["routes"][0]["pattern"], "drive.example.test")
                self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
                with self.assertRaises(SystemExit):
                    configure.main()
                self.assertIn(values["SESSION_ENCRYPTION_KEY"], env_file.read_text())


if __name__ == "__main__":
    unittest.main()
