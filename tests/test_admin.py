import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from starlette.testclient import TestClient
from PIL import Image

os.environ["MCP_TRANSPORT"] = "stdio"
source = Path("/app/server.py")
if not source.exists():
    source = Path(__file__).resolve().parents[1] / "plugins/openai-image-gen/server.py"
import sys
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("admin_server_test", source)
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in [("OUTPUT_DIR", Path(self.tmp.name)), ("TRANSPORT", "http"),
                            ("PUBLIC_BASE_URL", "https://testserver"), ("SECRET", "connector-secret-hidden")]:
            p = patch.object(s, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"IMAGE_ADMIN_PASSWORD": "test-admin-password-12345"})
        p.start(); self.addCleanup(p.stop)
        self.first = s.OUTPUT_DIR / "first.png"
        Image.new("RGB", (20, 20), "green").save(self.first)
        self.client = TestClient(s._http_app(), base_url="https://testserver")
        self.addCleanup(self.client.close)
        self.headers = {"Origin": "https://testserver"}

    def login(self):
        r = self.client.post("/admin/api/login", json={"password": "test-admin-password-12345"}, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.headers["X-CSRF-Token"] = r.json()["csrf"]
        return r

    def test_requires_login_and_separate_password(self):
        for path in ["state", "thumbnail/first.png", "image/first.png"]:
            self.assertEqual(self.client.get("/admin/api/" + path).status_code, 401)
        self.assertEqual(self.client.post("/admin/api/delete", json={"names": ["first.png"]}).status_code, 401)
        for password in [s.SECRET, s._link(self.first).split("/")[-2]]:
            self.assertEqual(self.client.post("/admin/api/login", json={"password": password}, headers=self.headers).status_code, 401)

    def test_login_state_cookie_preview_logout(self):
        r = self.login()
        for flag in ["HttpOnly", "Secure", "SameSite=strict"]:
            self.assertIn(flag, r.headers["set-cookie"])
        state = self.client.get("/admin/api/state")
        self.assertEqual(state.json()["count"], 1)
        self.assertNotIn(s.SECRET, state.text)
        self.assertEqual(state.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/admin/api/thumbnail/first.png").headers["content-type"], "image/jpeg")
        self.assertEqual(self.client.get("/admin/api/image/first.png").status_code, 200)
        self.assertEqual(self.client.post("/admin/api/logout", json={}, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/admin/api/state").status_code, 401)

    def test_csrf_and_origin_block_deletion(self):
        self.login()
        body = {"names": ["first.png"]}
        self.assertEqual(self.client.post("/admin/api/delete", json=body).status_code, 403)
        headers = dict(self.headers, Origin="https://attacker.example")
        self.assertEqual(self.client.post("/admin/api/delete", json=body, headers=headers).status_code, 400)
        self.assertTrue(self.first.exists())

    def test_delete_only_selected_image_and_revoke_link(self):
        self.login()
        second = s.OUTPUT_DIR / "second.png"
        Image.new("RGB", (20, 20)).save(second)
        link = s._link(self.first)
        r = self.client.post("/admin/api/delete", json={"names": ["first.png"]}, headers=self.headers)
        self.assertEqual(r.json()["deleted"], ["first.png"])
        self.assertFalse(self.first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(self.client.get(link).status_code, 404)

    def test_invalid_batch_is_not_partially_deleted(self):
        self.login()
        for names in [["first.png", "../private"], ["first.png", "missing.png"], [], ["first.png", "first.png"]]:
            self.assertEqual(self.client.post("/admin/api/delete", json={"names": names}, headers=self.headers).status_code, 400)
            self.assertTrue(self.first.exists())

    def test_login_throttling(self):
        for _ in range(5):
            self.assertEqual(self.client.post("/admin/api/login", json={"password": "wrong"}, headers=self.headers).status_code, 401)
        self.assertEqual(self.client.post("/admin/api/login", json={"password": "wrong"}, headers=self.headers).status_code, 429)

    def test_search_and_busy_lock(self):
        self.login()
        self.assertEqual(self.client.get("/admin/api/state?q=missing").json()["total"], 0)
        ready, release = threading.Event(), threading.Event()
        def hold():
            with s._STORAGE_LOCK:
                ready.set(); release.wait(5)
        t = threading.Thread(target=hold); t.start()
        try:
            self.assertTrue(ready.wait(2))
            self.assertEqual(self.client.post("/admin/api/delete", json={"names": ["first.png"]}, headers=self.headers).status_code, 409)
        finally:
            release.set(); t.join()
        self.assertTrue(self.first.exists())

    def test_admin_assets_and_traversal(self):
        self.assertEqual(self.client.get("/admin").status_code, 200)
        for asset in ["app.js", "style.css"]:
            self.assertEqual(self.client.get("/admin/assets/" + asset).status_code, 200)
        self.assertEqual(self.client.get("/admin/assets/server.py").status_code, 404)


if __name__ == "__main__": unittest.main()
