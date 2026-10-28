"""Every widget preset against a mock of its app: values, wrong and missing credentials,
and the special flows (sessions, re-login, version fallbacks)."""
import datetime as dt
import socket
import unittest

from tests import mock_apps as apps
from tests.helpers import DROP, MockApp

import widgets

K = apps.KEY


def values(result):
    """{label: value} from a widget result, failing clearly on an error."""
    if "error" in result:
        raise AssertionError(f"widget returned an error: {result['error']}")
    return {f["label"]: f["value"] for f in result["fields"]}


def warns(result):
    return {f["label"] for f in result["fields"] if f.get("warn")}


class WidgetTest(unittest.TestCase):
    def setUp(self):
        widgets._sessions.clear()  # Pi-hole SIDs and qBittorrent/UniFi cookies are cached per process
        self.mocks = []

    def tearDown(self):
        for m in self.mocks:
            m.close()

    def mock(self, handler):
        m = MockApp(handler)
        self.mocks.append(m)
        return m

    def run_widget(self, app, preset, options=None, secrets=None, path="/web", url=None):
        widget = {"type": preset, "options": options or {}}
        if url:
            widget["url"] = url
        return widgets.run(widget, app.url + path, secrets or {})

    def assertError(self, result, *fragments):
        self.assertIn("error", result, f"expected an error, got {result}")
        for frag in fragments:
            self.assertIn(frag, result["error"])


class ArrTests(WidgetTest):
    def test_sonarr(self):
        r = self.run_widget(self.mock(apps.sonarr()), "sonarr", secrets={"apiKey": K})
        self.assertEqual(values(r), {"Queue": 3, "Missing": 12, "Next 7d": len(apps.SONARR_CALENDAR)})

    def test_radarr_without_wanted_endpoint_skips_missing(self):
        r = self.run_widget(self.mock(apps.radarr(missing=False)), "radarr", secrets={"apiKey": K})
        self.assertEqual(set(values(r)), {"Queue", "Next 7d"})

    def test_lidarr_and_readarr_use_api_v1(self):
        self.assertEqual(values(self.run_widget(self.mock(apps.lidarr()), "lidarr", secrets={"apiKey": K}))["Queue"], 3)
        self.assertEqual(values(self.run_widget(self.mock(apps.readarr()), "readarr", secrets={"apiKey": K}))["Missing"], 12)

    def test_wrong_and_missing_key(self):
        app = self.mock(apps.sonarr())
        self.assertError(self.run_widget(app, "sonarr", secrets={"apiKey": "wrong"}), "HTTP 401")
        self.assertError(self.run_widget(app, "sonarr"), "API key is not set")

    def test_prowlarr(self):
        r = self.run_widget(self.mock(apps.prowlarr()), "prowlarr", secrets={"apiKey": K})
        self.assertEqual(values(r), {"Indexers": "2/3", "Queries": 1234, "Grabs": 32, "Issues": 1})
        self.assertEqual(warns(r), {"Issues"})

    def test_bazarr(self):
        r = self.run_widget(self.mock(apps.bazarr()), "bazarr", secrets={"apiKey": K})
        self.assertEqual(values(r), {"TV missing": 17, "Movies missing": 4})

    def test_seerr_flags_pending_requests(self):
        r = self.run_widget(self.mock(apps.seerr()), "seerr", secrets={"apiKey": K})
        self.assertEqual(values(r), {"Pending": 2, "Processing": 5, "Available": 42})
        self.assertEqual(warns(r), {"Pending"})

    def test_magarr_counts_only_warnings_and_errors_as_problems(self):
        r = self.run_widget(self.mock(apps.magarr()), "magarr", secrets={"apiKey": K})
        self.assertEqual(values(r), {"Magazines": 2, "Queue": 1, "Wanted": 4, "Problems": 1})


