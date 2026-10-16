import asyncio
import base64
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ["MCP_TRANSPORT"] = "stdio"
source = Path("/app/server.py")
if not source.exists():
    source = Path(__file__).resolve().parents[1] / "plugins/openai-image-gen/server.py"
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("config_test_server", source)
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)
FLARE, SUNBURST = s.SUPPORTED_MODELS


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for key, value in [("OUTPUT_DIR", Path(self.temp.name)), ("DEFAULT_MODEL", FLARE),
                           ("DEFAULT_SIZE", "auto"), ("DEFAULT_QUALITY", "auto")]:
            p = patch.object(s, key, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(s.shutil, "disk_usage", return_value=SimpleNamespace(free=10**12))
        p.start(); self.addCleanup(p.stop)

    def test_initial_default_and_catalog(self):
        self.assertEqual(s._defaults()["model"], FLARE)
        info = s.image_config()
        self.assertIn(FLARE, info); self.assertIn(SUNBURST, info)
        self.assertFalse((s.OUTPUT_DIR / ".image-config.json").exists())

    def test_persistence_and_partial_update(self):
        s.set_image_config(model=SUNBURST, size="1024x1536", quality="xhigh")
        s.set_image_config(quality="max")
        self.assertEqual(s._defaults(), {"model": SUNBURST, "size": "1024x1536", "quality": "max"})
        # A new module instance reads the persisted file, not a process-local value.
        fresh_spec = importlib.util.spec_from_file_location("fresh_config_server", source)
        fresh = importlib.util.module_from_spec(fresh_spec); fresh_spec.loader.exec_module(fresh)
        with patch.object(fresh, "OUTPUT_DIR", s.OUTPUT_DIR):
            self.assertEqual(fresh._defaults(), s._defaults())

    def test_invalid_config_does_not_change_saved_values(self):
        s.set_image_config(model=SUNBURST)
        before = (s.OUTPUT_DIR / ".image-config.json").read_bytes()
        for kwargs in [{"model": ""}, {"model": "bad model name"}, {"model": "x" * 200},
                       {"quality": "Not Valid!"}, {"size": "bad"}]:
            with self.assertRaises(ValueError): s.set_image_config(**kwargs)
            self.assertEqual((s.OUTPUT_DIR / ".image-config.json").read_bytes(), before)

    def test_failed_atomic_write_preserves_previous_config(self):
        s.set_image_config(model=SUNBURST)
        with patch.object(s.os, "replace", side_effect=OSError("disk write error")):
            with self.assertRaises(OSError): s.set_image_config(model=FLARE)
        self.assertEqual(s._defaults()["model"], SUNBURST)
        self.assertEqual(list(s.OUTPUT_DIR.glob(".image-config-*")), [])

    def test_generation_uses_defaults_and_preserves_override(self):
        s.set_image_config(model=SUNBURST, quality="high")
        item = SimpleNamespace(b64_json=base64.b64encode(b"test bytes").decode(), revised_prompt=None)
        with patch.object(s, "_client") as client:
            client.return_value.images.generate.return_value = SimpleNamespace(data=[item, item])
            result = s.generate_image("test", n=2, preview=False)
            args = client.return_value.images.generate.call_args.kwargs
            self.assertEqual((args["model"], args["quality"], args["n"]), (SUNBURST, "high", 2))
            self.assertIn("Generated 2 image", result[0])
            s.generate_image("test", model=FLARE, quality="low", preview=False)
            self.assertEqual(client.return_value.images.generate.call_args.kwargs["model"], FLARE)
        self.assertEqual(s._defaults()["model"], SUNBURST)
        self.assertEqual(s._defaults()["quality"], "high")

    def test_edit_uses_persisted_defaults(self):
        s.set_image_config(model=SUNBURST, size="1024x1024", quality="medium")
        item = SimpleNamespace(b64_json=base64.b64encode(b"edited").decode())
        with patch.object(s, "_open_input", return_value=io.BytesIO(b"input")), patch.object(s, "_client") as client:
            client.return_value.images.edit.return_value = SimpleNamespace(data=[item])
            s.edit_image("test", ["input.png"], preview=False)
            args = client.return_value.images.edit.call_args.kwargs
            self.assertEqual((args["model"], args["size"], args["quality"]), (SUNBURST, "1024x1024", "medium"))

    def test_other_model_names_pass_through(self):
        # Relays and future models: any well-formed model name reaches the API unchanged.
        item = SimpleNamespace(b64_json=base64.b64encode(b"x").decode(), revised_prompt=None)
        with patch.object(s, "_client") as client:
            client.return_value.images.generate.return_value = SimpleNamespace(data=[item])
            for model in ["gpt-image-2", "gpt-image-2.6-flare", "relay/gpt-image-2.5-flare"]:
                s.generate_image("test", model=model, preview=False)
                self.assertEqual(client.return_value.images.generate.call_args.kwargs["model"], model)
        s.set_image_config(model="gpt-image-2", quality="hd")
        self.assertEqual(s._defaults()["model"], "gpt-image-2")
        self.assertEqual(s._defaults()["quality"], "hd")

    def test_old_env_model_keeps_working(self):
        # An IMAGE_MODEL left over from an older .env must not break the server.
        with patch.object(s, "DEFAULT_MODEL", "gpt-image-2"):
            self.assertIn("model: gpt-image-2", s.image_config())

    def test_mcp_tool_schema_and_description(self):
        tools = {t.name: t for t in s.mcp._tool_manager.list_tools()}
        for name in ["generate_image", "edit_image", "set_image_config"]:
            props = tools[name].parameters["properties"]
            self.assertNotIn("enum", str(props["model"]))  # free text, not a fixed list
            self.assertIn(FLARE, tools[name].description)
        asyncio.run(s.mcp.call_tool("set_image_config", {"model": SUNBURST}))
        self.assertEqual(s._defaults()["model"], SUNBURST)


if __name__ == "__main__": unittest.main()
