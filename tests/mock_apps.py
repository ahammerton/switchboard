"""Mock versions of the apps Switchboard has widgets for.

Each factory returns a request handler for helpers.MockApp. The responses copy
the shape of each app's real API, and each checks authentication the way the
real app does, so wrong or missing credentials can be tested too.
"""
import base64
import datetime as dt
import json

KEY = "test-key"


def _basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _utc(days, hour=0):
    t = dt.datetime.now(dt.timezone.utc).replace(hour=hour, minute=0, second=0, microsecond=0)
    return (t + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


# ---------------------------------------------------------------- calendars

SONARR_CALENDAR = [
    {"airDateUtc": _utc(-1, 20), "seasonNumber": 1, "episodeNumber": 2, "title": "Second", "hasFile": True,
     "series": {"title": "Show A", "titleSlug": "show-a"}},
    {"airDateUtc": _utc(1, 21), "seasonNumber": 3, "episodeNumber": 4, "title": "TBA", "hasFile": False,
     "series": {"title": "Show B", "titleSlug": "show-b"}},
    {"airDateUtc": _utc(2, 19), "seasonNumber": 1, "episodeNumber": 1, "title": "Pilot", "hasFile": False,
     "series": {"title": "Show C", "titleSlug": "show-c"}},
]
RADARR_CALENDAR = [
    {"title": "Film A", "year": 2026, "titleSlug": "film-a", "hasFile": True, "digitalRelease": _utc(0)},
    {"title": "Film B", "year": 2026, "titleSlug": "film-b", "hasFile": False,
     "inCinemas": _utc(2), "digitalRelease": _utc(90)},  # digital release is outside the window
]
LIDARR_CALENDAR = [{"title": "Album A", "releaseDate": _utc(3), "artist": {"artistName": "Band A"},
                    "foreignAlbumId": "album-a", "statistics": {"trackFileCount": 0}}]
READARR_CALENDAR = [{"title": "Book A", "releaseDate": _utc(1), "author": {"authorName": "Author A"},
                     "titleSlug": "book-a", "statistics": {"bookFileCount": 1}}]
MAGARR_CALENDAR = [{"magazineTitle": "Magazine A", "title": "November", "issueNumber": "2026-11",
                    "publishDate": _date(2), "magazineId": 7, "file": None}]


# ---------------------------------------------------------------- *arr family

def arr(version, calendar, missing=True):
    """Sonarr/Radarr (v3), Lidarr/Readarr (v1). missing=False copies older Radarr (no wanted/missing)."""
    prefix = f"/api/{version}"

    def handle(r):
        if not r.path.startswith(prefix + "/"):
            return None
        if r.headers.get("X-Api-Key") != KEY:
            return 401, {"error": "Unauthorized"}
        tail = r.path[len(prefix):]
        if tail == "/queue":
            assert r.q("pageSize") == "1"
            return {"page": 1, "totalRecords": 3, "records": []}
        if tail == "/wanted/missing" and missing:
            return {"totalRecords": 12, "records": []}
        if tail == "/calendar":
            assert r.q("start") and r.q("end")
            return calendar
        return None
    return handle


def sonarr(**kw):
    return arr("v3", SONARR_CALENDAR, **kw)


def radarr(**kw):
    return arr("v3", RADARR_CALENDAR, **kw)


def lidarr():
    return arr("v1", LIDARR_CALENDAR)


def readarr():
    return arr("v1", READARR_CALENDAR)


def prowlarr():
    def handle(r):
        if r.headers.get("X-Api-Key") != KEY:
            return 401, {}
        return {
            "/api/v1/indexer": [{"enable": True}, {"enable": True}, {"enable": False}],
            "/api/v1/health": [{"type": "warning", "message": "indexer unavailable"}],
            "/api/v1/indexerstats": {"indexers": [{"numberOfQueries": 1200, "numberOfGrabs": 30},
                                                  {"numberOfQueries": 34, "numberOfGrabs": 2}]},
        }.get(r.path)
    return handle


def bazarr():
    def handle(r):
        if r.path != "/api/badges":
            return None
        if r.headers.get("X-API-KEY") != KEY:
            return 401, {}
        return {"episodes": 17, "movies": 4, "providers": 0}
    return handle


def seerr():
    def handle(r):
        if r.path != "/api/v1/request/count":
            return None
        if r.headers.get("X-Api-Key") != KEY:
            return 403, {}
        return {"total": 50, "pending": 2, "processing": 5, "available": 42}
    return handle


def magarr():
    def handle(r):
        if r.headers.get("X-Api-Key") != KEY:
            return 401, {"detail": "Unauthorized"}
        if r.path == "/api/v1/calendar":
            # Magarr takes whole dates, both ends inclusive.
            dt.date.fromisoformat(r.q("start"))
            dt.date.fromisoformat(r.q("end"))
            return MAGARR_CALENDAR
        return {
            "/api/v1/magazine": [{"id": 1}, {"id": 2}],
            "/api/v1/queue": [{}],
            "/api/v1/wanted": {"total": 4, "page": 1, "pageSize": 1, "records": []},
            "/api/v1/system/health": {"items": [{"level": "warning", "message": "no indexer"},
                                                {"level": "notice", "message": "automation paused"}]},
        }.get(r.path)
    return handle


# ---------------------------------------------------------------- media servers

def plex():
    def handle(r):
        if r.headers.get("X-Plex-Token") != KEY:
            return 401, "<html>401 Unauthorized</html>"
        if r.path == "/status/sessions":
            return {"MediaContainer": {"size": 2}}
        if r.path == "/library/sections":
            return {"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}, {"key": "2", "type": "show"},
                                                     {"key": "3", "type": "artist"}]}}
        if r.path.startswith("/library/sections/"):
            assert r.q("X-Plex-Container-Size") == "0"
            return {"MediaContainer": {"size": 0, "totalSize": {"1": 812, "2": 143}[r.path.split("/")[3]]}}
        return None
    return handle


