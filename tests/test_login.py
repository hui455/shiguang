"""Remember-me sessions: cookie lifetime, survive restarts, hashed on disk, password change."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from starlette.testclient import TestClient

os.environ["MCP_TRANSPORT"] = "stdio"
source = Path("/app/server.py")
if not source.exists():
    source = Path(__file__).resolve().parents[1] / "plugins/openai-image-gen/server.py"
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("login_server_test", source)
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

PASSWORD = "test-admin-password-12345"
ORIGIN = {"Origin": "https://testserver"}


class RememberMe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in [("OUTPUT_DIR", Path(self.tmp.name)), ("TRANSPORT", "http"),
                            ("PUBLIC_BASE_URL", "https://testserver"), ("SECRET", "connector-secret-hidden")]:
            p = patch.object(s, name, value); p.start(); self.addCleanup(p.stop)
        self.env = patch.dict(os.environ, {"IMAGE_ADMIN_PASSWORD": PASSWORD})
        self.env.start(); self.addCleanup(self.env.stop)
        p = patch.object(s.shutil, "disk_usage", return_value=SimpleNamespace(free=10**12))
        p.start(); self.addCleanup(p.stop)
        Image.new("RGB", (20, 20), "green").save(s.OUTPUT_DIR / "a.png")

    def client(self):
        c = TestClient(s._http_app(), base_url="https://testserver")
        self.addCleanup(c.close)
        return c

    def login(self, c, remember):
        r = c.post("/admin/api/login", json={"password": PASSWORD, "remember": remember}, headers=ORIGIN)
        self.assertEqual(r.status_code, 200)
        return r

    def test_cookie_lifetime_follows_remember(self):
        self.assertIn("Max-Age=2592000", self.login(self.client(), True).headers["set-cookie"])
        self.assertNotIn("Max-Age", self.login(self.client(), False).headers["set-cookie"])

    def test_session_survives_restart(self):
        first = self.client()
        token = self.login(first, True).cookies["image_admin"]
        restarted = self.client()  # new app instance reads the session file
        self.assertEqual(restarted.get("/admin/api/state", headers={"Cookie": f"image_admin={token}"}).status_code, 200)
        self.assertEqual(restarted.get("/admin/api/state", headers={"Cookie": "image_admin=forged"}).status_code, 401)

    def test_only_token_hashes_are_stored(self):
        token = self.login(self.client(), True).cookies["image_admin"]
        raw = (s.OUTPUT_DIR / ".admin-sessions.json").read_text()
        self.assertNotIn(token, raw)
        self.assertNotIn(PASSWORD, raw)

    def test_password_change_logs_everyone_out(self):
        token = self.login(self.client(), True).cookies["image_admin"]
        with patch.dict(os.environ, {"IMAGE_ADMIN_PASSWORD": "another-password-67890"}):
            c = self.client()
            self.assertEqual(c.get("/admin/api/state", headers={"Cookie": f"image_admin={token}"}).status_code, 401)
        # Control: with the original password the same cookie is still accepted, so the 401
        # above really comes from the password change.
        c = self.client()
        self.assertEqual(c.get("/admin/api/state", headers={"Cookie": f"image_admin={token}"}).status_code, 200)

    def test_logout_removes_session_from_disk(self):
        c = self.client()
        csrf = self.login(c, True).json()["csrf"]
        self.assertEqual(len(json.loads((s.OUTPUT_DIR / ".admin-sessions.json").read_text())["sessions"]), 1)
        r = c.post("/admin/api/logout", json={}, headers={**ORIGIN, "X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads((s.OUTPUT_DIR / ".admin-sessions.json").read_text())["sessions"], {})

    def test_full_session_table_evicts_oldest_instead_of_refusing(self):
        c = self.client()
        admin = next(r for r in c.app.router.routes if getattr(r, "path", "") == "/admin/api/login").endpoint.__self__
        admin.sessions = {f"k{i}": (s.time.time() + 1000 + i, "x") for i in range(128)}
        self.login(c, False)
        self.assertEqual(len(admin.sessions), 128)
        self.assertNotIn("k0", admin.sessions)

    def test_session_file_not_listed_or_downloadable(self):
        self.login(self.client(), True)
        c = self.client()
        self.login(c, False)
        names = [i["name"] for i in c.get("/admin/api/state").json()["items"]]
        self.assertEqual(names, ["a.png"])
        self.assertEqual(c.get("/admin/api/image/.admin-sessions.json").status_code, 404)

    def test_login_page_has_password_manager_fields(self):
        html = self.client().get("/admin").text
        for needle in ['autocomplete="username"', 'name="password"', 'autocomplete="current-password"', 'id="remember"']:
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
