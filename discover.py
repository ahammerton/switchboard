"""Find services: probe common homelab ports on chosen hosts and identify the apps.

Hosts come from the hosts of services already on the dashboard, the running
guests of Proxmox services that have a widget (read-only API token), and any
hosts or subnet the user types in. Every host is resolved to its IP and each
machine is scanned once; only private addresses are scanned, never public
sites. For every open port the app's front page is fetched once (GET /,
redirects followed) and recognised by its <title> or response headers.
Nothing is changed on the scanned machines.
"""
import concurrent.futures
import html
import ipaddress
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

import widgets

CONNECT_TIMEOUT = 0.6
HTTP_TIMEOUT = 4
MAX_HOSTS = 512
MAX_BODY = 64 * 1024

# Common homelab ports. HTTPS is tried first on the ones that normally use it.
PORTS = [80, 443, 1880, 2283, 2342, 3000, 3001, 3579, 4533, 5000, 5001, 5055, 5380, 6767, 6789, 7575, 7878,
         8000, 8006, 8080, 8081, 8083, 8085, 8090, 8096, 8112, 8123, 8181, 8191, 8200, 8282, 8384, 8443,
         8686, 8787, 8920, 8989, 9000, 9090, 9091, 9117, 9443, 9696, 9925, 13378, 19999, 25600, 32400, 61208]
TLS_FIRST = {443, 8006, 8443, 8920, 9443}

# (title substring, app id, name, icon, widget type). First match wins, so more specific names come first.
APPS = [
    ("jellyseerr", "jellyseerr", "Jellyseerr", "di:jellyseerr", "seerr"),
    ("overseerr", "overseerr", "Overseerr", "di:overseerr", "seerr"),
    ("seerr", "seerr", "Seerr", "di:jellyseerr", "seerr"),
    ("sonarr", "sonarr", "Sonarr", "di:sonarr", "sonarr"),
    ("radarr", "radarr", "Radarr", "di:radarr", "radarr"),
    ("lidarr", "lidarr", "Lidarr", "di:lidarr", "lidarr"),
    ("readarr", "readarr", "Readarr", "di:readarr", "readarr"),
    ("prowlarr", "prowlarr", "Prowlarr", "di:prowlarr", "prowlarr"),
    ("whisparr", "whisparr", "Whisparr", "di:whisparr", None),
    ("bazarr", "bazarr", "Bazarr", "di:bazarr", "bazarr"),
    ("magarr", "magarr", "Magarr", "📰", "magarr"),
    ("jellyfin", "jellyfin", "Jellyfin", "di:jellyfin", "jellyfin"),
    ("emby", "emby", "Emby", "di:emby", "emby"),
    ("tautulli", "tautulli", "Tautulli", "di:tautulli", "tautulli"),
    ("qbittorrent", "qbittorrent", "qBittorrent", "di:qbittorrent", "qbittorrent"),
    ("sabnzbd", "sabnzbd", "SABnzbd", "di:sabnzbd", "sabnzbd"),
    ("nzbget", "nzbget", "NZBGet", "di:nzbget", "nzbget"),
    ("deluge", "deluge", "Deluge", "di:deluge", None),
    ("transmission", "transmission", "Transmission", "di:transmission", None),
    ("proxmox backup", "pbs", "Proxmox Backup Server", "di:proxmox", None),
    ("proxmox", "proxmox", "Proxmox", "di:proxmox", "proxmox"),
    ("portainer", "portainer", "Portainer", "di:portainer", "portainer"),
    ("dockge", "dockge", "Dockge", "di:dockge", "dockge"),
    ("technitium", "technitium", "Technitium DNS", "di:technitium", "technitium"),
    ("forgejo", "forgejo", "Forgejo", "di:forgejo", "gitea"),
    ("gitea", "gitea", "Gitea", "di:gitea", "gitea"),
    ("home assistant", "homeassistant", "Home Assistant", "di:home-assistant", "homeassistant"),
    ("immich", "immich", "Immich", "di:immich", "immich"),
    ("mealie", "mealie", "Mealie", "di:mealie", "mealie"),
    ("komga", "komga", "Komga", "di:komga", "komga"),
    ("bookorbit", "bookorbit", "BookOrbit", "📚", "bookorbit"),
    ("pi-hole", "pihole", "Pi-hole", "di:pi-hole", "pihole"),
    ("adguard", "adguard", "AdGuard Home", "di:adguard-home", "adguard"),
    ("unifi", "unifi", "UniFi", "di:unifi", "unifi"),
    ("grafana", "grafana", "Grafana", "di:grafana", None),
    ("uptime kuma", "uptimekuma", "Uptime Kuma", "di:uptime-kuma", None),
    ("vaultwarden", "vaultwarden", "Vaultwarden", "di:vaultwarden", None),
    ("nextcloud", "nextcloud", "Nextcloud", "di:nextcloud", None),
    ("paperless", "paperless", "Paperless-ngx", "di:paperless-ngx", None),
    ("audiobookshelf", "audiobookshelf", "Audiobookshelf", "di:audiobookshelf", None),
    ("navidrome", "navidrome", "Navidrome", "di:navidrome", None),
    ("syncthing", "syncthing", "Syncthing", "di:syncthing", None),
    ("node-red", "nodered", "Node-RED", "di:node-red", None),
    ("frigate", "frigate", "Frigate", "di:frigate", None),
    ("homarr", "homarr", "Homarr", "di:homarr", None),
    ("jackett", "jackett", "Jackett", "di:jackett", None),
    ("ombi", "ombi", "Ombi", "di:ombi", None),
    ("photoprism", "photoprism", "PhotoPrism", "di:photoprism", None),
    ("netdata", "netdata", "Netdata", "di:netdata", None),
    ("glances", "glances", "Glances", "di:glances", None),
    ("cockpit", "cockpit", "Cockpit", "di:cockpit", None),
    ("truenas", "truenas", "TrueNAS", "di:truenas", None),
    ("openmediavault", "omv", "openmediavault", "di:openmediavault", None),
    ("synology", "synology", "Synology DSM", "di:synology", None),
    ("stirling", "stirling", "Stirling PDF", "di:stirling-pdf", None),
    ("code-server", "codeserver", "code-server", "di:code-server", None),
]
# Default pages that aren't worth listing.
BORING_TITLES = ("welcome to nginx", "404", "not found", "index of", "default page", "it works", "redirecting",
                 "403 forbidden", "401", "bad request", "error")

TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)

_ssl = ssl.create_default_context()
_ssl.check_hostname = False
_ssl.verify_mode = ssl.CERT_NONE
_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl))


def _is_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def _get(url):
    """(status, headers, body, final URL) or None if nothing answered over this scheme."""
    req = urllib.request.Request(url, headers={"User-Agent": "switchboard (service discovery)", "Accept": "*/*"})
    try:
        with _opener.open(req, timeout=HTTP_TIMEOUT) as res:
            return res.status, res.headers, res.read(MAX_BODY), res.geturl()
    except urllib.error.HTTPError as e:  # 401/403 pages still identify the app
        try:
            body = e.read(MAX_BODY)
        except OSError:
            body = b""
        finally:
            e.close()
        return e.code, e.headers, body, url
    except Exception:  # TLS on a plain port, resets, timeouts…
        return None


def identify(host, port):
    """What runs on host:port, as a candidate dict, or None."""
    shown_host = f"[{host}]" if ":" in host else host
    schemes = ("https", "http") if port in TLS_FIRST else ("http", "https")
    for scheme in schemes:
        base = f"{scheme}://{shown_host}" + ("" if (scheme, port) in (("http", 80), ("https", 443)) else f":{port}")
        got = _get(base + "/")
        if not got:
            continue
        status, headers, body, final = got
        title_match = TITLE_RE.search(body or b"")
        title = html.unescape(title_match.group(1).decode("utf-8", "replace")).strip() if title_match else ""
        title = re.sub(r"\s+", " ", title)
        low = title.lower()
        url = base

        # Apps that don't put their name in a <title> at "/".
        if headers and (headers.get("X-Plex-Protocol") or b"<MediaContainer" in (body or b"")[:400]):
            return _candidate("plex", "Plex", "di:plex", "plex", base + "/web", host, port)
        if b"FlareSolverr is ready" in (body or b""):
            return _candidate("flaresolverr", "FlareSolverr", "di:flaresolverr", None, url, host, port)

        for needle, app_id, name, icon, widget in APPS:
            if needle in low:
                if app_id == "pihole":
                    url = base + "/admin"
                return _candidate(app_id, name, icon, widget, url, host, port)

        if title and len(title) <= 60 and not any(b in low for b in BORING_TITLES):
            name = re.split(r"\s+[-|–·:]\s+", title)[0].strip() or title
            return _candidate(None, name, None, None, url, host, port, title=title)
        return None  # answered, but nothing recognisable
    return None


