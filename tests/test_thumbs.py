"""Thumbnail links: token-gated, resized, alpha kept, listed in tool results."""
import base64
import importlib.util
import io
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
spec = importlib.util.spec_from_file_location("thumbs_server_test", source)
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class ThumbnailLinks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in [("OUTPUT_DIR", Path(self.tmp.name)), ("TRANSPORT", "http"),
                            ("PUBLIC_BASE_URL", "https://testserver"), ("SECRET", "connector-secret-hidden"),
                            ("PREVIEW_MAX_PX", 256)]:
            p = patch.object(s, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(s.shutil, "disk_usage", return_value=SimpleNamespace(free=10**12))
        p.start(); self.addCleanup(p.stop)
        self.photo = s.OUTPUT_DIR / "card.png"
        Image.new("RGB", (1024, 1536), "red").save(self.photo)
        self.icon = s.OUTPUT_DIR / "icon.png"
        Image.new("RGBA", (800, 800), (0, 0, 0, 0)).save(self.icon)
        self.client = TestClient(s._http_app(), base_url="https://testserver")
        self.addCleanup(self.client.close)

    def get(self, url):
        return self.client.get(url.replace("https://testserver", ""))

    def test_thumbnail_is_small_jpeg(self):
        r = self.get(s._thumb_link(self.photo))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/jpeg")
        self.assertEqual(Image.open(io.BytesIO(r.content)).size, (171, 256))

    def test_transparent_thumbnail_stays_png_with_alpha(self):
        r = self.get(s._thumb_link(self.icon))
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertEqual(Image.open(io.BytesIO(r.content)).getpixel((5, 5))[3], 0)

    def test_thumbnail_and_download_share_the_token(self):
        thumb, full = s._thumb_link(self.photo), s._link(self.photo)
        self.assertEqual(thumb.split("/")[-2], full.split("/")[-2])
        self.assertEqual(self.get(full).status_code, 200)

    def test_wrong_token_or_name_is_404(self):
        token = s._thumb_link(self.photo).split("/")[-2]
        for url in [f"/thumbs/{'A' * 43}/card.png", f"/thumbs/{token}/icon.png",
                    f"/thumbs/{token}/.download-tokens", "/thumbs/short/card.png"]:
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_tool_result_lists_thumbnails_and_ps_format(self):
        buf = io.BytesIO(); Image.new("RGB", (64, 64), "blue").save(buf, "PNG")
        item = SimpleNamespace(b64_json=base64.b64encode(buf.getvalue()).decode(), revised_prompt=None)
        with patch.object(s, "_client") as client:
            client.return_value.images.generate.return_value = SimpleNamespace(data=[item])
            text = s.generate_image("test", filename="new-card", preview=False)[0]
        self.assertIn("download: https://testserver/files/", text)
        self.assertNotIn("thumbnail:", text)  # the chat shows the original, not a thumbnail
        self.assertIn("![<file name>](<download URL>)", text)
        self.assertNotIn("[![", text)  # linked images render as broken text in the Claude app

    def test_download_is_inline_and_cacheable(self):
        r = self.get(s._link(self.photo))
        self.assertTrue(r.headers["content-disposition"].startswith("inline"))
        self.assertIn("card.png", r.headers["content-disposition"])
        self.assertEqual(r.headers["cache-control"], "private, max-age=86400")
        self.assertEqual(r.headers["content-type"], "image/png")


if __name__ == "__main__":
    unittest.main()
