"""Alerts (debounce, recovery, grouping, delivery) and the 24-hour uptime history."""
import os
import tempfile
import time
import unittest

from tests.helpers import MockApp

import monitor


class Webhook:
    """A fake Discord / JSON webhook that records what it receives."""

    def __init__(self):
        self.posts = []

        def handle(r):
            if "Python-urllib" in (r.headers.get("User-Agent") or ""):
                return 403, {}  # Discord rejects urllib's default user agent
            if r.path in ("/discord", "/json"):
                self.posts.append(r.json())
                return 204, b""
            return None
        self.app = MockApp(handle)

    def titles(self):
        out = [e["title"] for p in self.posts for e in p.get("embeds", p.get("events", []))]
        self.posts.clear()
        return out


class FormatTests(unittest.TestCase):
    def test_format_value(self):
        f = monitor.format_value
        self.assertEqual(f(95.24, "percent"), "95%")
        self.assertEqual(f(5.25, "percent"), "5.2%")
        self.assertEqual(f(7 * 1024 ** 3, "bytes"), "7.0 GB")
        self.assertEqual(f(5 * 1024 ** 2, "speed"), "5.0 MB/s")
        self.assertEqual(f(3725, "duration"), "1h 2m")
        self.assertEqual(f("4/5", "text"), "4/5")
        self.assertEqual(f(None, "number"), "—")


