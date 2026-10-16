import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from starlette.testclient import TestClient

os.environ["MCP_TRANSPORT"] = "stdio"
source = Path("/app/server.py")
if not source.exists():
    source = Path(__file__).resolve().parents[1] / "plugins/openai-image-gen/server.py"
import sys
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("image_server_downloads", source)
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in [("OUTPUT_DIR", Path(self.temp.name)), ("TRANSPORT", "http"),
                            ("SECRET", "connector-secret-must-never-appear"),
                            ("PUBLIC_BASE_URL", "https://images.example.com")]:
            p = patch.object(s, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.first = s.OUTPUT_DIR / "first.png"
        self.second = s.OUTPUT_DIR / "second.png"
        self.first.write_bytes(b"first image")
        self.second.write_bytes(b"second image")
        self.client = TestClient(s._http_app())
        self.addCleanup(self.client.close)

    def test_independent_persistent_tokens_and_download(self):
        first = s._link(self.first)
        second = s._link(self.second)
        self.assertNotIn(s.SECRET, first)
        self.assertNotEqual(first.split("/")[-2], second.split("/")[-2])
        self.assertEqual(first, s._link(self.first))
        response = self.client.get(urlsplit(first).path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"first image")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(self.client.head(urlsplit(first).path).status_code, 200)

    def test_wrong_token_cannot_access_another_image(self):
        first = s._link(self.first)
        s._link(self.second)
        wrong = urlsplit(first).path.replace("first.png", "second.png")
        self.assertEqual(self.client.get(wrong).status_code, 404)
        self.assertEqual(self.client.get("/files/" + "x" * 43 + "/first.png").status_code, 404)

    def test_token_cannot_access_mcp_and_legacy_route_disabled(self):
        token = s._link(self.first).split("/")[-2]
        self.assertEqual(self.client.post(f"/{token}/mcp").status_code, 404)
        self.assertEqual(self.client.get(f"/{s.SECRET}/files/first.png").status_code, 404)

    def test_private_files_and_traversal_blocked(self):
        for name in [".env", "private.txt"]:
            p = s.OUTPUT_DIR / name
            p.write_text("private")
            with self.assertRaises(FileNotFoundError):
                s._link(p)
        token = s._link(self.first).split("/")[-2]
        for name in [".env", "private.txt", "..%2Ffirst.png", "..%5Cfirst.png"]:
            self.assertEqual(self.client.get(f"/files/{token}/{name}").status_code, 404)
        self.assertNotIn("private.txt", s.list_images())
        self.assertNotIn(".download-tokens", s.list_images())

    def test_existing_images_get_new_links_and_missing_files_404(self):
        links = s.list_images()
        self.assertIn("/files/", links)
        self.assertNotIn(s.SECRET, links)
        link = s._link(self.first)
        self.first.unlink()
        self.assertEqual(self.client.get(urlsplit(link).path).status_code, 404)

    def test_symlinks_rejected(self):
        link = s.OUTPUT_DIR / "alias.png"
        try:
            link.symlink_to(self.first)
        except OSError:
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(FileNotFoundError):
            s._link(link)


if __name__ == "__main__":
    unittest.main()
