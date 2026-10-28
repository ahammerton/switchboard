#!/usr/bin/env python3
"""Switchboard - a tiny, dependency-free homelab dashboard server.

Serves the static front end, stores the dashboard config as JSON and
performs server-side health checks of the configured services.

Environment variables:
  PORT           port to listen on (default 3000)
  HOST           address to bind (default 0.0.0.0)
  CONFIG_DIR     directory holding config.json (default ./config)
  EDIT_TOKEN     token required for every change. If unset, one is generated
                 on first start and kept in CONFIG_DIR/edit-token.
                 EDIT_TOKEN=off allows changes without a token.
  ALLOWED_HOSTS  extra host names the dashboard may be opened under, comma-
                 separated (e.g. dash.example.com, *.example.com). IP addresses,
                 localhost and private names (.lan, .local, .home.arpa,
                 .internal) are always allowed; "*" allows any name.
"""
import concurrent.futures
import datetime
import email.utils
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import discover
import monitor
import sysstats
import widgets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
DEFAULT_CONFIG = os.path.join(BASE_DIR, "default-config.json")
CONFIG_DIR = os.environ.get("CONFIG_DIR", os.path.join(BASE_DIR, "config"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
BG_DIR = os.path.join(CONFIG_DIR, "backgrounds")
SECRETS_FILE = os.path.join(CONFIG_DIR, "secrets.json")
HISTORY_FILE = os.path.join(CONFIG_DIR, "history.json")
ALERTS_SECRET_ID = "_alerts"  # secrets.json entry holding the alert webhook URL
VERSION = "0.1.0"
EDIT_TOKEN = os.environ.get("EDIT_TOKEN", "")  # resolved properly in main()
EDIT_TOKEN_FILE = os.path.join(CONFIG_DIR, "edit-token")
TOKEN_OFF = ("off", "none", "false", "no", "0")
ALLOWED_HOSTS = [h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
LOCAL_SUFFIXES = (".lan", ".local", ".home.arpa", ".internal", ".localdomain", ".localhost")
FAILED_TOKEN_LIMIT = 10      # wrong tokens per address…
FAILED_TOKEN_WINDOW = 60     # …per this many seconds before a lock-out
PORT = int(os.environ.get("PORT", "3000"))
HOST = os.environ.get("HOST", "0.0.0.0")

MAX_BODY = 1024 * 1024
MAX_UPLOAD = 20 * 1024 * 1024
BG_NAME_RE = re.compile(r"^[a-z0-9-]+\.(jpg|png|gif|webp|avif)$")
IMAGE_MIME = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp", "avif": "image/avif"}
CHECK_TIMEOUT = 5
STATUS_CACHE_SECONDS = 15
WEATHER_CACHE_SECONDS = 600
RSS_CACHE_SECONDS = 900
RSS_MIN_REFRESH_SECONDS = 60
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_FEED_ITEMS = 30
WIDGET_DEFAULT_INTERVAL = 60
WIDGET_MIN_INTERVAL = 10
SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Homelab services very often use self-signed certificates.
_insecure_ssl = ssl.create_default_context()
_insecure_ssl.check_hostname = False
_insecure_ssl.verify_mode = ssl.CERT_NONE

_config_lock = threading.Lock()
_status_cache = {"at": 0.0, "data": {}}
_status_lock = threading.Lock()
_weather_cache = {"key": None, "at": 0.0, "data": None}
_weather_lock = threading.Lock()
_rss_cache = {}  # url -> (fetched_at, data)
_rss_lock = threading.Lock()
_secrets_lock = threading.Lock()
_widget_cache = {}  # service id -> (fetched_at, cache key, result)
_widget_lock = threading.Lock()
_failed_tokens = {}  # client address -> recent failure times
_failed_lock = threading.Lock()
_warned_hosts = set()
_system_cache = {"at": 0.0, "key": None, "data": None}
_system_lock = threading.Lock()
SYSTEM_CACHE_SECONDS = 2
_upcoming_cache = {"at": 0.0, "key": None, "data": None}
_upcoming_lock = threading.Lock()
UPCOMING_CACHE_SECONDS = 600
UPCOMING_MIN_REFRESH_SECONDS = 60
MONITOR = None  # monitor.Monitor, created in main()
DEFAULT_DISKS = [{"path": "/", "label": "System"}]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect still means the service is up


_opener = urllib.request.build_opener(
    _NoRedirect, urllib.request.HTTPSHandler(context=_insecure_ssl)
)
# Feeds follow redirects; self-signed certs are allowed for feeds hosted in the lab.
_feed_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_insecure_ssl))

with open(DEFAULT_CONFIG, encoding="utf-8") as _f:
    DEFAULT_RSS = json.load(_f)["rss"]


def ensure_config():
    os.makedirs(BG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_FILE):
        shutil.copyfile(DEFAULT_CONFIG, CONFIG_FILE)


def normalise_config(cfg):
    """Bring older configs up to date: top-level groups move into a single
    tab, and the RSS section is added (switched off) with the sample feeds."""
    if not isinstance(cfg, dict):
        return cfg
    if "tabs" not in cfg and isinstance(cfg.get("groups"), list):
        cfg["tabs"] = [{"name": "Home", "groups": cfg.pop("groups")}]
    if "rss" not in cfg:
        cfg["rss"] = {**DEFAULT_RSS, "enabled": False}
    return cfg


def load_config():
    with _config_lock, open(CONFIG_FILE, encoding="utf-8") as f:
        return normalise_config(json.load(f))


def save_config(cfg):
    with _config_lock:
        fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)