def _candidate(app_id, name, icon, widget, url, host, port, title=None):
    return {"app": app_id, "name": name, "icon": icon, "widget": widget, "url": url,
            "host": host, "port": port, "title": title}


# ---------------------------------------------------------------- where to look

def parse_hosts(text):
    """Hosts, IPs and CIDR ranges (at most a /24) separated by spaces, commas or new lines."""
    hosts, errors = [], []
    for item in re.split(r"[\s,;]+", text or ""):
        if not item:
            continue
        if "/" in item:
            try:
                net = ipaddress.ip_network(item, strict=False)
            except ValueError:
                errors.append(f"“{item}” isn't a valid range")
                continue
            if net.num_addresses > 256:
                errors.append(f"{item} is too big: scan a /24 or smaller")
                continue
            hosts.extend(str(ip) for ip in (net.hosts() if net.num_addresses > 2 else net))
        else:
            hosts.append(item.strip("[]"))
    return hosts, errors


def resolve(host):
    """IPv4 address for a host name or IP, or None."""
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except (OSError, UnicodeError, IndexError):
        return None


def is_local(ip):
    """Private LAN, loopback or link-local: the only addresses we ever scan."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (a.is_private or a.is_loopback or a.is_link_local) and not (a.is_multicast or a.is_unspecified)


def _resolve_all(hosts):
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        return dict(zip(hosts, pool.map(resolve, hosts)))


def config_hosts(cfg):
    out = {}
    for t in cfg.get("tabs", []):
        for g in t.get("groups", []):
            for s in g.get("services", []):
                for u in (s.get("url"), s.get("statusUrl")):
                    host = urllib.parse.urlsplit(u or "").hostname
                    if host:
                        out.setdefault(host, s.get("name") or host)
    return out


def existing_endpoints(cfg):
    """(host, port) pairs already on the dashboard, so they aren't offered again."""
    seen = set()
    for t in cfg.get("tabs", []):
        for g in t.get("groups", []):
            for s in g.get("services", []):
                for u in (s.get("url"), s.get("statusUrl"), (s.get("widget") or {}).get("url")):
                    p = urllib.parse.urlsplit(u or "")
                    if p.hostname:
                        try:
                            port = p.port or (443 if p.scheme == "https" else 80)
                        except ValueError:
                            continue
                        seen.add((p.hostname.lower(), port))
    return seen


def already_listed(cfg, candidate):
    """Name of a dashboard service that is probably this app (e.g. the same app behind a reverse proxy)."""
    name = candidate["name"].lower()
    for t in cfg.get("tabs", []):
        for g in t.get("groups", []):
            for s in g.get("services", []):
                sname = (s.get("name") or "").lower()
                if candidate["widget"] and (s.get("widget") or {}).get("type") == candidate["widget"]:
                    return s.get("name")
                if candidate["app"] and sname and (name in sname or sname in name):
                    return s.get("name")
    return None