def jellyfin(emby=False):
    prefix = "/emby" if emby else ""

    def handle(r):
        ok = (r.headers.get("X-Emby-Token") == KEY) if emby else \
            (r.headers.get("Authorization") == f'MediaBrowser Token="{KEY}"')
        if not ok:
            return 401, {}
        if r.path == prefix + "/Sessions":
            return [{"NowPlayingItem": {}}, {}, {"NowPlayingItem": {"Name": "x"}}]
        if r.path == prefix + "/Items/Counts":
            return {"MovieCount": 640, "SeriesCount": 88, "EpisodeCount": 4210}
        return None
    return handle


def tautulli():
    def handle(r):
        if r.path != "/api/v2" or r.q("cmd") != "get_activity":
            return None
        if r.q("apikey") != KEY:
            return {"response": {"result": "error", "message": "Invalid apikey"}}
        return {"response": {"result": "success", "data": {
            "stream_count": "3", "stream_count_transcode": 1, "total_bandwidth": 24000}}}
    return handle


# ---------------------------------------------------------------- downloads

def qbittorrent(password="qbpass"):
    state = {"logins": 0}

    def handle(r):
        if r.path == "/api/v2/auth/login" and r.method == "POST":
            state["logins"] += 1
            f = r.form()
            if f.get("username") == "admin" and f.get("password") == password:
                return 200, b"Ok.", {"Set-Cookie": "SID=qb-session; path=/"}
            return 200, b"Fails."
        if "SID=qb-session" not in (r.headers.get("Cookie") or ""):
            return 403, b"Forbidden"
        if r.path == "/api/v2/transfer/info":
            return {"dl_info_speed": 5242880, "up_info_speed": 102400}
        if r.path == "/api/v2/torrents/info":
            return [{}] * (2 if r.q("filter") == "downloading" else 14)
        return None
    handle.state = state
    return handle


def sabnzbd():
    def handle(r):
        if r.path != "/api" or r.q("mode") != "queue":
            return None
        if r.q("apikey") != KEY:
            return {"status": False, "error": "API Key Incorrect"}
        return {"queue": {"kbpersec": "2048.5", "noofslots_total": 4, "timeleft": "0:12:30"}}
    return handle


def nzbget():
    def handle(r):
        if r.headers.get("Authorization") != _basic("nzb", "nzbpass"):
            return 401, {}
        if r.path == "/jsonrpc/status":
            return {"result": {"DownloadRate": 1048576, "RemainingSizeMB": 2048}}
        if r.path == "/jsonrpc/listgroups":
            return {"result": [{}, {}]}
        return None
    return handle