def validate_config(cfg):
    if not isinstance(cfg, dict):
        return "config must be an object"
    tabs = cfg.get("tabs")
    if not isinstance(tabs, list) or not tabs:
        return "config.tabs must be a non-empty array"
    for t in tabs:
        if not isinstance(t, dict) or not isinstance(t.get("groups", []), list):
            return "each tab must be an object with a groups array"
        for g in t.get("groups", []):
            if not isinstance(g, dict) or not isinstance(g.get("services", []), list):
                return "each group must be an object with a services array"
    rss = cfg.get("rss", {})
    if not isinstance(rss, dict) or not isinstance(rss.get("feeds", []), list):
        return "config.rss.feeds must be an array"
    return None


def iter_services(cfg):
    for t in cfg.get("tabs", []):
        for g in t.get("groups", []):
            yield from g.get("services", [])


def sniff_image(data):
    """Identify an image by its magic bytes (SVG is deliberately not allowed)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    return None


def save_background(data, filename):
    """Store an uploaded image as <name>-<hash>.<ext>; identical files share one copy."""
    ext = sniff_image(data)
    if not ext:
        raise ValueError("unsupported image type (use JPEG, PNG, GIF, WebP or AVIF)")
    stem = re.sub(r"[^a-z0-9]+", "-", os.path.splitext(filename)[0].lower()).strip("-")[:40] or "background"
    name = f"{stem}-{hashlib.sha256(data).hexdigest()[:10]}.{ext}"
    path = os.path.join(BG_DIR, name)
    if not os.path.exists(path):
        fd, tmp = tempfile.mkstemp(dir=BG_DIR, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    return {"name": name, "url": f"/backgrounds/{name}", "size": len(data)}


def list_backgrounds():
    images = []
    for entry in os.scandir(BG_DIR):
        if entry.is_file() and BG_NAME_RE.match(entry.name):
            st = entry.stat()
            images.append((st.st_mtime, {"name": entry.name, "url": f"/backgrounds/{entry.name}", "size": st.st_size}))
    return [img for _, img in sorted(images, key=lambda x: x[0], reverse=True)]


# ---------------------------------------------------------------- secrets
# API keys for widgets live in secrets.json, keyed by service id. They are
# never sent to the browser; the page only learns which keys are set.

def load_secrets():
    with _secrets_lock:
        try:
            with open(SECRETS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}


def save_secrets(data):
    with _secrets_lock:
        fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SECRETS_FILE)


def update_secrets(service_id, values):
    """Merge values: a non-empty string sets a key, null removes it, "" keeps it."""
    data = load_secrets()
    entry = data.get(service_id, {})
    for k, v in values.items():
        if v is None:
            entry.pop(k, None)
        elif isinstance(v, str) and v:
            entry[k] = v
    if entry:
        data[service_id] = entry
    else:
        data.pop(service_id, None)
    save_secrets(data)


def prune_secrets(cfg):
    """Forget secrets of services that were deleted or no longer have a widget."""
    ids = {s["id"] for s in widget_services(cfg)}
    data = load_secrets()
    kept = {k: v for k, v in data.items() if k in ids or k.startswith("_")}  # "_…" = non-service secrets
    if kept != data:
        save_secrets(kept)


# ---------------------------------------------------------------- widgets

def widget_services(cfg):
    for s in iter_services(cfg):
        w = s.get("widget")
        if isinstance(w, dict) and w.get("type") and s.get("id"):
            yield s


def run_widget(service, secrets):
    w = service["widget"]
    interval = max(WIDGET_MIN_INTERVAL, int(w.get("interval") or WIDGET_DEFAULT_INTERVAL))
    key = json.dumps([w, service.get("url"), secrets], sort_keys=True)
    with _widget_lock:
        cached = _widget_cache.get(service["id"])
    if cached and cached[1] == key and time.time() - cached[0] < interval:
        return cached[2]
    result = widgets.run(w, service.get("url"), secrets)
    with _widget_lock:
        _widget_cache[service["id"]] = (time.time(), key, result)
    return result


def collect_widgets():
    services = list(widget_services(load_config()))
    secrets = load_secrets()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda s: run_widget(s, secrets.get(s["id"], {})), services)
        return {s["id"]: r for s, r in zip(services, results)}


def get_system():
    """Host stats for the stats bar; nothing is collected unless it's switched on."""
    sysc = load_config().get("system") or {}
    if not sysc.get("enabled"):
        return {"enabled": False}
    disks = [d for d in sysc.get("disks", DEFAULT_DISKS) if isinstance(d, dict)]
    key = json.dumps(disks, sort_keys=True)
    with _system_lock:
        if _system_cache["key"] == key and time.time() - _system_cache["at"] < SYSTEM_CACHE_SECONDS:
            return _system_cache["data"]
        data = {"enabled": True, **sysstats.collect(disks)}
        _system_cache.update(at=time.time(), key=key, data=data)
        return data


