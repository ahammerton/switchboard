"""Background monitor: 24-hour uptime history and alerts (Discord or a JSON webhook).

It runs the same checks as the page, but on a timer in the server, so alerts
fire even when nobody has the dashboard open. A problem must persist for
`failures` consecutive checks (default 2) before an alert is sent, and every
alert gets a matching "recovered"/"cleared" message when it goes away.
"""
import datetime
import json
import os
import ssl
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request

BUCKET_SECONDS = 1800          # one uptime bar segment = 30 minutes
BUCKETS = 48                   # 24 hours
HISTORY_SAVE_SECONDS = 300
DISCORD_MAX_EMBEDS = 10

COLOURS = {"down": 0xDC2626, "up": 0x16A34A, "warn": 0xF59E0B, "clear": 0x16A34A, "test": 0x6366F1}
ICONS = {"down": "🔴", "up": "🟢", "warn": "🟠", "clear": "✅", "test": "🔔"}

_insecure_ssl = ssl.create_default_context()
_insecure_ssl.check_hostname = False
_insecure_ssl.verify_mode = ssl.CERT_NONE


def _duration(seconds):
    seconds = int(seconds)
    d, h, m = seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{max(m, 1)}m"


def format_value(value, fmt):
    """Python twin of the page's formatStat(), for alert text."""
    if value is None or value == "":
        return "—"
    if not isinstance(value, (int, float)) or fmt == "text":
        return str(value)
    if fmt in ("bytes", "speed"):
        n, units = float(value), ["B", "KB", "MB", "GB", "TB", "PB"]
        i = 0
        while abs(n) >= 1024 and i < len(units) - 1:
            n, i = n / 1024, i + 1
        s = f"{round(n) if n >= 100 or i == 0 else round(n, 1)} {units[i]}"
        return s + "/s" if fmt == "speed" else s
    if fmt == "percent":
        return f"{round(value, 1) if value < 10 else round(value)}%"
    if fmt == "duration":
        return _duration(value)
    if fmt == "ms":
        return f"{round(value)} ms"
    return f"{round(value, 1):g}"


# ---------------------------------------------------------------- uptime history