# ---------------------------------------------------------------- infrastructure

def proxmox():
    def handle(r):
        if r.headers.get("Authorization") != f"PVEAPIToken=api@pam!test={KEY}":
            return 401, {"data": None}
        if r.path == "/api2/json/cluster/resources":
            data = [
                {"type": "node", "node": "pve1", "status": "online", "cpu": 0.10, "maxcpu": 8, "mem": 8e9, "maxmem": 32e9},
                {"type": "node", "node": "pve2", "status": "online", "cpu": 0.50, "maxcpu": 8, "mem": 24e9, "maxmem": 32e9},
                {"type": "qemu", "node": "pve1", "vmid": 100, "name": "vm-a", "status": "running", "template": 0},
                {"type": "qemu", "node": "pve2", "vmid": 101, "name": "vm-b", "status": "stopped", "template": 0},
                {"type": "qemu", "node": "pve1", "vmid": 900, "name": "tmpl", "status": "stopped", "template": 1},
                {"type": "lxc", "node": "pve1", "vmid": 200, "name": "ct-a", "status": "running"},
                {"type": "lxc", "node": "pve2", "vmid": 201, "name": "ct-b", "status": "running"},
                {"type": "storage", "node": "pve1", "status": "available"},
            ]
            if r.q("type") == "vm":
                data = [x for x in data if x["type"] in ("qemu", "lxc")]
            return {"data": data}
        if r.path == "/api2/json/nodes/pve1/lxc/200/interfaces":
            return {"data": [{"name": "lo", "inet": "127.0.0.1/8"}, {"name": "eth0", "inet": "10.9.8.7/24"}]}
        if r.path == "/api2/json/nodes/pve2/lxc/201/interfaces":
            return {"data": [{"name": "eth0", "inet": "10.9.8.8/24"}]}
        if r.path == "/api2/json/nodes/pve1/qemu/100/agent/network-get-interfaces":
            return {"data": {"result": [{"ip-addresses": [{"ip-address": "10.9.8.9", "ip-address-type": "ipv4"},
                                                          {"ip-address": "fe80::1", "ip-address-type": "ipv6"}]}]}}
        return None
    return handle


def portainer():
    def handle(r):
        if r.path != "/api/endpoints":
            return None
        if r.headers.get("X-API-Key") != KEY:
            return 401, {}
        return [{"Snapshots": [{"RunningContainerCount": 20, "StoppedContainerCount": 2,
                                "UnhealthyContainerCount": 1, "StackCount": 6}]},
                {"Snapshots": [{"RunningContainerCount": 5, "StoppedContainerCount": 0,
                                "UnhealthyContainerCount": 0, "StackCount": 1}]},
                {"Snapshots": []}]
    return handle


def pihole(password="pipass"):
    state = {"logins": 0, "sids": set()}

    def handle(r):
        if r.path == "/api/auth" and r.method == "POST":
            state["logins"] += 1
            if r.json().get("password") != password:
                return 401, {"session": {"valid": False}}
            sid = f"sid-{state['logins']}"
            state["sids"].add(sid)
            return {"session": {"valid": True, "sid": sid, "validity": 300}}
        if r.path == "/api/stats/summary":
            if r.headers.get("X-FTL-SID") not in state["sids"]:
                return 401, {"error": {"key": "unauthorized"}}
            return {"queries": {"total": 48211, "blocked": 7012, "percent_blocked": 14.54}, "clients": {"active": 23}}
        return None
    handle.state = state
    return handle


def pihole5():
    def handle(r):
        if r.path != "/admin/api.php":
            return None
        if r.q("auth") != KEY:
            return []  # v5 answers an empty list when the token is wrong
        return {"dns_queries_today": 30000, "ads_blocked_today": 3000, "ads_percentage_today": 10.0}
    return handle


def adguard():
    def handle(r):
        if r.path != "/control/stats":
            return None
        if r.headers.get("Authorization") != _basic("admin", "agpass"):
            return 401, {}
        return {"num_dns_queries": 20000, "num_blocked_filtering": 2500, "avg_processing_time": 0.0123}
    return handle