def alert_webhook():
    return load_secrets().get(ALERTS_SECRET_ID, {}).get("webhookUrl", "")


def _when(d):
    """Sort key for calendar dates: date-only and naive values count as UTC."""
    try:
        dt = datetime.datetime.fromisoformat(str(d).replace("Z", "+00:00"))
    except ValueError:
        return datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def get_upcoming(force=False):
    """Combined calendar from the Sonarr/Radarr/Lidarr/Readarr services that have widgets."""
    cfg = load_config()
    uc = cfg.get("upcoming") or {}
    if not uc.get("enabled"):
        return {"enabled": False}
    kinds = uc.get("kinds") or {}
    days = max(1, min(60, int(uc.get("days") or 7)))
    past = max(0, min(30, int(uc.get("pastDays", 1) or 0)))
    today = datetime.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = today - datetime.timedelta(days=past), today + datetime.timedelta(days=days + 1)
    sources = [s for s in widget_services(cfg)
               if s["widget"]["type"] in widgets.CALENDARS
               and kinds.get(widgets.CALENDARS[s["widget"]["type"]][0], True) is not False]
    secrets = load_secrets()
    key = json.dumps([[s["id"], s["widget"], s.get("url"), secrets.get(s["id"])] for s in sources]
                     + [start.isoformat(), end.isoformat()], sort_keys=True)
    with _upcoming_lock:
        age = time.time() - _upcoming_cache["at"]
        max_age = UPCOMING_MIN_REFRESH_SECONDS if force else UPCOMING_CACHE_SECONDS
        if _upcoming_cache["key"] == key and age < max_age:
            return _upcoming_cache["data"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda s: widgets.calendar(s["widget"], s.get("url"), secrets.get(s["id"], {}), start, end), sources))
        items, srcs = [], []
        for svc, r in zip(sources, results):
            if "error" in r:
                srcs.append({"name": svc.get("name"), "error": r["error"]})
                continue
            for it in r["items"]:
                it["source"] = svc.get("name")
                items.append(it)
            srcs.append({"name": svc.get("name"), "count": len(r["items"])})
        items.sort(key=lambda it: _when(it.get("date")))
        data = {"enabled": True, "today": today.date().isoformat(), "start": start.isoformat(),
                "end": end.isoformat(), "items": items, "sources": srcs}
        _upcoming_cache.update(at=time.time(), key=key, data=data)
        return data


