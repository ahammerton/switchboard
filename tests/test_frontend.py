"""Static checks of the front end (no browser needed)."""
import os
import re
import shutil
import subprocess
import unittest

from tests.helpers import ROOT

PUBLIC = os.path.join(ROOT, "public")


def read(name):
    with open(os.path.join(PUBLIC, name), encoding="utf-8") as f:
        return f.read()


class FrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js isn't installed")
    def test_app_js_syntax(self):
        r = subprocess.run(["node", "--check", os.path.join(PUBLIC, "app.js")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

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