def technitium():
    def handle(r):
        if r.path != "/api/dashboard/stats/get":
            return None
        if r.q("token") != KEY:
            return {"status": "invalid-token", "errorMessage": "Invalid token"}
        return {"status": "ok", "response": {"stats": {
            "totalQueries": 52000, "totalBlocked": 6500, "totalClients": 31, "requestedRange": r.q("type")}}}
    return handle


def unifi(selfhosted=False, standalone=False):
    """API key → Integration API (UniFi OS prefix, or the root prefix when self-hosted).
    Local account → classic API (UniFi OS console, or a standalone controller)."""
    state = {"logins": 0}
    prefix = "/integration/v1" if selfhosted else "/proxy/network/integration/v1"
    health = {"data": [
        {"subsystem": "wlan", "num_user": 21}, {"subsystem": "lan", "num_user": 9},
        {"subsystem": "wan", "status": "ok"}, {"subsystem": "www", "status": "ok", "uptime": 1234567}]}

    def handle(r):
        if r.path.startswith(prefix):
            if r.headers.get("X-API-KEY") != KEY:
                return 401, {}
            rest = r.path[len(prefix):]
            if rest == "/sites":
                return {"totalCount": 2, "data": [{"id": "s-1", "name": "Default", "internalReference": "default"},
                                                  {"id": "s-2", "name": "Barn", "internalReference": "barn"}]}
            if rest == "/sites/s-1/devices":
                return {"totalCount": 5, "data": [{"state": "ONLINE"}] * 4 + [{"state": "OFFLINE"}]}
            if rest == "/sites/s-2/devices":
                return {"totalCount": 1, "data": [{"state": "ONLINE"}]}
            if rest.endswith("/clients"):
                return {"totalCount": 37 if "s-1" in rest else 4, "data": [{}]}
            return None
        cookie = r.headers.get("Cookie") or ""
        if not standalone:
            if r.path == "/api/auth/login" and r.method == "POST":
                state["logins"] += 1
                if r.json() != {"username": "ro", "password": "upass"}:
                    return 401, {}
                return 200, {}, {"Set-Cookie": "TOKEN=unifi-os; Path=/"}
            if r.path == "/proxy/network/api/s/default/stat/health":
                return health if "TOKEN=unifi-os" in cookie else (401, {})
        else:
            if r.path == "/api/login" and r.method == "POST":
                state["logins"] += 1
                if r.json() != {"username": "ro", "password": "upass"}:
                    return 400, {"meta": {"rc": "error"}}
                return 200, {"meta": {"rc": "ok"}}, {"Set-Cookie": "unifises=std; Path=/"}
            if r.path == "/api/s/default/stat/health":
                return health if "unifises=std" in cookie else (401, {})
        return None
    handle.state = state
    return handle


def gitea():
    def handle(r):
        if r.headers.get("Authorization") != f"token {KEY}":
            return 401, {"message": "unauthorized"}
        if r.path == "/api/v1/repos/search":
            return 200, {"ok": True, "data": []}, {"X-Total-Count": "9"}
        if r.path == "/api/v1/repos/issues/search":
            assert r.q("state") == "open"
            return 200, [], {"X-Total-Count": "3" if r.q("type") == "issues" else "1"}
        if r.path == "/api/v1/notifications/new":
            return {"new": 2}
        return None
    return handle


def dockge():
    """Socket.IO v4 over Engine.IO long-polling, as Dockge's web UI uses it."""
    sessions = {}

    def handle(r):
        if r.path != "/socket.io/":
            return None
        assert r.q("EIO") == "4" and r.q("transport") == "polling"
        sid = r.q("sid")
        if r.method == "GET" and sid is None:
            new = f"S{len(sessions) + 1}"
            sessions[new] = []
            return 200, b"0" + json.dumps({"sid": new, "pingInterval": 25000}).encode(), {"Content-Type": "text/plain"}
        queue = sessions.get(sid)
        if queue is None:
            return 400, {"message": "Session ID unknown"}
        if r.method == "POST":
            for pkt in r.body.decode().split("\x1e"):
                if pkt == "40":
                    queue += ['40{"sid":"ns"}', "2"]  # connected, then a ping to answer
                elif pkt.startswith("420"):
                    event, data = json.loads(pkt[3:])
                    assert event == "login"
                    if (data["username"], data["password"]) == ("admin", "dkpass"):
                        queue.append("42" + json.dumps(["agent", "stackList", {"ok": True, "stackList": {
                            "a": {"status": 3}, "b": {"status": 3}, "c": {"status": 4}, "d": {"status": 1}}}]))
                        queue.append('430[{"ok":true,"token":"jwt"}]')
                    elif data["username"] == "twofa":
                        queue.append('430[{"tokenRequired":true}]')
                    else:
                        queue.append('430[{"ok":false,"msg":"authIncorrectCreds"}]')
                elif pkt == "41":
                    sessions.pop(sid, None)
            return 200, b"ok", {"Content-Type": "text/plain"}
        out, queue[:] = list(queue), []
        return 200, ("\x1e".join(out) or "6").encode(), {"Content-Type": "text/plain"}
    return handle


