"""Regression tests for: non-blocking generation, gallery during generation,
transparent thumbnails, and login rate-limit table eviction."""
import asyncio
import base64
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
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
spec = importlib.util.spec_from_file_location("fixes_server_test", source)
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


def png_b64():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in [("OUTPUT_DIR", Path(self.tmp.name)), ("TRANSPORT", "http"),
                            ("PUBLIC_BASE_URL", "https://testserver"), ("SECRET", "connector-secret-hidden")]:
            p = patch.object(s, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"IMAGE_ADMIN_PASSWORD": "test-admin-password-12345"})
        p.start(); self.addCleanup(p.stop)
        p = patch.object(s.shutil, "disk_usage", return_value=SimpleNamespace(free=10**12))
        p.start(); self.addCleanup(p.stop)


class GenerationDoesNotBlock(Base):
    def test_mcp_tools_are_async(self):
        tools = {t.name: t for t in s.mcp._tool_manager.list_tools()}
        for name in ["generate_image", "edit_image"]:
            self.assertTrue(tools[name].is_async, name)
            self.assertIn("prompt", tools[name].parameters["properties"])

    def test_event_loop_keeps_running_during_generation(self):
        def slow_generate(**kwargs):
            time.sleep(0.6)
            return SimpleNamespace(data=[SimpleNamespace(b64_json=png_b64(), revised_prompt=None)])
        fake = SimpleNamespace(images=SimpleNamespace(generate=slow_generate))

        async def scenario():
            ticks = 0
            async def ticker():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.05)
                    ticks += 1
            t = asyncio.create_task(ticker())
            result = await s.mcp.call_tool("generate_image", {"prompt": "x", "preview": False})
            t.cancel()
            return ticks, result

        with patch.object(s, "_client", return_value=fake):
            ticks, result = asyncio.run(scenario())
        self.assertGreaterEqual(ticks, 5, "event loop was blocked during generation")
        self.assertIn("Generated 1 image", str(result))


class AdminDuringGeneration(Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(s._http_app(), base_url="https://testserver")
        self.addCleanup(self.client.close)
        self.headers = {"Origin": "https://testserver"}

    def login(self, ip=None):
        r = self.client.post("/admin/api/login", json={"password": "test-admin-password-12345"},
                             headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.headers["X-CSRF-Token"] = r.json()["csrf"]

    def test_gallery_readable_while_generating(self):
        Image.new("RGB", (20, 20), "green").save(s.OUTPUT_DIR / "first.png")
        self.login()
        ready, release = threading.Event(), threading.Event()
        def hold():
            with s._STORAGE_LOCK:
                ready.set(); release.wait(5)
        t = threading.Thread(target=hold); t.start()
        try:
            self.assertTrue(ready.wait(2))
            r = self.client.get("/admin/api/state")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["busy"])
            self.assertEqual(r.json()["count"], 1)
            self.assertTrue(r.json()["items"][0]["url"].startswith("https://testserver/files/"))
        finally:
            release.set(); t.join()
        self.assertFalse(self.client.get("/admin/api/state").json()["busy"])

    def test_transparent_thumbnail_keeps_alpha(self):
        Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(s.OUTPUT_DIR / "icon.png")
        Image.new("RGB", (40, 40), "blue").save(s.OUTPUT_DIR / "photo.png")
        self.login()
        r = self.client.get("/admin/api/thumbnail/icon.png")
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertEqual(Image.open(io.BytesIO(r.content)).getpixel((5, 5))[3], 0)
        self.assertEqual(self.client.get("/admin/api/thumbnail/photo.png").headers["content-type"], "image/jpeg")

    def test_deleted_image_during_link_creation_is_skipped(self):
        self._deleted_during_listing(after_link=False)

    def test_deleted_image_before_header_read_is_skipped(self):
        self._deleted_during_listing(after_link=True)

    def _deleted_during_listing(self, after_link):
        for name in ["gone.png", "keep.png"]:
            Image.new("RGB", (20, 20), "green").save(s.OUTPUT_DIR / name)
        self.login()
        route = next(r for r in self.client.app.router.routes if getattr(r, "path", "") == "/admin/api/state")
        admin = route.endpoint.__self__
        original = admin.link

        def delete_between_reads(path):
            if path.name == "gone.png":
                if after_link:
                    url = original(path)
                    path.unlink()
                    return url
                path.unlink()
            return original(path)

        with patch.object(admin, "link", side_effect=delete_between_reads):
            response = self.client.get("/admin/api/state")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([i["name"] for i in response.json()["items"]], ["keep.png"])
        refreshed = self.client.get("/admin/api/state").json()
        self.assertEqual(refreshed["count"], 1)
        self.assertEqual(refreshed["total"], 1)

    def test_full_attempt_table_does_not_lock_everyone_out(self):
        admin_route = next(r for r in self.client.app.router.routes if getattr(r, "path", "") == "/admin/api/login")
        admin = admin_route.endpoint.__self__
        far = time.time() + 900
        admin.attempts = {f"10.9.{i // 250}.{i % 250}": (1, far) for i in range(1000)}
        self.login()
        self.assertLessEqual(len(admin.attempts), 1000)


if __name__ == "__main__":
    unittest.main()
