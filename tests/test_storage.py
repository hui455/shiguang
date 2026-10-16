import base64, importlib.util, os, tempfile, threading, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
os.environ["MCP_TRANSPORT"] = "stdio"
source = Path("/app/server.py")
if not source.exists():
    source = Path(__file__).resolve().parents[1] / "plugins/openai-image-gen/server.py"
import sys
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("image_server", source)
s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        for name, value in [("OUTPUT_DIR", Path(self.tmp.name)), ("IMAGE_STORAGE_MAX_BYTES", 100), ("IMAGE_MIN_FREE_BYTES", 20)]:
            p = patch.object(s, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(s.shutil, "disk_usage", return_value=SimpleNamespace(free=1000))
        self.disk = p.start(); self.addCleanup(p.stop)
    def data(self, *items):
        return [SimpleNamespace(b64_json=base64.b64encode(x).decode()) for x in items]
    def test_success_existing_preserved(self):
        (s.OUTPUT_DIR / "cat.png").write_bytes(b"old")
        paths = s._save(self.data(b"new"), "cat", "cat", "png")
        self.assertEqual(paths[0].read_bytes(), b"new")
        self.assertEqual((s.OUTPUT_DIR / "cat.png").read_bytes(), b"old")
    def test_whole_batch_quota(self):
        with self.assertRaisesRegex(RuntimeError, "storage limit"):
            s._save(self.data(b"a" * 60, b"b" * 60), "x", None, "png")
        self.assertEqual(list(s.OUTPUT_DIR.iterdir()), [])
    def test_free_space_reserve(self):
        self.disk.return_value.free = 30
        with self.assertRaisesRegex(RuntimeError, "Disk space"):
            s._save(self.data(b"a" * 11), "x", None, "png")
        self.assertEqual(list(s.OUTPUT_DIR.iterdir()), [])
    def test_block_before_api_generate_and_edit(self):
        self.disk.return_value.free = 19
        with patch.object(s, "_client") as client, patch.object(s, "_open_input") as opened:
            for call in [lambda: s.generate_image("test"), lambda: s.edit_image("test", ["unused"])]:
                with self.assertRaisesRegex(RuntimeError, "Disk space"): call()
            client.assert_not_called(); opened.assert_not_called()
    def test_quota_blocks_before_api(self):
        (s.OUTPUT_DIR / "existing").write_bytes(b"a" * 100)
        with patch.object(s, "_client") as client:
            with self.assertRaisesRegex(RuntimeError, "storage limit"): s.generate_image("test")
            client.assert_not_called()
    def test_space_drop_rolls_back(self):
        self.disk.side_effect = [SimpleNamespace(free=1000), SimpleNamespace(free=1000), SimpleNamespace(free=20)]
        (s.OUTPUT_DIR / "existing").write_bytes(b"old")
        with self.assertRaisesRegex(RuntimeError, "Disk space"):
            s._save(self.data(b"first", b"second"), "x", None, "png")
        self.assertEqual([p.name for p in s.OUTPUT_DIR.iterdir()], ["existing"])
    def test_invalid_response_rolls_back(self):
        data = self.data(b"first") + [SimpleNamespace(b64_json="!!!!")]
        with self.assertRaises(ValueError): s._save(data, "x", None, "png")
        self.assertEqual(list(s.OUTPUT_DIR.iterdir()), [])
    def test_concurrency_and_unlock(self):
        ready = threading.Event(); release = threading.Event()
        @s._storage_guard
        def slow(): ready.set(); release.wait(5)
        t = threading.Thread(target=slow); t.start()
        try:
            self.assertTrue(ready.wait(2))
            with self.assertRaisesRegex(RuntimeError, "Another image"): s.generate_image("test")
        finally: release.set(); t.join()
        @s._storage_guard
        def failing(): raise ValueError("expected")
        with self.assertRaises(ValueError): failing()
        s._storage_guard(lambda: None)()
if __name__ == "__main__": unittest.main()