class MediaTests(WidgetTest):
    def test_plex_counts_movie_and_show_libraries(self):
        r = self.run_widget(self.mock(apps.plex()), "plex", secrets={"token": K})
        self.assertEqual(values(r), {"Streams": 2, "Movies": 812, "Shows": 143})

    def test_jellyfin_counts_sessions_that_are_playing(self):
        r = self.run_widget(self.mock(apps.jellyfin()), "jellyfin", secrets={"apiKey": K})
        self.assertEqual(values(r), {"Streams": 2, "Movies": 640, "Series": 88, "Episodes": 4210})

    def test_emby_uses_its_prefix_and_header(self):
        r = self.run_widget(self.mock(apps.jellyfin(emby=True)), "emby", secrets={"apiKey": K})
        self.assertEqual(values(r)["Streams"], 2)

    def test_tautulli(self):
        app = self.mock(apps.tautulli())
        r = self.run_widget(app, "tautulli", secrets={"apiKey": K})
        self.assertEqual(values(r), {"Streams": 3, "Transcodes": 1, "Bandwidth": 3_000_000})  # kbps → bytes/s
        self.assertError(self.run_widget(app, "tautulli", secrets={"apiKey": "bad"}), "Invalid apikey")


class DownloadTests(WidgetTest):
    def test_qbittorrent_logs_in_once_then_reuses_the_cookie(self):
        handler = apps.qbittorrent()
        app = self.mock(handler)
        for _ in range(2):
            r = self.run_widget(app, "qbittorrent", {"username": "admin"}, {"password": "qbpass"})
            self.assertEqual(values(r), {"Down": 5242880, "Up": 102400, "Active": 2, "Seeding": 14})
        self.assertEqual(handler.state["logins"], 1)

    def test_qbittorrent_bad_login(self):
        r = self.run_widget(self.mock(apps.qbittorrent()), "qbittorrent", {"username": "admin"}, {"password": "no"})
        self.assertError(r, "login failed")

    def test_sabnzbd(self):
        app = self.mock(apps.sabnzbd())
        r = self.run_widget(app, "sabnzbd", secrets={"apiKey": K}, path="/sabnzbd")
        self.assertEqual(values(r), {"Speed": 2048.5 * 1024, "Queue": 4, "Left": "0:12:30"})
        self.assertError(self.run_widget(app, "sabnzbd", secrets={"apiKey": "bad"}), "API Key Incorrect")

    def test_nzbget(self):
        r = self.run_widget(self.mock(apps.nzbget()), "nzbget", {"username": "nzb"}, {"password": "nzbpass"})
        self.assertEqual(values(r), {"Speed": 1048576, "Queue": 2, "Left": 2048 * 1024 * 1024})