def proxmox_guests(cfg, secrets):
    """{ip: "Proxmox: <guest name>"} for running VMs/containers of Proxmox services with a widget."""
    found, notes = {}, []
    for t in cfg.get("tabs", []):
        for g in t.get("groups", []):
            for s in g.get("services", []):
                w = s.get("widget") or {}
                if w.get("type") != "proxmox" or not s.get("id"):
                    continue
                sec = secrets.get(s["id"], {})
                try:
                    ctx = widgets.Ctx("proxmox", widgets.api_base(w, s.get("url")), w.get("options"), sec)
                    auth = {"Authorization": f"PVEAPIToken={ctx.need('tokenId', 'API token ID')}={ctx.need('tokenSecret', 'API token secret')}"}
                    guests = ctx.json("/api2/json/cluster/resources", headers=auth, params={"type": "vm"}).get("data", [])
                except widgets.WidgetError as e:
                    notes.append(f"{s.get('name')}: {e}")
                    continue
                for gst in guests:
                    if gst.get("status") != "running" or gst.get("template"):
                        continue
                    node, vmid, kind = gst.get("node"), gst.get("vmid"), gst.get("type")
                    label = f"{s.get('name')}: {gst.get('name') or vmid}"
                    try:
                        if kind == "lxc":
                            ifaces = ctx.json(f"/api2/json/nodes/{node}/lxc/{vmid}/interfaces", headers=auth).get("data") or []
                            addrs = [i.get("inet", "").split("/")[0] for i in ifaces]
                        else:  # needs the QEMU guest agent inside the VM
                            res = ctx.json(f"/api2/json/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces",
                                           headers=auth).get("data", {}).get("result", [])
                            addrs = [a.get("ip-address", "") for i in res for a in i.get("ip-addresses", [])
                                     if a.get("ip-address-type") == "ipv4"]
                    except widgets.WidgetError:
                        continue  # no agent / no permission for this guest: skip it
                    for ip in addrs:
                        if ip and not ip.startswith("127."):
                            found.setdefault(ip, label)
    return found, notes


# ---------------------------------------------------------------- scan

def scan(cfg, secrets, use_config_hosts=True, use_proxmox=True, extra="", self_endpoints=()):
    started = time.monotonic()
    labels, notes = {}, []  # host as given -> where it came from
    if use_config_hosts:
        for host, name in config_hosts(cfg).items():
            labels.setdefault(host, f"host of {name}")
    if use_proxmox:
        guests, pnotes = proxmox_guests(cfg, secrets)
        notes += pnotes
        for ip, label in guests.items():
            labels.setdefault(ip, label)
    extra_hosts, errors = parse_hosts(extra)
    notes += errors
    for h in extra_hosts:
        labels.setdefault(h, "added by you")

    # Scan machines, not names: many hostnames often point at one reverse proxy.
    existing = existing_endpoints(cfg)
    names = {h for h, _ in existing} | {h for h, _ in self_endpoints} | set(labels)
    ips = _resolve_all(sorted(names))
    machines, public, unresolved = {}, [], []
    for host, label in labels.items():
        ip = ips.get(host)
        if not ip:
            unresolved.append(host)
        elif not is_local(ip):
            public.append(host)
        else:
            machines.setdefault(ip, label)
    if public:
        notes.append(f"skipped {len(public)} public site{'s' if len(public) > 1 else ''} "
                     f"({', '.join(sorted(public)[:3])}{'…' if len(public) > 3 else ''}); only your own network is scanned")
    if unresolved:
        notes.append(f"couldn't resolve {', '.join(sorted(unresolved)[:3])}{'…' if len(unresolved) > 3 else ''}")
    if use_config_hosts and not extra_hosts and len(config_hosts(cfg)) >= 5 and len(machines) <= 2:
        notes.append("most of your services share one address (probably a reverse proxy), so apps are best "
                     "found on the machines they run on: add those hosts or your subnet, e.g. 192.168.1.0/24")
    hosts = list(machines)[:MAX_HOSTS]
    if len(machines) > MAX_HOSTS:
        notes.append(f"only the first {MAX_HOSTS} hosts were scanned")

    skip = {(ips.get(h) or h, p) for h, p in existing | set(self_endpoints)}
    pairs = [(h, p) for h in hosts for p in PORTS if (h, p) not in skip]
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as pool:
        open_pairs = [pair for pair, ok in zip(pairs, pool.map(lambda hp: _is_open(*hp), pairs)) if ok]
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        found = [c for c in pool.map(lambda hp: identify(*hp), open_pairs) if c]

    candidates, seen = [], set()
    for c in found:
        key = (c["host"], c["app"] or c["name"])  # same app on http and https: keep one
        if key in seen:
            continue
        seen.add(key)
        c["source"] = machines.get(c["host"], "")
        c["already"] = already_listed(cfg, c)
        candidates.append(c)
    candidates.sort(key=lambda c: (c["already"] is not None, c["app"] is None, c["name"].lower(), c["host"]))
    return {"candidates": candidates, "hosts": len(hosts), "ports": len(PORTS),
            "openPorts": len(open_pairs), "seconds": round(time.monotonic() - started, 1), "notes": notes}