class UptimeHistory:
    """Per status URL: {bucket number: [checks up, checks total]} for the last 24 h."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {}
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            self.data = {url: {int(b): v for b, v in buckets.items()} for url, buckets in raw.items()}
        except (OSError, ValueError, AttributeError):
            pass

    def record(self, status, now=None):
        bucket = int((now or time.time()) // BUCKET_SECONDS)
        oldest = bucket - BUCKETS + 1
        with self.lock:
            for url, result in status.items():
                counts = self.data.setdefault(url, {}).setdefault(bucket, [0, 0])
                counts[1] += 1
                counts[0] += 1 if result.get("up") else 0
            for url in list(self.data):
                self.data[url] = {b: v for b, v in self.data[url].items() if b >= oldest}
                if not self.data[url]:
                    del self.data[url]

    def summary(self, now=None):
        bucket = int((now or time.time()) // BUCKET_SECONDS)
        out = {}
        with self.lock:
            for url, buckets in self.data.items():
                series, up, total = [], 0, 0
                for b in range(bucket - BUCKETS + 1, bucket + 1):
                    c = buckets.get(b)
                    series.append(round(c[0] / c[1] * 100, 1) if c and c[1] else None)
                    if c:
                        up, total = up + c[0], total + c[1]
                out[url] = {"buckets": series, "uptime": round(up / total * 100, 2) if total else None}
        return {"bucketMinutes": BUCKET_SECONDS // 60, "services": out}

    def save(self):
        with self.lock:
            snapshot = {url: {str(b): v for b, v in buckets.items()} for url, buckets in self.data.items()}
        directory = os.path.dirname(self.path)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------- alert state machine

class Alerter:
    """Turns repeated check results into down/up and warn/clear events."""

    def __init__(self):
        self.state = {}  # key -> {"streak": int, "alerted": bool, "since": float}

    def reset(self):
        self.state.clear()

    def evaluate(self, checks, failures):
        """checks: [(key, is_bad, bad_event, good_event)], events are dicts or callables(since)."""
        events, now = [], time.time()
        for key, bad, bad_event, good_event in checks:
            st = self.state.setdefault(key, {"streak": 0, "alerted": False, "since": now})
            if bad:
                st["streak"] += 1
                if st["streak"] == 1:
                    st["since"] = now
                if st["streak"] >= failures and not st["alerted"]:
                    st["alerted"] = True
                    events.append(bad_event)
            else:
                if st["alerted"]:
                    events.append(good_event(now - st["since"]) if callable(good_event) else good_event)
                st.update(streak=0, alerted=False)
        return events


# ---------------------------------------------------------------- sending

def _post_json(url, payload, verify):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (switchboard, 1.0)",  # Discord rejects urllib's default agent
    })
    with urllib.request.urlopen(req, timeout=10, context=None if verify else _insecure_ssl) as res:
        res.read()


def send(events, kind, url, source):
    """Deliver events. Raises on failure; error text never includes the URL."""
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        if kind == "discord":
            for i in range(0, len(events), DISCORD_MAX_EMBEDS):
                _post_json(url, {"username": source, "embeds": [{
                    "title": f"{ICONS[e['kind']]} {e['title']}",
                    "description": e.get("message", ""),
                    "color": COLOURS[e["kind"]],
                    "timestamp": stamp,
                } for e in events[i:i + DISCORD_MAX_EMBEDS]]}, verify=True)
        else:
            _post_json(url, {"source": source, "time": stamp, "events": events}, verify=False)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from the webhook") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"can't reach the webhook: {e.reason}") from None


# ---------------------------------------------------------------- the monitor thread

class Monitor:
    """Hooks are functions from server.py: load_config, collect_status(force),
    collect_widgets(), get_system(), alert_webhook() -> str."""

    def __init__(self, history_path, **hooks):
        self.h = hooks
        self.history = UptimeHistory(history_path)
        self.alerter = Alerter()
        self.status = {"lastSent": None, "lastError": None, "lastEvents": []}
        self._stop = threading.Event()
        self._last_save = time.time()

    def start(self):
        threading.Thread(target=self._run, name="monitor", daemon=True).start()

    def _run(self):
        self._stop.wait(5)  # let the server start first
        while not self._stop.is_set():
            interval = 30
            try:
                interval = max(10, int(self.h["load_config"]().get("statusInterval") or 30))
                self.tick()
            except Exception:  # never let the monitor die
                traceback.print_exc()
            self._stop.wait(interval)

    def tick(self):
        cfg = self.h["load_config"]()
        status = self.h["collect_status"](force=True)
        self.history.record(status)
        if time.time() - self._last_save > HISTORY_SAVE_SECONDS:
            self.history.save()
            self._last_save = time.time()

        ac = cfg.get("alerts") or {}
        if not ac.get("enabled"):
            self.alerter.reset()  # don't send stale "recovered" messages when re-enabled
            return
        events = self.alerter.evaluate(self.checks(cfg, ac, status), max(1, int(ac.get("failures") or 2)))
        if events:
            self.deliver(events, cfg, ac)

    def deliver(self, events, cfg, ac):
        url = self.h["alert_webhook"]()
        if not url:
            self.status["lastError"] = "no webhook URL set"
            return
        try:
            send(events, ac.get("type", "discord"), url, cfg.get("title") or "Switchboard")
            self.status.update(lastSent=time.time(), lastError=None,
                               lastEvents=[e["title"] for e in events][-10:])
        except Exception as e:
            self.status["lastError"] = str(e)
            print(f"[monitor] alert delivery failed: {e}", flush=True)

    def checks(self, cfg, ac, status):
        checks = []
        if ac.get("services", True):
            names = {}
            for t in cfg.get("tabs", []):
                for g in t.get("groups", []):
                    for s in g.get("services", []):
                        url = s.get("statusUrl") or s.get("url")
                        if s.get("check", True) and url:
                            names.setdefault(url, []).append(s.get("name") or url)
            for url, result in status.items():
                if url not in names:
                    continue
                name = " / ".join(dict.fromkeys(names[url]))
                reason = result.get("error") or f"HTTP {result.get('code')}"
                checks.append((
                    f"svc:{url}", not result.get("up"),
                    {"kind": "down", "title": f"{name} is down", "message": f"{reason}\n{url}", "service": name, "url": url},
                    lambda gone, name=name, url=url: {"kind": "up", "title": f"{name} is back up",
                                                      "message": f"Was down for {_duration(gone)}\n{url}",
                                                      "service": name, "url": url},
                ))

        if ac.get("widgets", True):
            results = self.h["collect_widgets"]()
            for t in cfg.get("tabs", []):
                for g in t.get("groups", []):
                    for s in g.get("services", []):
                        r = results.get(s.get("id"))
                        if not r or "fields" not in r:
                            continue  # widget errors are not alerts; "down" covers availability
                        for f in r["fields"]:
                            value = format_value(f.get("value"), f.get("format"))
                            name, label = s.get("name"), f.get("label")
                            checks.append((
                                f"widget:{s['id']}:{label}", bool(f.get("warn")),
                                {"kind": "warn", "title": f"{name}: {label} {value}", "service": name},
                                {"kind": "clear", "title": f"{name}: {label} back to normal ({value})", "service": name},
                            ))

        sysc = cfg.get("system") or {}
        if ac.get("system", True) and sysc.get("enabled"):
            d = self.h["get_system"]()
            host = sysc.get("label") or d.get("hostname") or "Server"
            warn_pct = float(sysc.get("warnPercent") or 90)
            warn_temp = float(sysc.get("warnTemp") or 80)

            def pair(key, bad, warn_title, clear_title):
                checks.append((f"sys:{key}", bad, {"kind": "warn", "title": warn_title, "service": host},
                               {"kind": "clear", "title": clear_title, "service": host}))

            if d.get("temp"):
                c = d["temp"]["c"]
                pair("temp", c >= warn_temp, f"{host}: CPU temperature {round(c)}°C",
                     f"{host}: CPU temperature back to {round(c)}°C")
            if d.get("memory"):
                m = d["memory"]
                pct = (m["total"] - m["available"]) / m["total"] * 100
                pair("ram", pct >= warn_pct, f"{host}: RAM {round(pct)}% used ({format_value(m['available'], 'bytes')} free)",
                     f"{host}: RAM back to {round(pct)}% used")
            for disk in d.get("disks", []):
                if "total" in disk and disk["total"]:
                    pct = disk["used"] / disk["total"] * 100
                    pair(f"disk:{disk['path']}", pct >= warn_pct,
                         f"{host}: {disk['label']} disk {round(pct)}% full ({format_value(disk['free'], 'bytes')} free)",
                         f"{host}: {disk['label']} disk back to {round(pct)}% full")
        return checks