class InfrastructureTests(WidgetTest):
    def test_proxmox_whole_cluster_ignores_templates(self):
        r = self.run_widget(self.mock(apps.proxmox()), "proxmox", {"tokenId": "api@pam!test"}, {"tokenSecret": K})
        self.assertEqual(values(r), {"VMs": "1/2", "LXC": "2/2", "CPU": 30.0, "RAM": 50.0})

    def test_proxmox_single_node(self):
        r = self.run_widget(self.mock(apps.proxmox()), "proxmox", {"tokenId": "api@pam!test", "node": "pve1"},
                            {"tokenSecret": K})
        self.assertEqual(values(r), {"VMs": "1/1", "LXC": "1/1", "CPU": 10.0, "RAM": 25.0})

    def test_portainer_sums_endpoints(self):
        r = self.run_widget(self.mock(apps.portainer()), "portainer", secrets={"apiKey": K})
        self.assertEqual(values(r), {"Running": 25, "Stopped": 2, "Unhealthy": 1, "Stacks": 7})
        self.assertEqual(warns(r), {"Unhealthy"})

    def test_pihole_logs_in_once_and_logs_in_again_when_the_session_expires(self):
        handler = apps.pihole()
        app = self.mock(handler)
        expected = {"Queries": 48211, "Blocked": 7012, "Blocked %": 14.54, "Clients": 23}
        self.assertEqual(values(self.run_widget(app, "pihole", secrets={"password": "pipass"})), expected)
        self.assertEqual(values(self.run_widget(app, "pihole", secrets={"password": "pipass"})), expected)
        self.assertEqual(handler.state["logins"], 1)
        handler.state["sids"].clear()  # Pi-hole forgot the session
        self.assertEqual(values(self.run_widget(app, "pihole", secrets={"password": "pipass"})), expected)
        self.assertEqual(handler.state["logins"], 2)

    def test_pihole_wrong_password(self):
        self.assertError(self.run_widget(self.mock(apps.pihole()), "pihole", secrets={"password": "no"}), "401")

    def test_pihole5(self):
        app = self.mock(apps.pihole5())
        self.assertEqual(values(self.run_widget(app, "pihole5", secrets={"apiKey": K}))["Blocked %"], 10.0)
        self.assertError(self.run_widget(app, "pihole5", secrets={"apiKey": "bad"}), "check the API token")

    def test_adguard(self):
        r = self.run_widget(self.mock(apps.adguard()), "adguard", {"username": "admin"}, {"password": "agpass"})
        v = values(r)
        self.assertEqual((v["Queries"], v["Blocked"], v["Blocked %"]), (20000, 2500, 12.5))
        self.assertAlmostEqual(v["Latency"], 12.3)

    def test_technitium_range_and_bad_token(self):
        app = self.mock(apps.technitium())
        r = self.run_widget(app, "technitium", {"range": "LastWeek"}, {"token": K})
        self.assertEqual(values(r), {"Queries": 52000, "Blocked": 6500, "Blocked %": 12.5, "Clients": 31})
        self.assertError(self.run_widget(app, "technitium", secrets={"token": "bad"}), "invalid or expired token")

    def test_unifi_api_key_on_a_console_and_self_hosted(self):
        for handler in (apps.unifi(), apps.unifi(selfhosted=True)):
            r = self.run_widget(self.mock(handler), "unifi", secrets={"apiKey": K})
            self.assertEqual(values(r), {"Clients": 37, "Devices": "4/5"})
            self.assertEqual(warns(r), {"Devices"})

    def test_unifi_site_by_name_and_unknown_site(self):
        app = self.mock(apps.unifi())
        self.assertEqual(values(self.run_widget(app, "unifi", {"site": "barn"}, {"apiKey": K})),
                         {"Clients": 4, "Devices": "1/1"})
        self.assertError(self.run_widget(app, "unifi", {"site": "nope"}, {"apiKey": K}), "not found")

    def test_unifi_local_account_on_a_console_and_a_standalone_controller(self):
        for handler in (apps.unifi(), apps.unifi(standalone=True)):
            app = self.mock(handler)
            for _ in range(2):
                r = self.run_widget(app, "unifi", {"username": "ro"}, {"password": "upass"})
                self.assertEqual(values(r), {"WAN": "OK", "LAN users": 9, "Wi-Fi users": 21, "Uptime": 1234567})
            self.assertEqual(handler.state["logins"], 1, "the session cookie should be reused")

    def test_unifi_bad_password(self):
        self.assertError(self.run_widget(self.mock(apps.unifi()), "unifi", {"username": "ro"}, {"password": "x"}),
                         "login failed")

    def test_gitea_counts_come_from_headers(self):
        r = self.run_widget(self.mock(apps.gitea()), "gitea", secrets={"token": K})
        self.assertEqual(values(r), {"Repos": 9, "Issues": 3, "PRs": 1, "Unread": 2})
        self.assertEqual(warns(r), {"Unread"})

    def test_dockge_socketio_login(self):
        app = self.mock(apps.dockge())
        r = self.run_widget(app, "dockge", {"username": "admin"}, {"password": "dkpass"})
        self.assertEqual(values(r), {"Running": 2, "Exited": 1, "Stacks": 4})
        self.assertError(self.run_widget(app, "dockge", {"username": "admin"}, {"password": "no"}), "login failed")
        self.assertError(self.run_widget(app, "dockge", {"username": "twofa"}, {"password": "x"}), "2FA")