# ---------------------------------------------------------------- home & books

def homeassistant():
    def handle(r):
        if r.headers.get("Authorization") != f"Bearer {KEY}":
            return 401, {}
        if r.path == "/api/config":
            return {"version": "2026.10.1"}
        if r.path == "/api/states":
            return [{}] * 312
        if r.path == "/api/states/sensor.lounge":
            return {"state": "21.4", "attributes": {"friendly_name": "Lounge", "unit_of_measurement": "°C"}}
        if r.path.startswith("/api/states/"):
            return 404, {"message": "Entity not found."}
        return None
    return handle


def immich():
    def handle(r):
        if r.path != "/api/server/statistics":
            return None
        if r.headers.get("x-api-key") != KEY:
            return 401, {}
        return {"photos": 15234, "videos": 812, "usage": 412 * 1024 ** 3}
    return handle


def mealie(v1=False):
    def handle(r):
        if r.headers.get("Authorization") != f"Bearer {KEY}":
            return 401, {"detail": "Not authenticated"}
        if r.path == ("/api/groups/statistics" if v1 else "/api/households/statistics"):
            return {"totalRecipes": 214, "totalUsers": 3, "totalCategories": 12, "totalTags": 40}
        return None
    return handle


def komga(post_list=False):
    """post_list=True copies newer Komga, where GET /series and /books lists are gone."""
    def handle(r):
        if r.headers.get("X-API-Key") != KEY and r.headers.get("Authorization") != _basic("komga", "kpass"):
            return 401, {}
        if r.path == "/api/v1/libraries":
            return [{"id": "1"}, {"id": "2"}]
        if r.path in ("/api/v1/series", "/api/v1/books") and r.method == "GET":
            if post_list:
                return 405, {"error": "Method Not Allowed"}
            if r.q("read_status") == "UNREAD":
                return {"totalElements": 210}
            return {"totalElements": 120 if r.path.endswith("series") else 3400}
        if r.path in ("/api/v1/series/list", "/api/v1/books/list") and r.method == "POST" and post_list:
            return {"totalElements": 120 if "series" in r.path else 3400}
        return None
    return handle


def bookorbit():
    state = {"logins": 0, "tokens": set()}

    def handle(r):
        if r.path == "/api/v1/auth/login" and r.method == "POST":
            state["logins"] += 1
            if r.json() != {"username": "reader", "password": "bopass"}:
                return 401, {"message": "Invalid credentials"}
            token = f"bo-{state['logins']}"
            state["tokens"].add(token)
            return {"accessToken": token, "user": {}}
        if r.path == "/api/v1/statistics/summary":
            if (r.headers.get("Authorization") or "")[7:] not in state["tokens"]:
                return 401, {"message": "Unauthorized"}
            return {"totalBooks": 1843, "totalAuthors": 611, "totalSeries": 97, "totalStorageBytes": 23 * 1024 ** 3}
        return None
    handle.state = state
    return handle


def custom():
    def handle(r):
        if r.path == "/custom/stats":
            if r.headers.get("Authorization") != f"Bearer {KEY}":
                return 401, {}
            return {"data": {"users": 7, "items": [1, 2, 3]}, "system": {"uptime": 93784, "disk": {"used_pct": 63.2}}}
        if r.path == "/html":
            return b"<html>login page</html>"
        return None
    return handle
