"""Find services: recognising apps, where to look, and never scanning public addresses."""
import unittest
from unittest import mock

from tests import mock_apps as apps
from tests.helpers import MockApp

import discover


def page(title, headers=None, status=200):
    return lambda r: (status, f"<html><head><title>{title}</title></head></html>".encode(), headers or {})


class IdentifyTests(unittest.TestCase):
    def setUp(self):
        self.mocks = []

    def tearDown(self):
        for m in self.mocks:
            m.close()

    def identify(self, handler):
        m = MockApp(handler)
        self.mocks.append(m)
        return discover.identify("127.0.0.1", m.port), m

    def test_recognised_apps(self):
        for title, app_id, widget in (("Sonarr", "sonarr", "sonarr"), ("Jellyseerr", "jellyseerr", "seerr"),
                                      ("pve1 - Proxmox Virtual Environment", "proxmox", "proxmox"),
                                      ("Proxmox Backup Server", "pbs", None), ("Home Assistant", "homeassistant", "homeassistant")):
            c, _ = self.identify(page(title))
            self.assertEqual((c["app"], c["widget"]), (app_id, widget), title)

    def test_plex_by_header_and_pihole_admin_path(self):
        c, m = self.identify(page("Unauthorized", {"X-Plex-Protocol": "1.0"}, status=401))
        self.assertEqual((c["app"], c["url"]), ("plex", m.url + "/web"))
        c, m = self.identify(page("Pi-hole - pi.hole"))
        self.assertEqual(c["url"], m.url + "/admin")

    def test_unknown_and_boring_pages(self):
        c, _ = self.identify(page("My Cool App - Dashboard"))
        self.assertEqual((c["app"], c["name"]), (None, "My Cool App"))
        self.assertIsNone(self.identify(page("Welcome to nginx!"))[0])
        self.assertIsNone(self.identify(lambda r: b"no title here")[0])


class WhereToLookTests(unittest.TestCase):
    def test_parse_hosts(self):
        hosts, errors = discover.parse_hosts("nas.lan, 10.0.0.5 10.0.1.0/30; [fe80::1]")
        self.assertEqual(hosts, ["nas.lan", "10.0.0.5", "10.0.1.1", "10.0.1.2", "fe80::1"])
        self.assertEqual(errors, [])
        _, errors = discover.parse_hosts("10.0.0.0/16 bad/range")
        self.assertEqual(len(errors), 2)

    def test_only_private_addresses_are_scanned(self):
        for ip in ("192.168.1.5", "10.1.2.3", "172.16.0.1", "127.0.0.1", "169.254.1.1", "fd00::1"):
            self.assertTrue(discover.is_local(ip), ip)
        for ip in ("8.8.8.8", "151.101.1.1", "224.0.0.1", "0.0.0.0", "not-an-ip"):
            self.assertFalse(discover.is_local(ip), ip)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.sonarr = MockApp(page("Sonarr"))
        self.radarr = MockApp(page("Radarr"))
        self.me = MockApp(page("Switchboard"))
        ports = [self.sonarr.port, self.radarr.port, self.me.port]
        names = {"127.0.0.1": "127.0.0.1", "sonarr.example.com": "127.0.0.1", "reddit.com": "151.101.1.140"}
        self.patches = [mock.patch.object(discover, "PORTS", ports),
                        mock.patch.object(discover, "resolve", lambda h: names.get(h))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for m in (self.sonarr, self.radarr, self.me):
            m.close()

    def cfg(self, *services):
        return {"tabs": [{"name": "T", "groups": [{"name": "G", "services": list(services)}]}]}

    def test_finds_new_apps_and_skips_listed_ones_and_itself(self):
        cfg = self.cfg({"name": "Radarr", "url": self.radarr.url},
                       {"name": "Sonarr", "url": "https://sonarr.example.com"},  # same app behind a proxy name
                       {"name": "r/homelab", "url": "https://reddit.com/r/homelab"})
        r = discover.scan(cfg, {}, use_config_hosts=True, use_proxmox=False,
                          self_endpoints={("127.0.0.1", self.me.port)})
        found = {c["name"]: c for c in r["candidates"]}
        self.assertNotIn("Radarr", found, "the exact endpoint is already on the dashboard")
        self.assertNotIn("Switchboard", found, "the dashboard itself is skipped")
        self.assertEqual(found["Sonarr"]["already"], "Sonarr", "flagged as probably already listed")
        self.assertTrue(any("public site" in n and "reddit.com" in n for n in r["notes"]))
        self.assertEqual(r["hosts"], 1, "only the private machine is scanned")

    def test_extra_hosts(self):
        r = discover.scan(self.cfg(), {}, use_config_hosts=False, use_proxmox=False, extra="127.0.0.1")
        self.assertEqual({c["name"] for c in r["candidates"]}, {"Sonarr", "Radarr", "Switchboard"})
        self.assertTrue(all(c["source"] == "added by you" for c in r["candidates"]))

    def test_proxmox_guests_from_the_widget_token(self):
        pve = MockApp(apps.proxmox())
        try:
            cfg = self.cfg({"id": "pve", "name": "Proxmox", "url": pve.url,
                            "widget": {"type": "proxmox", "options": {"tokenId": "api@pam!test"}}})
            guests, notes = discover.proxmox_guests(cfg, {"pve": {"tokenSecret": apps.KEY}})
            self.assertEqual(guests, {"10.9.8.7": "Proxmox: ct-a", "10.9.8.8": "Proxmox: ct-b",
                                      "10.9.8.9": "Proxmox: vm-a"})
            self.assertEqual(notes, [])
            _, notes = discover.proxmox_guests(cfg, {"pve": {"tokenSecret": "wrong"}})
            self.assertIn("401", notes[0])
        finally:
            pve.close()


if __name__ == "__main__":
    unittest.main()
