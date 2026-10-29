"""The real server in a subprocess: config, secrets, the edit token, cross-site and
DNS rebinding protection, uploads, widgets and the Upcoming tab."""
import datetime as dt
import json
import os
import stat
import struct
import unittest
import zlib

from tests import mock_apps as apps
from tests.helpers import ROOT, MockApp, Switchboard

JSON = {"Content-Type": "application/json"}


def default_config():
    with open(os.path.join(ROOT, "default-config.json")) as f:
        return json.load(f)


def tiny_png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + b"\x40\x80\xa0" * 4 for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class ServerTest(unittest.TestCase):
    """One server for the whole class (starting one takes about a second)."""
    env = None

    @classmethod
    def setUpClass(cls):
        cls.sb = Switchboard(env=cls.env)

    @classmethod
    def tearDownClass(cls):
        cls.sb.stop()

    def req(self, method, path, body=None, headers=None, token=None, host=None):
        return self.sb.request(method, path, body, headers, token, host)


class FirstStartTests(ServerTest):
    def test_config_and_token_are_created(self):
        self.assertTrue(os.path.exists(os.path.join(self.sb.dir, "config.json")))
        mode = stat.S_IMODE(os.stat(os.path.join(self.sb.dir, "edit-token")).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertGreaterEqual(len(self.sb.token), 20)
        self.assertIn(self.sb.token, self.sb.read_log(), "a new token is printed once")

    def test_meta(self):
        status, meta, _ = self.req("GET", "/api/meta")
        self.assertEqual(status, 200)
        self.assertTrue(meta["editProtected"])
        self.assertRegex(meta["version"], r"^\d+\.\d+\.\d+$")

    def test_cache_headers(self):
        self.assertEqual(self.req("GET", "/app.js")[2].get("Cache-Control"), "no-cache")
        self.assertEqual(self.req("GET", "/api/config")[2].get("Cache-Control"), "no-store")

    def test_static_files_cannot_escape_public(self):
        self.assertEqual(self.req("GET", "/../server.py")[0], 404)
        self.assertEqual(self.req("GET", "/api/nope")[0], 404)


class TokenTests(ServerTest):
    def test_changes_need_the_token(self):
        cfg = default_config()
        self.assertEqual(self.req("PUT", "/api/config", cfg)[0], 401)
        self.assertEqual(self.req("PUT", "/api/config", cfg, token="wrong")[0], 401)
        self.assertEqual(self.req("PUT", "/api/config", cfg, token=self.sb.token)[0], 200)

    def test_bad_config_is_rejected(self):
        status, body, _ = self.req("PUT", "/api/config", {"tabs": []}, token=self.sb.token)
        self.assertEqual(status, 400)
        self.assertIn("tabs", body["error"])

    def test_old_layout_is_migrated_into_a_home_tab(self):
        old = {"title": "Old", "groups": [{"name": "G", "services": [{"name": "S", "url": "http://x"}]}]}
        self.assertEqual(self.req("PUT", "/api/config", old, token=self.sb.token)[0], 200)
        cfg = self.req("GET", "/api/config")[1]
        self.assertNotIn("groups", cfg)
        self.assertEqual(cfg["tabs"][0]["name"], "Home")
        self.assertIn("rss", cfg)
        self.req("PUT", "/api/config", default_config(), token=self.sb.token)


class RateLimitTests(ServerTest):
    def test_wrong_tokens_are_rate_limited(self):
        codes = [self.req("PUT", "/api/config", {}, token="wrong")[0] for _ in range(11)]
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10], 429)


class CrossSiteTests(ServerTest):
    def test_other_websites_cannot_make_changes(self):
        t = self.sb.token
        evil = {"Origin": "https://evil.example", **JSON}
        self.assertEqual(self.req("POST", "/api/discover", {"hosts": ""}, evil, token=t)[0], 403)
        self.assertEqual(self.req("PUT", "/api/config", default_config(), evil, token=t)[0], 403)
        self.assertEqual(self.req("POST", "/api/discover", {}, {"Sec-Fetch-Site": "cross-site", **JSON}, token=t)[0], 403)
        self.assertEqual(self.req("POST", "/api/discover", {}, {"Origin": "null", **JSON}, token=t)[0], 403)

    def test_the_dashboards_own_page_can(self):
        own = {"Origin": f"http://127.0.0.1:{self.sb.port}", "Sec-Fetch-Site": "same-origin", **JSON}
        status, body, _ = self.req("POST", "/api/discover", {"useConfigHosts": False, "hosts": ""}, own,
                                   token=self.sb.token)
        self.assertEqual(status, 200, body)

    def test_bodies_must_be_json(self):
        status, _, _ = self.req("POST", "/api/widgets/test", b"{}", {"Content-Type": "text/plain"},
                                token=self.sb.token)
        self.assertEqual(status, 415)