class HomeAndBooksTests(WidgetTest):
    def test_home_assistant_summary_and_entities(self):
        app = self.mock(apps.homeassistant())
        self.assertEqual(values(self.run_widget(app, "homeassistant", secrets={"token": K})),
                         {"Version": "2026.10.1", "Entities": 312})
        r = self.run_widget(app, "homeassistant", {"entities": "sensor.lounge, sensor.gone"}, {"token": K})
        self.assertEqual(values(r), {"Lounge": "21.4 °C", "sensor.gone": "not found"})
        self.assertEqual(warns(r), {"sensor.gone"})

    def test_immich(self):
        r = self.run_widget(self.mock(apps.immich()), "immich", secrets={"apiKey": K})
        self.assertEqual(values(r), {"Photos": 15234, "Videos": 812, "Storage": 412 * 1024 ** 3})

    def test_mealie_v2_and_v1(self):
        for handler in (apps.mealie(), apps.mealie(v1=True)):
            r = self.run_widget(self.mock(handler), "mealie", secrets={"token": K})
            self.assertEqual(values(r), {"Recipes": 214, "Users": 3, "Categories": 12, "Tags": 40})

    def test_komga_api_key_basic_auth_and_post_list_fallback(self):
        full = {"Libraries": 2, "Series": 120, "Books": 3400, "Unread": 210}
        self.assertEqual(values(self.run_widget(self.mock(apps.komga()), "komga", secrets={"apiKey": K})), full)
        self.assertEqual(values(self.run_widget(self.mock(apps.komga()), "komga", {"username": "komga"},
                                                {"password": "kpass"})), full)
        newer = values(self.run_widget(self.mock(apps.komga(post_list=True)), "komga", secrets={"apiKey": K}))
        self.assertEqual(newer, {"Libraries": 2, "Series": 120, "Books": 3400})
        self.assertError(self.run_widget(self.mock(apps.komga()), "komga"), "API key")

    def test_bookorbit_reuses_its_token_and_logs_in_again_when_it_expires(self):
        handler = apps.bookorbit()
        app = self.mock(handler)
        expected = {"Books": 1843, "Authors": 611, "Series": 97, "Storage": 23 * 1024 ** 3}
        for _ in range(2):
            self.assertEqual(values(self.run_widget(app, "bookorbit", {"username": "reader"}, {"password": "bopass"})),
                             expected)
        self.assertEqual(handler.state["logins"], 1)
        handler.state["tokens"].clear()
        self.assertEqual(values(self.run_widget(app, "bookorbit", {"username": "reader"}, {"password": "bopass"})),
                         expected)
        self.assertEqual(handler.state["logins"], 2)

    def test_custom_json_paths_and_formats(self):
        r = self.run_widget(self.mock(apps.custom()), "custom", {
            "endpoint": "/custom/stats", "authType": "bearer",
            "fields": "Users = data.users\nItems = data.items.#\nUptime = system.uptime | duration\n"
                      "Disk = system.disk.used_pct | percent\nNope = a.b.c"}, {"secret": K})
        self.assertEqual(values(r), {"Users": 7, "Items": 3, "Uptime": 93784, "Disk": 63.2, "Nope": None})
        self.assertEqual({f["label"]: f["format"] for f in r["fields"]}["Uptime"], "duration")

    def test_custom_needs_fields_and_json(self):
        app = self.mock(apps.custom())
        self.assertError(self.run_widget(app, "custom", {"endpoint": "/custom/stats", "authType": "bearer"},
                                         {"secret": K}), "add at least one field")
        self.assertError(self.run_widget(app, "custom", {"endpoint": "/html", "fields": "X = y"}), "not JSON")


