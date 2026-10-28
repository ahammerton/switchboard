"""Service widgets: a few live stats from a self-hosted app's API.

Every preset is a function ``fetch(ctx) -> [field, ...]`` where a field is
``{"label": str, "value": number | str | None, "format": str}``. The front end
formats values by ``format``: number, bytes, speed (bytes/s), percent,
duration (seconds), ms or text.

Widgets run on the server, so API keys (kept in secrets.json) never reach
the browser and self-signed certificates / CORS are not a problem.
"""
import base64
import datetime
import hashlib
import http.client
import http.cookiejar
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 8
MAX_BYTES = 10 * 1024 * 1024

_ssl = ssl.create_default_context()
_ssl.check_hostname = False
_ssl.verify_mode = ssl.CERT_NONE
_default_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl))

# Login sessions (Pi-hole SIDs, qBittorrent cookies) reused between refreshes.
_sessions = {}
_sessions_lock = threading.Lock()


class WidgetError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def field(label, value, fmt="number", warn=False):
    f = {"label": label, "value": value, "format": fmt}
    if warn:
        f["warn"] = True
    return f


def _friendly_http_error(code):
    hints = {
        400: "bad request",
        401: "check the API key / credentials",
        403: "access denied: check the API key / permissions",
        404: "not found: check the URL and widget type",
    }
    return f"HTTP {code}" + (f" – {hints[code]}" if code in hints else "")


