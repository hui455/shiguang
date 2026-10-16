"""Image records: prompt, parameters and references saved per image, shown in the gallery."""
import base64
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image
from starlette.testclient import TestClient

os.environ["MCP_TRANSPORT"] = "stdio"
source = Path("/app/server.py")
if not source.exists():
    source = Path(__file__).resolve().parents[1] / "plugins/openai-image-gen/server.py"
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("records_server_test", source)
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

PASSWORD = "test-admin-password-12345"
ORIGIN = {"Origin": "https://testserver"}


def png_bytes(color="blue", size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def api_item(data=None, revised=None):
    return SimpleNamespace(b64_json=base64.b64encode(data or png_bytes()).decode(), revised_prompt=revised)


class Records(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in [("OUTPUT_DIR", Path(self.tmp.name)), ("TRANSPORT", "http"),
                            ("PUBLIC_BASE_URL", "https://testserver"), ("SECRET", "connector-secret-hidden")]:
            p = patch.object(s, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"IMAGE_ADMIN_PASSWORD": PASSWORD, "OPENAI_API_KEY": "sk-test"})
        p.start(); self.addCleanup(p.stop)
        p = patch.object(s.shutil, "disk_usage", return_value=SimpleNamespace(free=10**12))
        p.start(); self.addCleanup(p.stop)
        self.client_patch = patch.object(s, "_client")
        self.api = self.client_patch.start(); self.addCleanup(self.client_patch.stop)

    def generate(self, prompt, filename, revised=None):
        self.api.return_value.images.generate.return_value = SimpleNamespace(data=[api_item(revised=revised)])
        return s.generate_image(prompt, filename=filename, quality="medium", size="1024x1536", preview=False)[0]

    def edit(self, prompt, images, filename, mask=None):
        self.api.return_value.images.edit.return_value = SimpleNamespace(data=[api_item(png_bytes("red"))])
        return s.edit_image(prompt, images, mask=mask, filename=filename, preview=False)[0]

    def admin(self):
        c = TestClient(s._http_app(), base_url="https://testserver")
        self.addCleanup(c.close)
        r = c.post("/admin/api/login", json={"password": PASSWORD, "remember": False}, headers=ORIGIN)
        self.assertEqual(r.status_code, 200)
        return c, r.json()["csrf"]

    # ---- recording ----
    def test_generate_saves_prompt_and_parameters(self):
        self.generate("An elf girl in a market, anime illustration style", "elf-ref", revised="An elf girl…")
        record = s.read_meta("elf-ref.png")
        self.assertEqual(record["kind"], "generate")
        self.assertEqual(record["prompt"], "An elf girl in a market, anime illustration style")
        self.assertEqual(record["revised_prompt"], "An elf girl…")
        self.assertEqual((record["quality"], record["size"]), ("medium", "1024x1536"))
        self.assertTrue(record["model"])

    def test_edit_records_gallery_reference_by_name_without_copying(self):
        self.generate("reference portrait", "elf-ref")
        self.edit("same girl at the market", ["elf-ref.png"], "scene01")
        record = s.read_meta("scene01.png")
        self.assertEqual(record["kind"], "edit")
        self.assertEqual(record["references"], [{"source": "gallery", "name": "elf-ref.png"}])
        names = sorted(p.name for p in s.OUTPUT_DIR.iterdir() if not p.name.startswith("."))
        self.assertEqual(names, ["elf-ref.png", "scene01.png"])  # no extra copy

    def test_own_download_link_counts_as_gallery_reference(self):
        self.generate("reference portrait", "elf-ref")
        link = s._link(s.OUTPUT_DIR / "elf-ref.png")
        with patch.object(s.urllib.request, "urlopen") as fetch:
            self.edit("same girl", [link], "scene02")
            fetch.assert_not_called()
        self.assertEqual(s.read_meta("scene02.png")["references"][0]["name"], "elf-ref.png")

    def test_url_reference_is_copied_into_gallery(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = png_bytes("green")
        with patch.object(s, "_is_public_host", return_value=True), \
                patch.object(s.urllib.request, "urlopen", return_value=response):
            text = self.edit("restyle", ["https://example.com/pics/cat.png"], "cat-anime")
        ref = s.read_meta("cat-anime.png")["references"][0]
        self.assertEqual(ref["source"], "url")
        self.assertEqual(ref["url"], "https://example.com/pics/cat.png")
        self.assertEqual(ref["name"], "cat-anime-ref1.png")
        self.assertTrue((s.OUTPUT_DIR / "cat-anime-ref1.png").is_file())
        self.assertEqual(s.read_meta("cat-anime-ref1.png")["kind"], "reference")
        self.assertIn("leave them out of the ps block", text)

    def test_record_failure_does_not_fail_generation(self):
        with patch.object(s, "_write_meta", side_effect=OSError("disk")):
            text = self.generate("prompt", "still-works")
        self.assertIn("still-works.png", text)
        self.assertTrue((s.OUTPUT_DIR / "still-works.png").is_file())

    # ---- tools ----
    def test_list_images_and_image_info_show_records(self):
        self.generate("A cozy tavern " + "x" * 300, "tavern")
        self.edit("same tavern at night", ["tavern.png"], "tavern-night")
        listing = s.list_images()
        self.assertIn("prompt: A cozy tavern", listing)
        self.assertIn("…", listing)  # long prompts are shortened in the list
        self.assertIn("references: tavern.png", listing)
        info = s.image_info("tavern.png")
        self.assertIn("x" * 300, info)  # full prompt
        self.assertIn("quality=medium", info)
        Image.new("RGB", (8, 8)).save(s.OUTPUT_DIR / "old.png")  # made before records existed
        self.assertIn("no record", s.image_info("old.png"))

    # ---- gallery ----
    def test_gallery_shows_record_and_searches_prompts(self):
        self.generate("Sample-Character the elf, anime illustration style", "char-a")
        self.generate("a pumpkin pie stall", "stall")
        c, _ = self.admin()
        items = {i["name"]: i for i in c.get("/admin/api/state").json()["items"]}
        self.assertEqual(items["char-a.png"]["record"]["prompt"], "Sample-Character the elf, anime illustration style")
        found = [i["name"] for i in c.get("/admin/api/state?q=sample-character").json()["items"]]
        self.assertEqual(found, ["char-a.png"])

    def test_used_by_and_deleted_reference(self):
        self.generate("portrait", "elf-ref")
        self.edit("scene one", ["elf-ref.png"], "scene01")
        self.edit("scene two", ["elf-ref.png"], "scene02")
        c, csrf = self.admin()
        items = {i["name"]: i for i in c.get("/admin/api/state").json()["items"]}
        self.assertEqual(items["elf-ref.png"]["used_by_count"], 2)
        self.assertEqual(items["elf-ref.png"]["used_by"], ["scene01.png", "scene02.png"])
        ref = items["scene01.png"]["record"]["references"][0]
        self.assertEqual((ref["name"], ref["exists"]), ("elf-ref.png", True))

        r = c.post("/admin/api/delete", json={"names": ["elf-ref.png"]},
                   headers={**ORIGIN, "X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(s.read_meta("elf-ref.png"))  # its own record goes with it
        scene = c.get("/admin/api/item/scene01.png").json()["item"]
        self.assertEqual(scene["record"]["prompt"], "scene one")  # prompt kept
        self.assertFalse(scene["record"]["references"][0]["exists"])

    def test_item_endpoint_requires_login_and_existing_image(self):
        self.generate("portrait", "elf-ref")
        anonymous = TestClient(s._http_app(), base_url="https://testserver")
        self.addCleanup(anonymous.close)
        self.assertEqual(anonymous.get("/admin/api/item/elf-ref.png").status_code, 401)
        c, _ = self.admin()
        self.assertEqual(c.get("/admin/api/item/elf-ref.png").status_code, 200)
        self.assertEqual(c.get("/admin/api/item/missing.png").status_code, 404)

    def test_records_are_never_listed_or_downloadable(self):
        self.generate("portrait", "elf-ref")
        c, _ = self.admin()
        names = [i["name"] for i in c.get("/admin/api/state").json()["items"]]
        self.assertEqual(names, ["elf-ref.png"])
        meta_name = s._meta_path("elf-ref.png").name
        self.assertEqual(c.get(f"/admin/api/image/{meta_name}").status_code, 404)
        self.assertEqual(c.get("/admin/api/image/.image-meta").status_code, 404)


if __name__ == "__main__":
    unittest.main()
