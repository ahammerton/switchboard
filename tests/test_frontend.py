"""Static checks of the front end (no browser needed)."""
import json
import os
import re
import shutil
import struct
import subprocess
import unittest

from tests.helpers import ROOT

PUBLIC = os.path.join(ROOT, "public")


def read(name):
    with open(os.path.join(PUBLIC, name), encoding="utf-8") as f:
        return f.read()


class FrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js isn't installed")
    def test_js_syntax(self):
        for name in ("app.js", "sw.js"):
            r = subprocess.run(["node", "--check", os.path.join(PUBLIC, name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, name + ": " + r.stderr)

    def test_manifest_icons_exist_at_their_sizes(self):
        manifest = json.loads(read("manifest.webmanifest"))
        self.assertEqual(manifest["display"], "standalone")
        purposes = set()
        for icon in manifest["icons"]:
            path = os.path.join(PUBLIC, icon["src"])
            self.assertTrue(os.path.exists(path), icon["src"])
            purposes.add(icon["purpose"])
            if icon["type"] == "image/png":
                with open(path, "rb") as f:
                    head = f.read(24)
                self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n", icon["src"])
                self.assertEqual("%dx%d" % struct.unpack(">II", head[16:24]), icon["sizes"], icon["src"])
        self.assertEqual(purposes, {"any", "maskable"})

    def test_offline_page_is_what_the_service_worker_caches(self):
        self.assertIn('"offline.html"', read("sw.js"))
        self.assertTrue(os.path.exists(os.path.join(PUBLIC, "offline.html")))

    def test_every_element_app_js_looks_up_exists(self):
        html_ids = set(re.findall(r'id="([^"]+)"', read("index.html")))
        js_ids = set(re.findall(r'\$\("#([\w-]+)', read("app.js"))) | \
            set(re.findall(r'getElementById\("([\w-]+)"\)', read("app.js")))
        self.assertTrue(js_ids, "no element lookups found")
        self.assertEqual(js_ids - html_ids, set())

    def test_page_loads_only_local_files(self):
        html = read("index.html")
        for src in re.findall(r'(?:src|href)="([^"]+)"', html):
            if src.startswith("data:"):
                continue
            self.assertFalse(src.startswith(("http:", "https:", "//")), src)
            self.assertTrue(os.path.exists(os.path.join(PUBLIC, src)), src)


if __name__ == "__main__":
    unittest.main()