def check_url(url):
    if not url.startswith(("http://", "https://")):
        return {"up": False, "error": "unsupported url"}
    start = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": "switchboard"})
    try:
        with _opener.open(req, timeout=CHECK_TIMEOUT) as res:
            code = res.status
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
    except Exception as e:  # connection refused, timeout, DNS...
        return {"up": False, "error": str(getattr(e, "reason", e))}
    return {"up": code < 500, "code": code, "ms": round((time.monotonic() - start) * 1000)}


def collect_status(force=False):
    """Check every service in the config. Only configured URLs are ever
    requested, so the endpoint can't be abused to probe arbitrary hosts.
    The background monitor calls this with force=True on every tick."""
    with _status_lock:
        if not force and time.time() - _status_cache["at"] < STATUS_CACHE_SECONDS:
            return _status_cache["data"]
        urls = {
            s.get("statusUrl") or s.get("url")
            for s in iter_services(load_config())
            if s.get("check", True) and (s.get("statusUrl") or s.get("url"))
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            data = dict(zip(urls, pool.map(check_url, urls)))
        _status_cache.update(at=time.time(), data=data)
        return data


def fetch_json(url, params):
    req = urllib.request.Request(
        url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": "switchboard"}
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.load(res)


def get_weather():
    """Current conditions + 4-day forecast from Open-Meteo (no API key needed)."""
    w = load_config().get("weather") or {}
    if not w.get("enabled") or w.get("latitude") is None or w.get("longitude") is None:
        return {"enabled": False}
    imperial = w.get("units") == "imperial"
    key = (w["latitude"], w["longitude"], imperial, w.get("name"))
    with _weather_lock:
        if _weather_cache["key"] == key and time.time() - _weather_cache["at"] < WEATHER_CACHE_SECONDS:
            return _weather_cache["data"]
        raw = fetch_json("https://api.open-meteo.com/v1/forecast", {
            "latitude": w["latitude"],
            "longitude": w["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "timezone": "auto",
            "forecast_days": 4,
            "temperature_unit": "fahrenheit" if imperial else "celsius",
            "wind_speed_unit": "mph" if imperial else "kmh",
        })
        cur, daily = raw["current"], raw["daily"]
        data = {
            "enabled": True,
            "location": w.get("name", ""),
            "units": {"temp": "°F" if imperial else "°C", "wind": "mph" if imperial else "km/h"},
            "current": {
                "temp": cur["temperature_2m"],
                "feelsLike": cur["apparent_temperature"],
                "humidity": cur["relative_humidity_2m"],
                "wind": cur["wind_speed_10m"],
                "code": cur["weather_code"],
                "isDay": bool(cur["is_day"]),
            },
            "daily": [
                {"date": d, "code": c, "max": hi, "min": lo}
                for d, c, hi, lo in zip(daily["time"], daily["weather_code"],
                                        daily["temperature_2m_max"], daily["temperature_2m_min"])
            ],
        }
        _weather_cache.update(key=key, at=time.time(), data=data)
        return data


def geocode(query):
    """Resolve "City" or "City, Region/Country" to coordinates."""
    name, _, hint = (part.strip() for part in query.partition(","))
    raw = fetch_json("https://geocoding-api.open-meteo.com/v1/search",
                     {"name": name, "count": 10, "language": "en", "format": "json"})
    results = raw.get("results") or []
    if hint:
        h = hint.lower()
        results = [r for r in results if any(
            h in (r.get(k) or "").lower() for k in ("country", "country_code", "admin1", "admin2"))]
    if not results:
        return None
    r = results[0]
    return {
        "name": r["name"],
        "label": ", ".join(x for x in (r["name"], r.get("admin1"), r.get("country")) if x),
        "latitude": r["latitude"],
        "longitude": r["longitude"],
    }


def _local(tag):
    return tag.rsplit("}", 1)[-1]  # strip XML namespace


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el, *names):
    c = _child(el, *names)
    return (c.text or "").strip() if c is not None else ""


def _atom_link(el):
    links = [c for c in el if _local(c.tag) == "link"]
    for c in links:
        if c.get("rel", "alternate") == "alternate":
            return c.get("href", "")
    return links[0].get("href", "") if links else ""


def _plain(s, limit=220):
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def _date(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)  # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))  # Atom: ISO 8601
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.isoformat()


def parse_feed(data):
    """Parse RSS 2.0, RSS 1.0 (RDF) or Atom into a common shape."""
    root = ET.fromstring(data)
    items = []
    if _local(root.tag) == "feed":  # Atom
        title, link = _text(root, "title"), _atom_link(root)
        for e in root:
            if _local(e.tag) == "entry":
                items.append({
                    "title": _plain(_text(e, "title"), 300),
                    "link": _atom_link(e),
                    "date": _date(_text(e, "published", "updated")),
                    "summary": _plain(_text(e, "summary", "content")),
                })
    else:
        channel = _child(root, "channel")
        if channel is None:
            raise ValueError("not an RSS or Atom feed")
        title, link = _text(channel, "title"), _text(channel, "link")
        # RSS 2.0 nests items in <channel>; RSS 1.0 puts them beside it.
        parent = channel if _child(channel, "item") is not None else root
        for it in parent:
            if _local(it.tag) == "item":
                items.append({
                    "title": _plain(_text(it, "title"), 300),
                    "link": _text(it, "link") or _text(it, "guid"),
                    "date": _date(_text(it, "pubDate", "date", "published", "updated")),
                    "summary": _plain(_text(it, "description", "summary", "encoded")),
                })
    return {"title": _plain(title, 120), "link": link, "items": items[:MAX_FEED_ITEMS]}


def fetch_feed(url):
    if not url.startswith(("http://", "https://")):
        return {"error": "unsupported url"}
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; switchboard/1.0)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.5",
    })
    try:
        with _feed_opener.open(req, timeout=10) as res:
            data = res.read(MAX_FEED_BYTES + 1)
        if len(data) > MAX_FEED_BYTES:
            return {"error": "feed is too large"}
        return parse_feed(data)
    except urllib.error.HTTPError as e:
        e.close()
        return {"error": f"HTTP {e.code}"}
    except ET.ParseError:
        return {"error": "not a valid RSS/Atom feed"}
    except Exception as e:
        return {"error": str(getattr(e, "reason", e))}