class HostTests(ServerTest):
    def test_unknown_public_names_are_refused(self):
        status, body, _ = self.req("GET", "/api/config", host="evil.example")
        self.assertEqual(status, 421)
        self.assertIn("ALLOWED_HOSTS", body["error"])

    def test_ips_localhost_and_private_names_work(self):
        for host in ("192.168.1.5:3000", "[::1]:3000", "localhost", "dash.lan", "nas.local", "box.home.arpa", "myhost"):
            self.assertEqual(self.req("GET", "/", host=host)[0], 200, host)


class AllowedHostsTests(ServerTest):
    env = {"ALLOWED_HOSTS": "dash.example.com, *.lab.example.org"}

    def test_listed_and_wildcard_names(self):
        self.assertEqual(self.req("GET", "/", host="dash.example.com")[0], 200)
        self.assertEqual(self.req("GET", "/", host="a.lab.example.org")[0], 200)
        self.assertEqual(self.req("GET", "/", host="other.example.com")[0], 421)


class TokenOffTests(ServerTest):
    env = {"EDIT_TOKEN": "off"}

    def test_off_allows_changes_without_a_token(self):
        self.assertFalse(self.req("GET", "/api/meta")[1]["editProtected"])
        self.assertEqual(self.req("PUT", "/api/config", default_config())[0], 200)
        self.assertFalse(os.path.exists(os.path.join(self.sb.dir, "edit-token")))


class ChosenTokenTests(ServerTest):
    env = {"EDIT_TOKEN": "my-own-token"}

    def test_a_chosen_token_is_used_and_not_written(self):
        self.assertEqual(self.req("PUT", "/api/config", default_config(), token="my-own-token")[0], 200)
        self.assertFalse(os.path.exists(os.path.join(self.sb.dir, "edit-token")))


class UploadTests(ServerTest):
    def upload(self, data, ctype="image/png", name="My Photo.png"):
        return self.req("POST", "/api/backgrounds", data, {"Content-Type": ctype, "X-Filename": name},
                        token=self.sb.token)

    def test_upload_serve_and_delete(self):
        png = tiny_png()
        status, img, _ = self.upload(png)
        self.assertEqual(status, 201)
        self.assertRegex(img["name"], r"^my-photo-[0-9a-f]{10}\.png$")
        self.assertEqual(self.upload(png)[1]["name"], img["name"], "the same file is stored once")
        status, body, headers = self.req("GET", img["url"])
        self.assertEqual((status, body, headers["Content-Type"]), (200, png, "image/png"))
        self.assertIn(img["name"], [i["name"] for i in self.req("GET", "/api/backgrounds")[1]["images"]])
        self.assertEqual(self.req("DELETE", "/api/backgrounds/" + img["name"])[0], 401)
        self.assertEqual(self.req("DELETE", "/api/backgrounds/" + img["name"], token=self.sb.token)[0], 200)

    def test_only_real_images_are_accepted(self):
        self.assertEqual(self.upload(b'<svg onload="alert(1)"/>', "image/svg+xml", "x.svg")[0], 415)
        self.assertEqual(self.upload(tiny_png(), "text/plain")[0], 415)

    def test_path_tricks(self):
        self.assertEqual(self.req("GET", "/backgrounds/..%2Fconfig.json")[0], 404)
        self.assertEqual(self.req("DELETE", "/api/backgrounds/..%2Fconfig.json", token=self.sb.token)[0], 404)