class Ctx:
    """What a preset gets: the API base URL, plain options and secrets."""

    def __init__(self, preset, base, options, secrets):
        self.preset = preset
        self.base = base.rstrip("/")
        self.opt = options or {}
        self.secret = secrets or {}

    def need(self, key, label):
        value = self.secret.get(key) or self.opt.get(key)
        if not value:
            raise WidgetError(f"{label} is not set")
        return value

    def session_key(self, *parts):
        digest = hashlib.sha256("\0".join(str(p) for p in parts).encode()).hexdigest()
        return (self.preset, self.base, digest)

    def fetch(self, path, *, params=None, headers=None, method="GET", json_body=None,
              form=None, data=None, opener=None, with_headers=False):
        url = path if re.match(r"^https?://", path) else self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        hdrs = {"User-Agent": "switchboard", "Accept": "application/json", **(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with (opener or _default_opener).open(req, timeout=TIMEOUT) as res:
                body = res.read(MAX_BYTES + 1)
                resp_headers = res.headers
        except urllib.error.HTTPError as e:
            raise WidgetError(_friendly_http_error(e.code), e.code) from None
        except urllib.error.URLError as e:
            raise WidgetError(f"can't connect: {e.reason}") from None
        except TimeoutError:
            raise WidgetError("timed out") from None
        except (http.client.HTTPException, OSError) as e:  # e.g. connection dropped mid-response
            raise WidgetError(f"connection failed: {type(e).__name__}") from None
        if len(body) > MAX_BYTES:
            raise WidgetError("response too large")
        return (body, resp_headers) if with_headers else body

    def json(self, path, optional=False, with_headers=False, **kwargs):
        """Parsed JSON. optional=True returns None when the endpoint doesn't exist (404/405)."""
        try:
            result = self.fetch(path, with_headers=with_headers, **kwargs)
        except WidgetError as e:
            if optional and e.status in (404, 405):
                return (None, None) if with_headers else None
            raise
        body, headers = result if with_headers else (result, None)
        try:
            data = json.loads(body)
        except ValueError:
            raise WidgetError("response was not JSON: check the URL and widget type") from None
        return (data, headers) if with_headers else data


def json_path(data, path):
    """Resolve "a.b[0].c". A "#" segment returns the length of a list or object."""
    cur = data
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path or ""):
        if part == "#":
            return len(cur) if isinstance(cur, (list, dict)) else None
        if part.startswith("["):
            idx = int(part[1:-1])
            cur = cur[idx] if isinstance(cur, list) and idx < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            cur = None
        if cur is None:
            return None
    return cur


def _num(v, default=0):
    try:
        return float(v) if "." in str(v) else int(v)
    except (TypeError, ValueError):
        return default


def _basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# ---------------------------------------------------------------- presets

def _arr(version):
    """Sonarr, Radarr (API v3), Lidarr, Readarr (API v1)."""
    def fetch(ctx):
        h = {"X-Api-Key": ctx.need("apiKey", "API key")}
        api = f"/api/{version}"
        queue = ctx.json(f"{api}/queue", params={"pageSize": 1}, headers=h)
        missing = ctx.json(f"{api}/wanted/missing", params={"pageSize": 1}, headers=h, optional=True)
        now = datetime.datetime.now(datetime.timezone.utc)
        upcoming = ctx.json(f"{api}/calendar", headers=h, optional=True, params={
            "start": now.isoformat(), "end": (now + datetime.timedelta(days=7)).isoformat()})
        fields = [field("Queue", queue.get("totalRecords", 0))]
        if missing is not None:
            fields.append(field("Missing", missing.get("totalRecords", 0)))
        if upcoming is not None:
            fields.append(field("Next 7d", len(upcoming)))
        return fields
    return fetch


def prowlarr(ctx):
    h = {"X-Api-Key": ctx.need("apiKey", "API key")}
    indexers = ctx.json("/api/v1/indexer", headers=h)
    health = ctx.json("/api/v1/health", headers=h, optional=True) or []
    stats = ctx.json("/api/v1/indexerstats", headers=h, optional=True) or {}
    enabled = sum(1 for i in indexers if i.get("enable"))
    per = stats.get("indexers", [])
    return [
        field("Indexers", f"{enabled}/{len(indexers)}", "text"),
        field("Queries", sum(i.get("numberOfQueries", 0) for i in per)),
        field("Grabs", sum(i.get("numberOfGrabs", 0) for i in per)),
        field("Issues", len(health), warn=bool(health)),
    ]


def bazarr(ctx):
    badges = ctx.json("/api/badges", headers={"X-API-KEY": ctx.need("apiKey", "API key")})
    return [
        field("TV missing", badges.get("episodes", 0)),
        field("Movies missing", badges.get("movies", 0)),
    ]


def seerr(ctx):
    c = ctx.json("/api/v1/request/count", headers={"X-Api-Key": ctx.need("apiKey", "API key")})
    return [
        field("Pending", c.get("pending", 0), warn=c.get("pending", 0) > 0),
        field("Processing", c.get("processing", 0)),
        field("Available", c.get("available", 0)),
    ]


def proxmox(ctx):
    token_id = ctx.need("tokenId", "API token ID")
    secret = ctx.need("tokenSecret", "API token secret")
    data = ctx.json("/api2/json/cluster/resources",
                    headers={"Authorization": f"PVEAPIToken={token_id}={secret}"}).get("data", [])
    node = (ctx.opt.get("node") or "").strip()
    if node:
        data = [r for r in data if r.get("node") == node]
    nodes = [r for r in data if r.get("type") == "node" and r.get("status") == "online"]
    guests = [r for r in data if r.get("type") in ("qemu", "lxc") and not r.get("template")]
    if not nodes and not guests:
        raise WidgetError("no data: give the token the PVEAuditor role on /")

    def running(kind):
        g = [x for x in guests if x["type"] == kind]
        return f"{sum(1 for x in g if x.get('status') == 'running')}/{len(g)}"

    cores = sum(n.get("maxcpu", 0) for n in nodes)
    cpu = sum(n.get("cpu", 0) * n.get("maxcpu", 0) for n in nodes) / cores * 100 if cores else None
    maxmem = sum(n.get("maxmem", 0) for n in nodes)
    mem = sum(n.get("mem", 0) for n in nodes) / maxmem * 100 if maxmem else None
    return [
        field("VMs", running("qemu"), "text"),
        field("LXC", running("lxc"), "text"),
        field("CPU", cpu, "percent", warn=(cpu or 0) > 90),
        field("RAM", mem, "percent", warn=(mem or 0) > 90),
    ]


def plex(ctx):
    h = {"X-Plex-Token": ctx.need("token", "Plex token")}
    streams = ctx.json("/status/sessions", headers=h).get("MediaContainer", {}).get("size", 0)
    sections = ctx.json("/library/sections", headers=h).get("MediaContainer", {}).get("Directory", [])
    counts = {"movie": 0, "show": 0}
    for s in sections:
        if s.get("type") in counts:
            mc = ctx.json(f"/library/sections/{s['key']}/all", headers=h, params={
                "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0}).get("MediaContainer", {})
            counts[s["type"]] += mc.get("totalSize", mc.get("size", 0))
    return [field("Streams", streams), field("Movies", counts["movie"]), field("Shows", counts["show"])]


def _jellyfin_like(prefix, header):
    """Jellyfin and Emby share most of their API."""
    def fetch(ctx):
        h = header(ctx.need("apiKey", "API key"))
        sessions = ctx.json(f"{prefix}/Sessions", headers=h, params={"activeWithinSeconds": 600})
        counts = ctx.json(f"{prefix}/Items/Counts", headers=h)
        return [
            field("Streams", sum(1 for s in sessions if "NowPlayingItem" in s)),
            field("Movies", counts.get("MovieCount", 0)),
            field("Series", counts.get("SeriesCount", 0)),
            field("Episodes", counts.get("EpisodeCount", 0)),
        ]
    return fetch


def tautulli(ctx):
    r = ctx.json("/api/v2", params={"apikey": ctx.need("apiKey", "API key"), "cmd": "get_activity"})
    r = r.get("response", {})
    if r.get("result") != "success":
        raise WidgetError(r.get("message") or "Tautulli API error")
    d = r.get("data", {})
    return [
        field("Streams", _num(d.get("stream_count"))),
        field("Transcodes", _num(d.get("stream_count_transcode"))),
        field("Bandwidth", _num(d.get("total_bandwidth")) * 1000 / 8, "speed"),  # kbps -> bytes/s
    ]


def pihole(ctx):
    """Pi-hole v6: optional password login, session ID reused until it expires."""
    password = ctx.secret.get("password", "")
    key = ctx.session_key(password)

    def login():
        r = ctx.json("/api/auth", method="POST", json_body={"password": password})
        sess = r.get("session", {})
        if not sess.get("valid"):
            raise WidgetError("Pi-hole rejected the password")
        with _sessions_lock:
            _sessions[key] = sess.get("sid")
        return sess.get("sid")

    def summary(sid):
        return ctx.json("/api/stats/summary", headers={"X-FTL-SID": sid} if sid else {})

    with _sessions_lock:
        sid = _sessions.get(key)
    if sid is None and password:
        sid = login()
    try:
        s = summary(sid)
    except WidgetError as e:
        if e.status != 401:
            raise
        s = summary(login())
    q = s.get("queries", {})
    return [
        field("Queries", q.get("total", 0)),
        field("Blocked", q.get("blocked", 0)),
        field("Blocked %", q.get("percent_blocked"), "percent"),
        field("Clients", s.get("clients", {}).get("active", 0)),
    ]


def pihole5(ctx):
    d = ctx.json("/admin/api.php", params={"summaryRaw": "", "auth": ctx.need("apiKey", "API token")})
    if not isinstance(d, dict) or "dns_queries_today" not in d:
        raise WidgetError("no data: check the API token")
    return [
        field("Queries", d.get("dns_queries_today", 0)),
        field("Blocked", d.get("ads_blocked_today", 0)),
        field("Blocked %", d.get("ads_percentage_today"), "percent"),
    ]


def adguard(ctx):
    h = {"Authorization": _basic(ctx.opt.get("username", ""), ctx.secret.get("password", ""))}
    s = ctx.json("/control/stats", headers=h)
    total, blocked = s.get("num_dns_queries", 0), s.get("num_blocked_filtering", 0)
    return [
        field("Queries", total),
        field("Blocked", blocked),
        field("Blocked %", blocked / total * 100 if total else 0, "percent"),
        field("Latency", s.get("avg_processing_time", 0) * 1000, "ms"),
    ]


def homeassistant(ctx):
    h = {"Authorization": f"Bearer {ctx.need('token', 'Access token')}"}
    entities = [e.strip() for e in (ctx.opt.get("entities") or "").split(",") if e.strip()][:4]
    if not entities:
        cfg = ctx.json("/api/config", headers=h)
        states = ctx.json("/api/states", headers=h)
        return [field("Version", cfg.get("version"), "text"), field("Entities", len(states))]
    fields = []
    for entity in entities:
        try:
            s = ctx.json(f"/api/states/{urllib.parse.quote(entity)}", headers=h)
        except WidgetError as e:
            if e.status != 404:
                raise
            fields.append(field(entity, "not found", "text", warn=True))
            continue
        attrs = s.get("attributes", {})
        unit = attrs.get("unit_of_measurement")
        fields.append(field(attrs.get("friendly_name", entity),
                            f"{s.get('state')}{' ' + unit if unit else ''}", "text"))
    return fields


def portainer(ctx):
    endpoints = ctx.json("/api/endpoints", headers={"X-API-Key": ctx.need("apiKey", "Access token")})
    totals = {"RunningContainerCount": 0, "StoppedContainerCount": 0,
              "UnhealthyContainerCount": 0, "StackCount": 0}
    for ep in endpoints:
        snaps = ep.get("Snapshots") or []
        if snaps:
            for k in totals:
                totals[k] += snaps[-1].get(k, 0) or 0
    unhealthy = totals["UnhealthyContainerCount"]
    return [
        field("Running", totals["RunningContainerCount"]),
        field("Stopped", totals["StoppedContainerCount"]),
        field("Unhealthy", unhealthy, warn=unhealthy > 0),
        field("Stacks", totals["StackCount"]),
    ]


def qbittorrent(ctx):
    """Cookie login (skipped when the WebUI allows unauthenticated local access)."""
    user, password = ctx.opt.get("username", ""), ctx.secret.get("password", "")
    key = ctx.session_key(user, password)
    with _sessions_lock:
        opener = _sessions.get(key)
        if opener is None:
            opener = _sessions[key] = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=_ssl),
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def stats():
        info = ctx.json("/api/v2/transfer/info", opener=opener)
        down = ctx.json("/api/v2/torrents/info", params={"filter": "downloading"}, opener=opener)
        seed = ctx.json("/api/v2/torrents/info", params={"filter": "seeding"}, opener=opener)
        return [
            field("Down", info.get("dl_info_speed", 0), "speed"),
            field("Up", info.get("up_info_speed", 0), "speed"),
            field("Active", len(down)),
            field("Seeding", len(seed)),
        ]

    try:
        return stats()
    except WidgetError as e:
        if e.status not in (401, 403):
            raise
    body = ctx.fetch("/api/v2/auth/login", method="POST", opener=opener,
                     form={"username": user, "password": password},
                     headers={"Referer": ctx.base, "Origin": ctx.base})
    if not body.strip().startswith(b"Ok"):
        raise WidgetError("qBittorrent login failed: check username / password")
    return stats()


def sabnzbd(ctx):
    d = ctx.json("/api", params={"mode": "queue", "output": "json", "apikey": ctx.need("apiKey", "API key")})
    if d.get("status") is False or "error" in d:
        raise WidgetError(d.get("error") or "SABnzbd API error")
    q = d.get("queue", {})
    left = q.get("timeleft") or ""
    return [
        field("Speed", _num(q.get("kbpersec")) * 1024, "speed"),
        field("Queue", _num(q.get("noofslots_total", q.get("noofslots", 0)))),
        field("Left", left if left not in ("", "0:00:00") else "—", "text"),
    ]


def nzbget(ctx):
    h = {"Authorization": _basic(ctx.opt.get("username", ""), ctx.secret.get("password", ""))}
    status = ctx.json("/jsonrpc/status", headers=h).get("result", {})
    groups = ctx.json("/jsonrpc/listgroups", headers=h).get("result", [])
    return [
        field("Speed", status.get("DownloadRate", 0), "speed"),
        field("Queue", len(groups)),
        field("Left", status.get("RemainingSizeMB", 0) * 1024 * 1024, "bytes"),
    ]


def immich(ctx):
    s = ctx.json("/api/server/statistics", headers={"x-api-key": ctx.need("apiKey", "API key")})
    return [
        field("Photos", s.get("photos", 0)),
        field("Videos", s.get("videos", 0)),
        field("Storage", s.get("usage", 0), "bytes"),
    ]


def magarr(ctx):
    """Magarr (magazine PVR): a Sonarr-style API with an X-Api-Key header."""
    h = {"X-Api-Key": ctx.need("apiKey", "API key")}
    magazines = ctx.json("/api/v1/magazine", headers=h)
    queue = ctx.json("/api/v1/queue", headers=h)
    wanted = ctx.json("/api/v1/wanted", headers=h, params={"pageSize": 1})
    health = ctx.json("/api/v1/system/health", headers=h, optional=True) or {}
    problems = [i for i in health.get("items", []) if i.get("level") in ("error", "warning")]
    return [
        field("Magazines", len(magazines)),
        field("Queue", len(queue)),
        field("Wanted", wanted.get("total", 0)),
        field("Problems", len(problems), warn=bool(problems)),
    ]


def mealie(ctx):
    h = {"Authorization": f"Bearer {ctx.need('token', 'API token')}"}
    # Mealie v2 keeps stats per household; v1 used groups.
    s = ctx.json("/api/households/statistics", headers=h, optional=True)
    if s is None:
        s = ctx.json("/api/groups/statistics", headers=h)
    return [
        field("Recipes", s.get("totalRecipes", 0)),
        field("Users", s.get("totalUsers", 0)),
        field("Categories", s.get("totalCategories", 0)),
        field("Tags", s.get("totalTags", 0)),
    ]


def komga(ctx):
    key = ctx.secret.get("apiKey")
    user = ctx.opt.get("username", "")
    if key:
        h = {"X-API-Key": key}  # Komga 1.20+
    elif user:
        h = {"Authorization": _basic(user, ctx.secret.get("password", ""))}
    else:
        raise WidgetError("set an API key, or a username and password")

    def total(kind, **params):
        r = ctx.json(f"/api/v1/{kind}", headers=h, optional=True, params={"size": 1, **params})
        if r is None:  # newer Komga: search via POST /list
            r = ctx.json(f"/api/v1/{kind}/list", method="POST", headers=h, params={"size": 1}, json_body={})
        return r.get("totalElements", 0)

    libraries = ctx.json("/api/v1/libraries", headers=h)
    fields = [
        field("Libraries", len(libraries)),
        field("Series", total("series")),
        field("Books", total("books")),
    ]
    unread = ctx.json("/api/v1/books", headers=h, optional=True, params={"size": 1, "read_status": "UNREAD"})
    if unread is not None:
        fields.append(field("Unread", unread.get("totalElements", 0)))
    return fields


def bookorbit(ctx):
    """BookOrbit has no API keys: log in once and reuse the access token."""
    user = ctx.need("username", "Username")
    password = ctx.need("password", "Password")
    key = ctx.session_key(user, password)

    def login():
        r = ctx.json("/api/v1/auth/login", method="POST", json_body={"username": user, "password": password})
        token = r.get("accessToken")
        if not token:
            raise WidgetError("BookOrbit login failed: check username / password")
        with _sessions_lock:
            _sessions[key] = token
        return token

    def summary(token):
        return ctx.json("/api/v1/statistics/summary", headers={"Authorization": f"Bearer {token}"})

    with _sessions_lock:
        token = _sessions.get(key)
    try:
        s = summary(token or login())
    except WidgetError as e:
        if e.status != 401 or token is None:
            raise
        s = summary(login())  # cached token expired
    return [
        field("Books", s.get("totalBooks", 0)),
        field("Authors", s.get("totalAuthors", 0)),
        field("Series", s.get("totalSeries", 0)),
        field("Storage", s.get("totalStorageBytes", 0), "bytes"),
    ]


def unifi(ctx):
    """API key → official Integration API; otherwise a local account → classic API."""
    site_opt = (ctx.opt.get("site") or "").strip()
    key = ctx.secret.get("apiKey")
    if key:
        h = {"X-API-KEY": key}
        base = None
        for prefix in ("/proxy/network/integration/v1", "/integration/v1"):  # UniFi OS console / self-hosted
            sites = ctx.json(f"{prefix}/sites", headers=h, optional=True)
            if sites is not None:
                base = prefix
                break
        if base is None:
            raise WidgetError("Integration API not found: needs UniFi Network 9.0 or newer")
        sites = sites.get("data", [])
        site = next((x for x in sites if site_opt in (x.get("name"), x.get("internalReference"))), None) \
            if site_opt else (sites[0] if sites else None)
        if not site:
            raise WidgetError(f"site “{site_opt}” not found" if site_opt else "no sites found")
        devices = ctx.json(f"{base}/sites/{site['id']}/devices", headers=h, params={"limit": 200})
        clients = ctx.json(f"{base}/sites/{site['id']}/clients", headers=h, params={"limit": 1})
        dev = devices.get("data", [])
        online = sum(1 for d in dev if d.get("state") == "ONLINE")
        total = devices.get("totalCount", len(dev))
        return [
            field("Clients", clients.get("totalCount", 0)),
            field("Devices", f"{online}/{total}", "text", warn=online < total),
        ]

    user, password = ctx.need("username", "Username or API key"), ctx.secret.get("password", "")
    sess_key = ctx.session_key(user, password)
    with _sessions_lock:
        opener = _sessions.get(sess_key)
        if opener is None:
            opener = _sessions[sess_key] = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=_ssl),
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    site = site_opt or "default"
    creds = {"username": user, "password": password}

    def health():
        # UniFi OS consoles proxy the Network app; standalone controllers serve it at the root.
        for login_path, api in (("/api/auth/login", "/proxy/network/api"), ("/api/login", "/api")):
            try:
                return ctx.json(f"{api}/s/{site}/stat/health", opener=opener)
            except WidgetError as e:
                if e.status not in (401, 403, 404):
                    raise
            try:
                ctx.fetch(login_path, method="POST", json_body=creds, opener=opener)
            except WidgetError as e:
                if e.status == 404:
                    continue  # not this kind of controller
                if e.status in (400, 401, 403):
                    raise WidgetError("UniFi login failed: check username / password (use a local account)") from None
                raise
            return ctx.json(f"{api}/s/{site}/stat/health", opener=opener)
        raise WidgetError("UniFi Network API not found at this address")

    sub = {x.get("subsystem"): x for x in health().get("data", [])}
    wan, www = sub.get("wan", {}), sub.get("www", {})
    status = wan.get("status") or www.get("status") or "unknown"
    return [
        field("WAN", status.upper() if status == "ok" else status, "text", warn=status != "ok"),
        field("LAN users", sub.get("lan", {}).get("num_user", 0)),
        field("Wi-Fi users", sub.get("wlan", {}).get("num_user", 0)),
        field("Uptime", www.get("uptime") or wan.get("gw_system-stats", {}).get("uptime"), "duration"),
    ]


def technitium(ctx):
    token = ctx.need("token", "API token")
    r = ctx.json("/api/dashboard/stats/get", headers={"Authorization": f"Bearer {token}"},
                 params={"token": token, "type": ctx.opt.get("range") or "LastDay", "utc": "true"})
    status = r.get("status")
    if status == "invalid-token":
        raise WidgetError("invalid or expired token: create an API token under Administration → Sessions")
    if status != "ok":
        raise WidgetError(r.get("errorMessage") or f"Technitium API status “{status}”")
    st = r.get("response", {}).get("stats", {})
    total, blocked = st.get("totalQueries", 0), st.get("totalBlocked", 0)
    return [
        field("Queries", total),
        field("Blocked", blocked),
        field("Blocked %", blocked / total * 100 if total else 0, "percent"),
        field("Clients", st.get("totalClients", 0)),
    ]


def gitea(ctx):
    """Gitea (and Forgejo): counts come from the X-Total-Count header."""
    h = {"Authorization": f"token {ctx.need('token', 'Access token')}"}

    def count(path, **params):
        _, headers = ctx.json(path, headers=h, with_headers=True, params={"limit": 1, **params})
        return _num(headers.get("X-Total-Count"))

    new = ctx.json("/api/v1/notifications/new", headers=h, optional=True) or {}
    return [
        field("Repos", count("/api/v1/repos/search")),
        field("Issues", count("/api/v1/repos/issues/search", state="open", type="issues")),
        field("PRs", count("/api/v1/repos/issues/search", state="open", type="pulls")),
        field("Unread", new.get("new", 0), warn=new.get("new", 0) > 0),
    ]


class _SocketIO:
    """Just enough of Socket.IO v4 over Engine.IO HTTP long-polling (stdlib only)."""

    def __init__(self, ctx):
        self.ctx, self.sid = ctx, None

    def _params(self):
        p = {"EIO": 4, "transport": "polling", "t": f"{time.time():.6f}"}
        if self.sid:
            p["sid"] = self.sid
        return p

    def receive(self):
        body = self.ctx.fetch("/socket.io/", params=self._params(), headers={"Accept": "*/*"})
        return body.decode("utf-8", "replace").split("\x1e")  # packets are record-separated

    def send(self, *packets):
        self.ctx.fetch("/socket.io/", method="POST", params=self._params(),
                       data="\x1e".join(packets).encode(),
                       headers={"Content-Type": "text/plain;charset=UTF-8", "Accept": "*/*"})

    def open(self):
        first = self.receive()[0]
        if not first.startswith("0{"):
            raise WidgetError("not a Socket.IO server: check the URL")
        self.sid = json.loads(first[1:])["sid"]
        self.send("40")  # connect to the default namespace


DOCKGE_RUNNING, DOCKGE_EXITED = 3, 4  # from Dockge's common/util-common.ts


def dockge(ctx):
    """Dockge has no REST API: log in over Socket.IO and read the pushed stack list."""
    user, password = ctx.need("username", "Username"), ctx.need("password", "Password")
    sio = _SocketIO(ctx)
    sio.open()
    sio.send("420" + json.dumps(["login", {"username": user, "password": password, "token": ""}]))
    stacks, logged_in = None, False
    deadline = time.monotonic() + TIMEOUT
    try:
        while stacks is None and time.monotonic() < deadline:
            for pkt in sio.receive():
                if pkt == "2":
                    sio.send("3")  # pong
                elif pkt.startswith("430"):  # ack for our login (id 0)
                    res = (json.loads(pkt[3:]) or [{}])[0]
                    if res.get("tokenRequired"):
                        raise WidgetError("this account uses 2FA: use an account without it")
                    if not res.get("ok"):
                        msg = res.get("msg") or ""
                        raise WidgetError("Dockge login failed: check username / password"
                                          if msg.startswith("auth") or not msg else f"Dockge: {msg}")
                    logged_in = True
                elif pkt.startswith("42"):
                    event = json.loads(pkt[2:])
                    if event[:2] == ["agent", "stackList"] and isinstance(event[2], dict) and event[2].get("ok"):
                        stacks = event[2].get("stackList", {})
                elif pkt.startswith("44"):
                    raise WidgetError("Dockge refused the connection")
                elif pkt == "1":
                    raise WidgetError("Dockge closed the connection")
    finally:
        try:
            sio.send("41")  # disconnect
        except WidgetError:
            pass
    if stacks is None:
        raise WidgetError("logged in, but no stack list arrived" if logged_in else "Dockge login timed out")
    statuses = [st.get("status") for st in stacks.values()]
    exited = statuses.count(DOCKGE_EXITED)
    return [
        field("Running", statuses.count(DOCKGE_RUNNING)),
        field("Exited", exited, warn=exited > 0),
        field("Stacks", len(statuses)),
    ]


CUSTOM_LINE = re.compile(r"^\s*([^=]+?)\s*=\s*([^|]+?)\s*(?:\|\s*(\w+))?\s*$")


def custom(ctx):
    """Any JSON API: one "Label = path | format" per line."""
    endpoint = (ctx.opt.get("endpoint") or "").strip()
    auth, secret = ctx.opt.get("authType", "none"), ctx.secret.get("secret", "")
    name = (ctx.opt.get("authName") or "").strip()
    headers, params = {}, {}
    if auth == "header":
        headers[name or "X-Api-Key"] = secret
    elif auth == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    elif auth == "basic":
        headers["Authorization"] = _basic(ctx.opt.get("username", ""), secret)
    elif auth == "query":
        params[name or "apikey"] = secret
    data = ctx.json(endpoint or "/", params=params, headers=headers)
    fields = []
    for line in (ctx.opt.get("fields") or "").splitlines():
        m = CUSTOM_LINE.match(line)
        if m:
            fields.append(field(m[1], json_path(data, m[2]), (m[3] or "auto").lower()))
    if not fields:
        raise WidgetError('add at least one field, e.g. "Users = data.users | number"')
    return fields[:6]


# ---------------------------------------------------------------- registry

API_KEY = {"key": "apiKey", "label": "API key", "type": "secret", "required": True}


def _opt(key, label, type_="text", **extra):
    return {"key": key, "label": label, "type": type_, **extra}


PRESETS = {
    # Media management
    "sonarr": {"name": "Sonarr", "group": "Media management", "fetch": _arr("v3"), "options": [API_KEY],
               "help": "Sonarr → Settings → General → Security → API Key"},
    "radarr": {"name": "Radarr", "group": "Media management", "fetch": _arr("v3"), "options": [API_KEY],
               "help": "Radarr → Settings → General → Security → API Key"},
    "lidarr": {"name": "Lidarr", "group": "Media management", "fetch": _arr("v1"), "options": [API_KEY],
               "help": "Lidarr → Settings → General → Security → API Key"},
    "readarr": {"name": "Readarr", "group": "Media management", "fetch": _arr("v1"), "options": [API_KEY],
                "help": "Readarr → Settings → General → Security → API Key"},
    "prowlarr": {"name": "Prowlarr", "group": "Media management", "fetch": prowlarr, "options": [API_KEY],
                 "help": "Prowlarr → Settings → General → Security → API Key"},
    "bazarr": {"name": "Bazarr", "group": "Media management", "fetch": bazarr, "options": [API_KEY],
               "help": "Bazarr → Settings → General → Security → API Key"},
    "seerr": {"name": "Seerr / Overseerr / Jellyseerr", "group": "Media management", "fetch": seerr,
              "options": [API_KEY], "help": "Settings → General → API Key"},
    # Media servers
    "plex": {"name": "Plex", "group": "Media servers", "fetch": plex,
             "options": [_opt("token", "Plex token", "secret", required=True)],
             "help": "In Plex Web, open any item → ⋯ → Get Info → View XML, and copy X-Plex-Token from the address bar"},
    "jellyfin": {"name": "Jellyfin", "group": "Media servers", "options": [API_KEY],
                 "fetch": _jellyfin_like("", lambda k: {"Authorization": f'MediaBrowser Token="{k}"'}),
                 "help": "Jellyfin → Dashboard → API Keys → +"},
    "emby": {"name": "Emby", "group": "Media servers", "options": [API_KEY],
             "fetch": _jellyfin_like("/emby", lambda k: {"X-Emby-Token": k}),
             "help": "Emby → Settings → Advanced → API Keys"},
    "tautulli": {"name": "Tautulli", "group": "Media servers", "fetch": tautulli, "options": [API_KEY],
                 "help": "Tautulli → Settings → Web Interface → API Key"},
    # Downloads
    "qbittorrent": {"name": "qBittorrent", "group": "Downloads", "fetch": qbittorrent,
                    "options": [_opt("username", "Username"), _opt("password", "Password", "secret")],
                    "help": "Your WebUI login. Leave blank if the WebUI skips auth for local clients"},
    "sabnzbd": {"name": "SABnzbd", "group": "Downloads", "fetch": sabnzbd, "options": [API_KEY],
                "help": "SABnzbd → Config → General → Security → API Key"},
    "nzbget": {"name": "NZBGet", "group": "Downloads", "fetch": nzbget,
               "options": [_opt("username", "Username"), _opt("password", "Password", "secret")],
               "help": "NZBGet → Settings → Security → ControlUsername / ControlPassword"},
    # Infrastructure
    "proxmox": {"name": "Proxmox VE", "group": "Infrastructure", "fetch": proxmox, "options": [
        _opt("tokenId", "API token ID", required=True, placeholder="root@pam!homepage"),
        _opt("tokenSecret", "API token secret", "secret", required=True),
        _opt("node", "Node (optional)", placeholder="Leave blank for the whole cluster")],
        "help": "Datacenter → Permissions → API Tokens → Add, then give the token the PVEAuditor role on /"},
    "portainer": {"name": "Portainer", "group": "Infrastructure", "fetch": portainer,
                  "options": [_opt("apiKey", "Access token", "secret", required=True)],
                  "help": "Portainer → My account → Access tokens → Add access token"},
    "pihole": {"name": "Pi-hole (v6+)", "group": "Infrastructure", "fetch": pihole,
               "options": [_opt("password", "App password", "secret")],
               "help": "Settings → Web interface / API → Configure app password. Leave blank if Pi-hole has no password"},
    "pihole5": {"name": "Pi-hole (v5)", "group": "Infrastructure", "fetch": pihole5,
                "options": [_opt("apiKey", "API token", "secret", required=True)],
                "help": "Settings → API → Show API token"},
    "adguard": {"name": "AdGuard Home", "group": "Infrastructure", "fetch": adguard,
                "options": [_opt("username", "Username"), _opt("password", "Password", "secret")],
                "help": "Your AdGuard Home web login"},
    "unifi": {"name": "UniFi Network", "group": "Infrastructure", "fetch": unifi, "options": [
        _opt("apiKey", "API key (recommended)", "secret",
             hint="UniFi Network 9.0+: Settings → Control Plane → Integrations → Create API Key. "
                  "Or leave blank and use a local account below."),
        _opt("username", "Username (local account)"),
        _opt("password", "Password", "secret"),
        _opt("site", "Site (optional)", placeholder="default")],
        "help": "Use the console's address, e.g. https://192.168.1.1. An API key gives devices online and client count; "
                "a local read-only account gives WAN status, users and uptime"},
    "technitium": {"name": "Technitium DNS", "group": "Infrastructure", "fetch": technitium, "options": [
        _opt("token", "API token", "secret", required=True),
        _opt("range", "Period", "select", options=[
            ["LastHour", "Last hour"], ["LastDay", "Last day"], ["LastWeek", "Last week"], ["LastMonth", "Last month"]])],
        "help": "Technitium → Administration → Sessions → Create Token (ideally for a dedicated read-only user)"},
    "dockge": {"name": "Dockge", "group": "Infrastructure", "fetch": dockge, "options": [
        _opt("username", "Username", required=True), _opt("password", "Password", "secret", required=True)],
        "help": "Your Dockge login (Dockge has no API keys). Accounts with 2FA aren't supported"},
    # Development
    "gitea": {"name": "Gitea / Forgejo", "group": "Development", "fetch": gitea,
              "options": [_opt("token", "Access token", "secret", required=True)],
              "help": "Settings → Applications → Generate New Token, with read access to repository, issue and notification"},
    # Books & comics
    "komga": {"name": "Komga", "group": "Books & comics", "fetch": komga, "options": [
        _opt("apiKey", "API key", "secret", hint="Komga 1.20+: Account settings → API Keys. Or use a username and password instead."),
        _opt("username", "Username (if no API key)"),
        _opt("password", "Password (if no API key)", "secret")],
        "help": "An API key is recommended"},
    "magarr": {"name": "Magarr", "group": "Books & comics", "fetch": magarr, "options": [API_KEY],
               "help": "Magarr's API key is in its config file: /config/config.yaml → auth → api_key"},
    "bookorbit": {"name": "BookOrbit", "group": "Books & comics", "fetch": bookorbit, "options": [
        _opt("username", "Username", required=True), _opt("password", "Password", "secret", required=True)],
        "help": "Your BookOrbit login (BookOrbit has no API keys), ideally a dedicated non-admin user"},
    # Home & lifestyle
    "mealie": {"name": "Mealie", "group": "Home & lifestyle", "fetch": mealie,
               "options": [_opt("token", "API token", "secret", required=True)],
               "help": "Mealie → your profile → API Tokens → Generate"},
    "homeassistant": {"name": "Home Assistant", "group": "Home & lifestyle", "fetch": homeassistant, "options": [
        _opt("token", "Long-lived access token", "secret", required=True),
        _opt("entities", "Entities (optional)", placeholder="sensor.living_room_temperature, light.kitchen",
             hint="Up to 4 entity IDs, comma-separated. Leave blank to show version and entity count.")],
        "help": "Home Assistant → your profile → Security → Long-lived access tokens"},
    "immich": {"name": "Immich", "group": "Home & lifestyle", "fetch": immich, "options": [API_KEY],
               "help": "Immich → Account settings → API keys (the key must belong to an admin)"},
    # Anything else
    "custom": {"name": "Custom JSON API", "group": "Other", "fetch": custom, "options": [
        _opt("endpoint", "API path or URL", required=True, placeholder="/api/status  or  http://host:port/api/status"),
        _opt("authType", "Authentication", "select", options=[
            ["none", "None"], ["header", "Header"], ["bearer", "Bearer token"],
            ["basic", "Basic (username + password)"], ["query", "Query parameter"]]),
        _opt("authName", "Header / parameter name", placeholder="X-Api-Key or apikey"),
        _opt("username", "Username (basic auth)"),
        _opt("secret", "API key / token / password", "secret"),
        _opt("fields", "Fields", "textarea", required=True, placeholder="Users = data.users\nUptime = system.uptime | duration",
             hint='One per line: "Label = json.path | format". Formats: number, bytes, speed, percent, '
                  'duration, ms, text. Use [0] for list items and # for a count, e.g. "Items = items.#".')],
        "help": "Shows values from any JSON API"},
}


def preset_catalog():
    """Preset definitions for the edit dialog (without the fetch functions)."""
    return [{"id": pid, **{k: v for k, v in p.items() if k != "fetch"}} for pid, p in PRESETS.items()]


def secret_keys(preset_id):
    p = PRESETS.get(preset_id, {})
    return {o["key"] for o in p.get("options", []) if o.get("type") == "secret"}


def api_base(widget, service_url):
    """The app's API root: widget.url if set, else the service URL's scheme://host:port."""
    base = (widget.get("url") or "").strip()
    if base:
        return base
    u = urllib.parse.urlsplit(service_url or "")
    if u.scheme not in ("http", "https") or not u.netloc:
        raise WidgetError("the service needs an http(s) URL")
    return f"{u.scheme}://{u.netloc}"  # apps behind a sub-path set widget.url instead


def run(widget, service_url, secrets):
    """Fetch one widget. Returns {"fields": [...]} or {"error": "..."}."""
    preset_id = widget.get("type")
    preset = PRESETS.get(preset_id)
    if not preset:
        return {"error": f"unknown widget type “{preset_id}”"}
    try:
        base = api_base(widget, service_url)
        return {"fields": preset["fetch"](Ctx(preset_id, base, widget.get("options"), secrets))}
    except WidgetError as e:
        return {"error": str(e)}
    except (KeyError, TypeError, AttributeError, ValueError, IndexError) as e:
        return {"error": f"unexpected response ({type(e).__name__}): is this the right widget type?"}
    except Exception as e:  # one misbehaving app must never break the other tiles
        return {"error": f"widget failed ({type(e).__name__})"}


# ---------------------------------------------------------------- calendars (Upcoming tab)

def _iso(d):
    return datetime.datetime.fromisoformat(d.replace("Z", "+00:00"))


def _sonarr_calendar(ctx, start, end, link):
    eps = ctx.json("/api/v3/calendar", headers={"X-Api-Key": ctx.need("apiKey", "API key")}, params={
        "start": start.isoformat(), "end": end.isoformat(), "includeSeries": "true", "unmonitored": "false"})
    items = []
    for e in eps:
        series = e.get("series") or {}
        title = (e.get("title") or "").strip()
        items.append({
            "date": e.get("airDateUtc") or e.get("airDate"),
            "allDay": not e.get("airDateUtc"),
            "kind": "episode",
            "title": series.get("title") or "Unknown series",
            "detail": f"S{e.get('seasonNumber', 0):02d}E{e.get('episodeNumber', 0):02d}"
                      + (f" · {title}" if title and title != "TBA" else ""),
            "hasFile": bool(e.get("hasFile")),
            "link": f"{link}/series/{series['titleSlug']}" if series.get("titleSlug") else link,
        })
    return items


_RADARR_DATES = (("inCinemas", "In cinemas"), ("digitalRelease", "Digital release"), ("physicalRelease", "Physical release"))


def _radarr_calendar(ctx, start, end, link):
    movies = ctx.json("/api/v3/calendar", headers={"X-Api-Key": ctx.need("apiKey", "API key")}, params={
        "start": start.isoformat(), "end": end.isoformat(), "unmonitored": "false"})
    items = []
    for m in movies:
        for key, label in _RADARR_DATES:  # a movie can have several release dates in the window
            d = m.get(key)
            if d and start <= _iso(d) < end:
                items.append({
                    "date": d,
                    "allDay": True,
                    "kind": "movie",
                    "title": f"{m.get('title', 'Unknown movie')}" + (f" ({m['year']})" if m.get("year") else ""),
                    "detail": label,
                    "hasFile": bool(m.get("hasFile")),
                    "link": f"{link}/movie/{m.get('titleSlug') or m.get('tmdbId', '')}",
                })
    return items


def _lidarr_calendar(ctx, start, end, link):
    albums = ctx.json("/api/v1/calendar", headers={"X-Api-Key": ctx.need("apiKey", "API key")}, params={
        "start": start.isoformat(), "end": end.isoformat(), "includeArtist": "true", "unmonitored": "false"})
    return [{
        "date": a.get("releaseDate"),
        "allDay": True,
        "kind": "album",
        "title": (a.get("artist") or {}).get("artistName") or "Unknown artist",
        "detail": a.get("title") or "",
        "hasFile": ((a.get("statistics") or {}).get("trackFileCount") or 0) > 0,
        "link": f"{link}/album/{a['foreignAlbumId']}" if a.get("foreignAlbumId") else link,
    } for a in albums if a.get("releaseDate")]


def _readarr_calendar(ctx, start, end, link):
    books = ctx.json("/api/v1/calendar", headers={"X-Api-Key": ctx.need("apiKey", "API key")}, params={
        "start": start.isoformat(), "end": end.isoformat(), "includeAuthor": "true", "unmonitored": "false"})
    return [{
        "date": b.get("releaseDate"),
        "allDay": True,
        "kind": "book",
        "title": b.get("title") or "Unknown book",
        "detail": (b.get("author") or {}).get("authorName") or "",
        "hasFile": ((b.get("statistics") or {}).get("bookFileCount") or 0) > 0,
        "link": f"{link}/book/{b['titleSlug']}" if b.get("titleSlug") else link,
    } for b in books if b.get("releaseDate")]


def _magarr_calendar(ctx, start, end, link):
    # Magarr takes whole dates, both ends inclusive.
    issues = ctx.json("/api/v1/calendar", headers={"X-Api-Key": ctx.need("apiKey", "API key")}, params={
        "start": start.date().isoformat(), "end": (end - datetime.timedelta(days=1)).date().isoformat()})
    return [{
        "date": i.get("publishDate"),
        "allDay": True,
        "kind": "magazine",
        "title": i.get("magazineTitle") or "Unknown magazine",
        "detail": i.get("title") or f"Issue {i.get('issueNumber', '')}".strip(),
        "hasFile": i.get("file") is not None,
        "link": f"{link}/magazine/{i['magazineId']}" if i.get("magazineId") else link,
    } for i in issues if i.get("publishDate")]


CALENDARS = {
    "sonarr": ("episode", _sonarr_calendar),
    "radarr": ("movie", _radarr_calendar),
    "lidarr": ("album", _lidarr_calendar),
    "readarr": ("book", _readarr_calendar),
    "magarr": ("magazine", _magarr_calendar),
}


def calendar(widget, service_url, secrets, start, end):
    """Calendar items from an *arr widget's app. Returns {"items": [...]} or {"error": "..."}."""
    preset_id = widget.get("type")
    if preset_id not in CALENDARS:
        return {"error": "not a calendar app"}
    try:
        ctx = Ctx(preset_id, api_base(widget, service_url), widget.get("options"), secrets)
        link = (service_url or ctx.base).rstrip("/")
        return {"items": CALENDARS[preset_id][1](ctx, start, end, link)}
    except WidgetError as e:
        return {"error": str(e)}
    except (KeyError, TypeError, AttributeError, ValueError, IndexError) as e:
        return {"error": f"unexpected response ({type(e).__name__})"}
    except Exception as e:
        return {"error": f"calendar failed ({type(e).__name__})"}