class EngineTests(WidgetTest):
    def test_api_base(self):
        self.assertEqual(widgets.api_base({}, "http://10.0.0.5:32400/web"), "http://10.0.0.5:32400")
        self.assertEqual(widgets.api_base({"url": "https://proxy.lan/sonarr"}, "http://x"), "https://proxy.lan/sonarr")
        with self.assertRaises(widgets.WidgetError):
            widgets.api_base({}, "not a url")

    def test_widget_url_overrides_the_service_url(self):
        app = self.mock(apps.bazarr())
        r = widgets.run({"type": "bazarr", "url": app.url}, "http://wrong.invalid/", {"apiKey": K})
        self.assertEqual(values(r)["TV missing"], 17)

    def test_unknown_type_and_unreachable_app(self):
        self.assertError(widgets.run({"type": "nope"}, "http://127.0.0.1/", {}), "unknown widget type")
        with socket.socket() as s:  # a port with nothing listening
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertError(widgets.run({"type": "sonarr"}, f"http://127.0.0.1:{port}", {"apiKey": K}), "can't connect")

    def test_a_dropped_connection_only_breaks_that_widget(self):
        app = self.mock(lambda r: DROP)
        self.assertError(self.run_widget(app, "sonarr", secrets={"apiKey": K}), "connection failed")

    def test_json_path(self):
        data = {"a": {"b": [{"c": 5}, {"c": 6}]}, "list": [1, 2, 3]}
        self.assertEqual(widgets.json_path(data, "a.b[1].c"), 6)
        self.assertEqual(widgets.json_path(data, "list.#"), 3)
        self.assertEqual(widgets.json_path(data, "a.b.0.c"), 5)
        self.assertIsNone(widgets.json_path(data, "a.missing.x"))

    def test_every_preset_has_a_fetch_and_a_catalog_entry(self):
        catalog = {p["id"]: p for p in widgets.preset_catalog()}
        self.assertEqual(set(catalog), set(widgets.PRESETS))
        for pid, p in widgets.PRESETS.items():
            self.assertTrue(callable(p["fetch"]), pid)
            self.assertNotIn("fetch", catalog[pid])
            self.assertTrue(p["name"] and p["group"], pid)


class CalendarTests(WidgetTest):
    def window(self):
        today = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return today - dt.timedelta(days=1), today + dt.timedelta(days=8)

    def calendar(self, preset, handler):
        app = self.mock(handler)
        r = widgets.calendar({"type": preset}, app.url, {"apiKey": K}, *self.window())
        if "error" in r:
            raise AssertionError(r["error"])
        return r["items"]

    def test_sonarr_items_and_tba_titles(self):
        items = self.calendar("sonarr", apps.sonarr())
        self.assertEqual([i["title"] for i in items], ["Show A", "Show B", "Show C"])
        self.assertEqual(items[0]["detail"], "S01E02 · Second")
        self.assertEqual(items[1]["detail"], "S03E04")  # "TBA" placeholder dropped
        self.assertTrue(items[0]["link"].endswith("/series/show-a"))
        self.assertFalse(items[0]["allDay"])

    def test_radarr_lists_only_release_dates_inside_the_window(self):
        items = self.calendar("radarr", apps.radarr())
        self.assertEqual([(i["title"], i["detail"]) for i in items],
                         [("Film A (2026)", "Digital release"), ("Film B (2026)", "In cinemas")])

    def test_lidarr_readarr_magarr(self):
        self.assertEqual(self.calendar("lidarr", apps.lidarr())[0]["title"], "Band A")
        book = self.calendar("readarr", apps.readarr())[0]
        self.assertEqual((book["title"], book["hasFile"]), ("Book A", True))
        issue = self.calendar("magarr", apps.magarr())[0]
        self.assertEqual((issue["title"], issue["detail"], issue["kind"]), ("Magazine A", "November", "magazine"))
        self.assertTrue(issue["link"].endswith("/magazine/7"))

    def test_not_a_calendar_app(self):
        self.assertIn("error", widgets.calendar({"type": "plex"}, "http://x", {}, *self.window()))


if __name__ == "__main__":
    unittest.main()