def get_rss(force=False):
    """Fetch every configured feed (only configured URLs, never arbitrary
    ones), re-using cached copies younger than RSS_CACHE_SECONDS."""
    urls = [f.get("url") for f in (load_config().get("rss") or {}).get("feeds", []) if f.get("url")]
    max_age = RSS_MIN_REFRESH_SECONDS if force else RSS_CACHE_SECONDS
    with _rss_lock:
        now = time.time()
        stale = [u for u in set(urls) if now - _rss_cache.get(u, (0, None))[0] > max_age]
        if stale:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                for u, data in zip(stale, pool.map(fetch_feed, stale)):
                    _rss_cache[u] = (time.time(), data)
        return {"feeds": {u: _rss_cache[u][1] for u in urls}}


def resolve_edit_token():
    """EDIT_TOKEN from the environment, else the saved one, else a new one (saved and printed once)."""
    env = os.environ.get("EDIT_TOKEN")
    if env is not None and env.strip():
        return "" if env.strip().lower() in TOKEN_OFF else env.strip()
    try:
        with open(EDIT_TOKEN_FILE, encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            print(f"Edit token: saved in {EDIT_TOKEN_FILE}", flush=True)
            return saved
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(18)
    fd = os.open(EDIT_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    print("=" * 64, f"\nNew edit token (needed to change the dashboard): {token}\n"
          f"It is saved in {EDIT_TOKEN_FILE}. Set EDIT_TOKEN to choose your own,\n"
          f"or EDIT_TOKEN=off to allow changes without one.\n" + "=" * 64, flush=True)
    return token


def split_host(host_header):
    """("name", "port") from a Host header, handling [IPv6]:port."""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        name, _, rest = h[1:].partition("]")
        return name, rest.lstrip(":")
    name, _, port = h.rpartition(":") if h.count(":") == 1 else (h, "", "")
    return (name, port) if name else (h, "")


def host_allowed(host_header):
    """Defence against DNS rebinding: refuse public names we weren't told about."""
    name, _ = split_host(host_header)
    if not name or "*" in ALLOWED_HOSTS or name in ALLOWED_HOSTS:
        return True
    if any(a.startswith("*.") and name.endswith(a[1:]) for a in ALLOWED_HOSTS):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return (name == "localhost" or "." not in name or name.endswith(LOCAL_SUFFIXES)
            or name in (socket.gethostname().lower(), socket.getfqdn().lower()))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC_DIR, **kwargs)

    def log_message(self, fmt, *args):
        if os.environ.get("QUIET") != "1":
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        if getattr(self, "_static", False):
            # Make browsers re-validate the page files (a cheap 304 when unchanged),
            # so an updated app.js is picked up without a hard refresh.
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def guard(self, changes=False):
        """Run before every request. Refuses unknown public host names (DNS rebinding)
        and, for requests that change anything, requests from other websites (CSRF)."""
        host = self.headers.get("Host", "")
        if not host_allowed(host):
            name = split_host(host)[0]
            if name not in _warned_hosts:
                _warned_hosts.add(name)
                print(f"Refused requests for host name '{name}': add it to ALLOWED_HOSTS to use it", flush=True)
            self.send_json({"error": f"'{name}' isn't an allowed host name for this dashboard. "
                                     f"Add it to the ALLOWED_HOSTS setting (e.g. ALLOWED_HOSTS={name})."}, 421)
            return False
        if changes:
            site = self.headers.get("Sec-Fetch-Site")
            origin = self.headers.get("Origin")
            cross = site in ("cross-site", "same-site")
            if origin is not None and not cross:
                o = urllib.parse.urlsplit(origin)
                cross = origin == "null" or o.netloc.lower() != host.lower()
            if cross:
                self.send_json({"error": "blocked: changes must come from the dashboard itself"}, 403)
                return False
        return True

    def authorised(self):
        if not EDIT_TOKEN:
            return True
        ip = self.client_address[0]
        now = time.time()
        with _failed_lock:
            recent = [t for t in _failed_tokens.get(ip, []) if now - t < FAILED_TOKEN_WINDOW]
            _failed_tokens[ip] = recent
        if len(recent) >= FAILED_TOKEN_LIMIT:
            self.send_json({"error": "too many wrong edit tokens: wait a minute and try again"}, 429)
            return False
        if hmac.compare_digest(self.headers.get("X-Edit-Token", "").encode(), EDIT_TOKEN.encode()):
            return True
        with _failed_lock:
            _failed_tokens.setdefault(ip, []).append(now)
        self.send_json({"error": "invalid edit token"}, 401)
        return False

    def send_background(self, name):
        if not BG_NAME_RE.match(name):
            return self.send_json({"error": "not found"}, 404)
        try:
            with open(os.path.join(BG_DIR, name), "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return self.send_json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", IMAGE_MIME[name.rsplit(".", 1)[1]])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")  # names are content-hashed
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        if self.guard():
            super().do_HEAD()

    def do_GET(self):
        if not self.guard():
            return
        path = self.path.split("?", 1)[0]
        if path.startswith("/backgrounds/"):
            return self.send_background(urllib.parse.unquote(path[len("/backgrounds/"):]))
        if path == "/api/backgrounds":
            return self.send_json({"images": list_backgrounds()})
        if path == "/api/system":
            return self.send_json(get_system())
        if path == "/api/history":
            return self.send_json(MONITOR.history.summary())
        if path == "/api/alerts/status":
            return self.send_json({**MONITOR.status, "webhookSet": bool(alert_webhook())})
        if path == "/api/upcoming":
            return self.send_json(get_upcoming(force="refresh=1" in self.path.partition("?")[2]))
        if path == "/api/widgets":
            return self.send_json({"widgets": collect_widgets()})
        if path == "/api/widgets/presets":
            return self.send_json({"presets": widgets.preset_catalog()})
        if path.startswith("/api/secrets/"):
            sid = urllib.parse.unquote(path[len("/api/secrets/"):])
            if not SERVICE_ID_RE.match(sid):
                return self.send_json({"error": "bad service id"}, 400)
            return self.send_json({"set": sorted(load_secrets().get(sid, {}))})
        if path == "/api/config":
            return self.send_json(load_config())
        if path == "/api/meta":
            return self.send_json({"editProtected": bool(EDIT_TOKEN), "version": VERSION})
        if path == "/api/status":
            return self.send_json(collect_status())
        if path == "/api/weather":
            try:
                return self.send_json(get_weather())
            except Exception as e:
                return self.send_json({"error": f"weather unavailable: {e}"}, 502)
        if path == "/api/rss":
            force = "refresh=1" in self.path.partition("?")[2]
            return self.send_json(get_rss(force))
        if path == "/api/geocode":
            q = urllib.parse.parse_qs(self.path.partition("?")[2]).get("q", [""])[0].strip()
            if not q:
                return self.send_json({"error": "missing q"}, 400)
            try:
                place = geocode(q)
            except Exception as e:
                return self.send_json({"error": f"lookup failed: {e}"}, 502)
            return self.send_json(place or {"error": f"no place found for “{q}”"}, 200 if place else 404)
        if path.startswith("/api/"):
            return self.send_json({"error": "not found"}, 404)
        self._static = True
        return super().do_GET()

    def read_json_body(self):
        # Requiring JSON also means browsers can't send these as "simple" cross-site requests.
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            self.send_json({"error": "expected Content-Type: application/json"}, 415)
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self.send_json({"error": "bad body size"}, 413)
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as e:
            self.send_json({"error": f"invalid JSON: {e}"}, 400)
            return None

    def test_widget(self):
        """Try a widget from the edit dialog before it is saved. Blank secrets
        fall back to the ones already stored for that service."""
        if not self.authorised():
            return
        body = self.read_json_body()
        if not isinstance(body, dict) or not isinstance(body.get("widget"), dict):
            return self.send_json({"error": "expected {widget, url, id, secrets}"}, 400)
        stored = load_secrets().get(body.get("id") or "", {})
        given = {k: v for k, v in (body.get("secrets") or {}).items() if isinstance(v, str) and v}
        return self.send_json(widgets.run(body["widget"], body.get("url"), {**stored, **given}))

    def test_alert(self):
        """Send a test message, using a URL typed into the dialog or the saved one."""
        if not self.authorised():
            return
        body = self.read_json_body()
        if not isinstance(body, dict):
            return
        url = (body.get("webhookUrl") or "").strip() or alert_webhook()
        if not url.startswith(("http://", "https://")):
            return self.send_json({"error": "enter a webhook URL first"}, 400)
        cfg = load_config()
        try:
            monitor.send([{"kind": "test", "title": "Test alert from Switchboard",
                           "message": "If you can read this, alerts are working."}],
                         body.get("type") or "discord", url, cfg.get("title") or "Switchboard")
        except Exception as e:
            return self.send_json({"error": str(e)}, 502)
        return self.send_json({"ok": True})

    def discover_services(self):
        """Scan for services (edit token required: it probes the network)."""
        if not self.authorised():
            return
        body = self.read_json_body()
        if not isinstance(body, dict):
            return
        host_header = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        me = {(h, PORT) for h in (host_header, "127.0.0.1", "localhost", socket.gethostname()) if h}
        result = discover.scan(load_config(), load_secrets(),
                               use_config_hosts=body.get("useConfigHosts", True),
                               use_proxmox=body.get("useProxmox", True),
                               extra=str(body.get("hosts") or "")[:4000],
                               self_endpoints=me)
        return self.send_json(result)

    def do_POST(self):
        if not self.guard(changes=True):
            return
        if self.path.split("?", 1)[0] == "/api/discover":
            return self.discover_services()
        if self.path.split("?", 1)[0] == "/api/alerts/test":
            return self.test_alert()
        if self.path.split("?", 1)[0] == "/api/widgets/test":
            return self.test_widget()
        if self.path.split("?", 1)[0] != "/api/backgrounds":
            return self.send_json({"error": "not found"}, 404)
        if not self.authorised():
            return
        ctype = (self.headers.get("Content-Type") or "").lower()
        if not (ctype.startswith("image/") or ctype.startswith("application/octet-stream")):
            return self.send_json({"error": "expected an image upload"}, 415)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self.send_json({"error": "empty upload"}, 400)
        if length > MAX_UPLOAD:
            return self.send_json({"error": f"image is larger than {MAX_UPLOAD // (1024 * 1024)} MB"}, 413)
        filename = urllib.parse.unquote(self.headers.get("X-Filename", ""))
        try:
            return self.send_json(save_background(self.rfile.read(length), filename), 201)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 415)

    def do_DELETE(self):
        if not self.guard(changes=True):
            return
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/backgrounds/"):
            return self.send_json({"error": "not found"}, 404)
        if not self.authorised():
            return
        name = urllib.parse.unquote(path[len("/api/backgrounds/"):])
        if not BG_NAME_RE.match(name):
            return self.send_json({"error": "not found"}, 404)
        try:
            os.remove(os.path.join(BG_DIR, name))
        except FileNotFoundError:
            return self.send_json({"error": "not found"}, 404)
        return self.send_json({"ok": True})

    def do_PUT(self):
        if not self.guard(changes=True):
            return
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/secrets/"):
            if not self.authorised():
                return
            sid = urllib.parse.unquote(path[len("/api/secrets/"):])
            if not SERVICE_ID_RE.match(sid):
                return self.send_json({"error": "bad service id"}, 400)
            body = self.read_json_body()
            if body is None:
                return
            if not isinstance(body, dict):
                return self.send_json({"error": "expected an object"}, 400)
            update_secrets(sid, body)
            with _widget_lock:
                _widget_cache.pop(sid, None)
            return self.send_json({"ok": True})
        if path != "/api/config":
            return self.send_json({"error": "not found"}, 404)
        if not self.authorised():
            return
        body = self.read_json_body()
        if body is None:
            return
        cfg = normalise_config(body)
        err = validate_config(cfg)
        if err:
            return self.send_json({"error": err}, 400)
        save_config(cfg)
        prune_secrets(cfg)
        _status_cache["at"] = 0  # re-check with the new service list
        _weather_cache["at"] = 0
        return self.send_json({"ok": True})


def main():
    global MONITOR, EDIT_TOKEN
    ensure_config()
    EDIT_TOKEN = resolve_edit_token()
    if not EDIT_TOKEN:
        print("EDIT_TOKEN=off: anyone who can open the dashboard can change it", flush=True)
    MONITOR = monitor.Monitor(HISTORY_FILE, load_config=load_config, collect_status=collect_status,
                              collect_widgets=collect_widgets, get_system=get_system, alert_webhook=alert_webhook)
    MONITOR.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Switchboard {VERSION} on http://{HOST}:{PORT}  (config: {CONFIG_FILE})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