class WidgetAndSecretTests(unittest.TestCase):
    """Widgets through the server, with a mock Sonarr and Radarr."""

    @classmethod
    def setUpClass(cls):
        cls.sonarr = MockApp(apps.sonarr())
        cls.radarr = MockApp(apps.radarr())
        cfg = default_config()
        cfg["upcoming"] = {"enabled": True, "days": 7, "pastDays": 1}
        cfg["tabs"] = [{"name": "Media", "groups": [{"name": "Library", "services": [
            {"id": "son", "name": "Sonarr", "url": cls.sonarr.url, "widget": {"type": "sonarr"}},
            {"id": "rad", "name": "Radarr", "url": cls.radarr.url, "widget": {"type": "radarr"}},
            {"id": "bad", "name": "Broken", "url": cls.sonarr.url, "widget": {"type": "sonarr"}},
        ]}]}]
        cls.sb = Switchboard(config=cfg, env={"EDIT_TOKEN": "tok"})
        for sid, key in (("son", apps.KEY), ("rad", apps.KEY), ("bad", "wrong")):
            status, body, _ = cls.sb.request("PUT", f"/api/secrets/{sid}", {"apiKey": key}, token="tok")
            assert status == 200, body

    @classmethod
    def tearDownClass(cls):
        cls.sb.stop()
        cls.sonarr.close()
        cls.radarr.close()

    def test_widgets_endpoint(self):
        w = self.sb.request("GET", "/api/widgets")[1]["widgets"]
        self.assertEqual({f["label"]: f["value"] for f in w["son"]["fields"]}["Queue"], 3)
        self.assertIn("401", w["bad"]["error"])

    def test_secrets_never_reach_the_browser(self):
        for path in ("/api/config", "/api/widgets", "/api/secrets/son", "/api/upcoming"):
            raw = json.dumps(self.sb.request("GET", path)[1])
            self.assertNotIn(apps.KEY, raw, path)
        self.assertEqual(self.sb.request("GET", "/api/secrets/son")[1], {"set": ["apiKey"]})

    def test_secrets_need_the_token_and_a_valid_id(self):
        self.assertEqual(self.sb.request("PUT", "/api/secrets/son", {"apiKey": "x"})[0], 401)
        self.assertEqual(self.sb.request("PUT", "/api/secrets/..%2Fx", {}, token="tok")[0], 400)

    def test_a_saved_secret_can_be_removed(self):
        put = lambda body: self.sb.request("PUT", "/api/secrets/tmp", body, token="tok")[0]
        self.assertEqual(put({"apiKey": "k", "password": "p"}), 200)
        self.assertEqual(put({"apiKey": ""}), 200)  # blank keeps the saved value
        self.assertEqual(self.sb.request("GET", "/api/secrets/tmp")[1], {"set": ["apiKey", "password"]})
        self.assertEqual(put({"apiKey": None}), 200)  # null removes it, which the edit dialog relies on
        self.assertEqual(self.sb.request("GET", "/api/secrets/tmp")[1], {"set": ["password"]})
        self.assertEqual(put({"password": None}), 200)
        self.assertEqual(self.sb.request("GET", "/api/secrets/tmp")[1], {"set": []})

    def test_widget_test_endpoint_uses_stored_secrets(self):
        body = {"id": "son", "url": self.sonarr.url, "widget": {"type": "sonarr"}, "secrets": {"apiKey": ""}}
        r = self.sb.request("POST", "/api/widgets/test", body, token="tok")[1]
        self.assertIn("fields", r)
        body["secrets"] = {"apiKey": "wrong"}
        self.assertIn("401", self.sb.request("POST", "/api/widgets/test", body, token="tok")[1]["error"])
        self.assertEqual(self.sb.request("POST", "/api/widgets/test", body)[0], 401)

    def test_upcoming_merges_calendars_in_date_order(self):
        d = self.sb.request("GET", "/api/upcoming")[1]
        self.assertTrue(d["enabled"])
        self.assertEqual(len(d["items"]), len(apps.SONARR_CALENDAR) + 2)  # "Broken" has a wrong key: no items
        self.assertEqual({s["name"] for s in d["sources"] if "error" in s}, {"Broken"})
        when = [dt.datetime.fromisoformat(i["date"].replace("Z", "+00:00")) for i in d["items"]]
        self.assertEqual(when, sorted(when))
        self.assertEqual({i["source"] for i in d["items"]}, {"Sonarr", "Radarr"})


class PruneSecretsTests(unittest.TestCase):
    def test_removing_a_widget_deletes_its_secrets_but_keeps_alert_settings(self):
        cfg = default_config()
        cfg["tabs"] = [{"name": "T", "groups": [{"name": "G", "services": [
            {"id": "a", "name": "A", "url": "http://127.0.0.1:9", "widget": {"type": "sonarr"}}]}]}]
        sb = Switchboard(config=cfg, env={"EDIT_TOKEN": "tok"},
                         secrets={"a": {"apiKey": "x"}, "gone": {"apiKey": "y"}, "_alerts": {"webhookUrl": "http://h"}})
        try:
            cfg["tabs"][0]["groups"][0]["services"][0].pop("widget")
            self.assertEqual(sb.request("PUT", "/api/config", cfg, token="tok")[0], 200)
            with open(os.path.join(sb.dir, "secrets.json")) as f:
                self.assertEqual(set(json.load(f)), {"_alerts"})
        finally:
            sb.stop()


if __name__ == "__main__":
    unittest.main()
