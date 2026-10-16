"""Gallery API used by the redesigned admin page: page sizes, image info, favicon."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
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
spec = importlib.util.spec_from_file_location("gallery_server_test", source)
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class GalleryApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in [("OUTPUT_DIR", Path(self.tmp.name)), ("TRANSPORT", "http"),
                            ("PUBLIC_BASE_URL", "https://testserver"), ("SECRET", "connector-secret-hidden")]:
            p = patch.object(s, name, value); p.start(); self.addCleanup(p.stop)
        for name, value in [("IMAGE_ADMIN_PASSWORD", "test-admin-password-12345")]:
            p = patch.dict(os.environ, {name: value}); p.start(); self.addCleanup(p.stop)
        p = patch.object(s.shutil, "disk_usage", return_value=SimpleNamespace(free=10**12))
        p.start(); self.addCleanup(p.stop)
        now = time.time()
        for i in range(30):
            path = s.OUTPUT_DIR / f"img-{i:02d}.png"
            Image.new("RGB", (64, 96), "red").save(path)
            os.utime(path, (now - i, now - i))
        self.client = TestClient(s._http_app(), base_url="https://testserver")
        self.addCleanup(self.client.close)
        r = self.client.post("/admin/api/login", json={"password": "test-admin-password-12345"},
                             headers={"Origin": "https://testserver"})
        self.assertEqual(r.status_code, 200)

    def state(self, **params):
        r = self.client.get("/admin/api/state", params=params)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_default_page_size_is_24(self):
        data = self.state()
        self.assertEqual((data["per_page"], data["pages"], len(data["items"])), (24, 2, 24))

    def test_allowed_page_sizes(self):
        for size, pages in [(12, 3), (48, 1), (96, 1)]:
            data = self.state(per_page=size)
            self.assertEqual((data["per_page"], data["pages"]), (size, pages))
        self.assertEqual(len(self.state(per_page=12, page=3)["items"]), 6)

    def test_invalid_page_size_falls_back(self):
        for bad in ["7", "1000", "abc", "-12"]:
            self.assertEqual(self.state(per_page=bad)["per_page"], 24)

    def test_page_is_clamped(self):
        self.assertEqual(self.state(per_page=12, page=99)["page"], 3)

    def test_items_carry_dimensions_and_format(self):
        item = self.state()["items"][0]
        self.assertEqual((item["width"], item["height"], item["format"]), (64, 96, "PNG"))
        self.assertFalse(item["alpha"])

    def test_icon_asset_served(self):
        r = self.client.get("/admin/assets/icon.svg")
        self.assertEqual(r.status_code, 200)
        self.assertIn("svg", r.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