class AlerterTests(unittest.TestCase):
    def check(self, bad):
        return [("k", bad, {"kind": "down", "title": "down"}, lambda gone: {"kind": "up", "title": "up", "gone": gone})]

    def test_debounce_one_alert_and_one_recovery(self):
        a = monitor.Alerter()
        self.assertEqual(a.evaluate(self.check(True), 2), [])                       # first failure: a blip
        self.assertEqual(a.evaluate(self.check(False), 2), [])                      # recovered: nothing to say
        self.assertEqual(a.evaluate(self.check(True), 2), [])
        self.assertEqual([e["title"] for e in a.evaluate(self.check(True), 2)], ["down"])
        self.assertEqual(a.evaluate(self.check(True), 2), [], "no repeat while still down")
        up = a.evaluate(self.check(False), 2)
        self.assertEqual([e["title"] for e in up], ["up"])
        self.assertGreaterEqual(up[0]["gone"], 0)
        self.assertEqual(a.evaluate(self.check(False), 2), [])

    def test_failures_setting(self):
        a = monitor.Alerter()
        self.assertEqual(len(a.evaluate(self.check(True), 1)), 1)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.hook = Webhook()
        self.dir = tempfile.mkdtemp()
        self.cfg = {
            "title": "Lab", "alerts": {"enabled": True, "type": "discord", "failures": 2},
            "system": {"enabled": True, "warnPercent": 90, "warnTemp": 80},
            "tabs": [{"name": "T", "groups": [{"name": "G", "services": [
                {"name": "Plex", "url": "http://plex/web"},
                {"name": "Sonarr", "url": "http://sonarr"},
                {"id": "pve", "name": "Proxmox", "url": "https://pve", "widget": {"type": "proxmox"}},
                {"name": "Bookmark", "url": "https://example.com", "check": False}]}]}]}
        self.status = {"http://plex/web": {"up": True, "code": 200}, "http://sonarr": {"up": True, "code": 200},
                       "https://pve": {"up": True, "code": 200}}
        self.widgets = {"pve": {"fields": [{"label": "RAM", "value": 50.0, "format": "percent"}]}}
        self.system = {"hostname": "lab", "temp": {"c": 45}, "memory": {"total": 32e9, "available": 16e9},
                       "disks": [{"path": "/", "label": "System", "total": 100e9, "used": 50e9, "free": 50e9}]}
        self.m = monitor.Monitor(
            os.path.join(self.dir, "history.json"), load_config=lambda: self.cfg,
            collect_status=lambda force: self.status, collect_widgets=lambda: self.widgets,
            get_system=lambda: self.system, alert_webhook=lambda: self.hook.app.url + "/discord")

    def tearDown(self):
        self.hook.app.close()

    def tick(self):
        self.m.tick()
        return self.hook.titles()

    def test_services_down_grouped_and_back_up(self):
        self.assertEqual(self.tick(), [])
        self.status["http://plex/web"] = {"up": False, "error": "timed out"}
        self.status["http://sonarr"] = {"up": False, "code": 502}
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.tick(), ["🔴 Plex is down", "🔴 Sonarr is down"])
        self.assertEqual(len(self.hook.posts), 0)
        self.assertEqual(self.tick(), [])
        self.status["http://plex/web"] = {"up": True, "code": 200}
        self.assertEqual(self.tick(), ["🟢 Plex is back up"])

    def test_discord_message_format(self):
        self.status["http://plex/web"] = {"up": False, "error": "timed out"}
        self.m.tick()
        self.m.tick()
        post = self.hook.posts[0]
        self.assertEqual(post["username"], "Lab")
        embed = post["embeds"][0]
        self.assertEqual(embed["color"], monitor.COLOURS["down"])
        self.assertIn("timed out", embed["description"])
        self.assertIn("timestamp", embed)

    def test_widget_warnings_and_system_thresholds(self):
        self.widgets["pve"]["fields"][0].update(value=95.2, warn=True)
        self.system["temp"]["c"] = 86
        self.system["disks"][0].update(used=93e9, free=7e9)
        self.tick()
        self.assertEqual(sorted(self.tick()), sorted([
            "🟠 Proxmox: RAM 95%", "🟠 lab: CPU temperature 86°C", "🟠 lab: System disk 93% full (6.5 GB free)"]))
        self.widgets["pve"]["fields"][0].update(value=60.0, warn=False)
        self.system["temp"]["c"] = 50
        self.system["disks"][0].update(used=50e9, free=50e9)
        self.assertEqual(len(self.tick()), 3)

    def test_switching_alerts_off_sends_nothing(self):
        self.cfg["alerts"]["enabled"] = False
        self.status["http://plex/web"] = {"up": False, "error": "x"}
        self.tick()
        self.assertEqual(self.tick(), [])

    def test_unchecked_services_never_alert(self):
        self.status["https://example.com"] = {"up": False, "error": "x"}
        self.tick()
        self.assertEqual(self.tick(), [])

    def test_delivery_errors_are_recorded_without_the_url(self):
        self.m.h["alert_webhook"] = lambda: self.hook.app.url + "/secret-path-xyz"
        self.status["http://plex/web"] = {"up": False, "error": "x"}
        self.m.tick()
        self.m.tick()
        self.assertIn("HTTP 404", self.m.status["lastError"])
        self.assertNotIn("secret-path-xyz", self.m.status["lastError"])

    def test_json_webhook(self):
        monitor.send([{"kind": "down", "title": "X is down", "message": "m"}], "webhook",
                     self.hook.app.url + "/json", "Lab")
        post = self.hook.posts[0]
        self.assertEqual((post["source"], post["events"][0]["title"]), ("Lab", "X is down"))


class HistoryTests(unittest.TestCase):
    def test_buckets_uptime_and_persistence(self):
        path = os.path.join(tempfile.mkdtemp(), "history.json")
        h = monitor.UptimeHistory(path)
        now = time.time()
        for up in (True, True, True, False):
            h.record({"http://a": {"up": up}}, now)
        s = h.summary(now)["services"]["http://a"]
        self.assertEqual(len(s["buckets"]), monitor.BUCKETS)
        self.assertEqual(s["buckets"][-1], 75.0)
        self.assertIsNone(s["buckets"][0])
        self.assertEqual(s["uptime"], 75.0)
        h.save()
        self.assertEqual(monitor.UptimeHistory(path).summary(now)["services"]["http://a"]["uptime"], 75.0)

    def test_old_buckets_are_dropped(self):
        h = monitor.UptimeHistory(os.path.join(tempfile.mkdtemp(), "h.json"))
        now = time.time()
        h.record({"http://a": {"up": True}}, now - 2 * 86400)
        h.record({"http://b": {"up": True}}, now)
        self.assertNotIn("http://a", h.summary(now)["services"])


if __name__ == "__main__":
    unittest.main()
