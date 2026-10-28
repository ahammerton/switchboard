"""Stats for the machine the dashboard runs on (standard library only).

Linux (bare metal, Raspberry Pi, Docker) reads /proc and /sys. In a container,
/proc and /sys show the host's CPU, memory, uptime and temperatures; disks
are only visible if the host paths are mounted into the container.
macOS uses sysctl / vm_stat; CPU temperature isn't available there without
privileged tools, so it's reported as unavailable.
"""
import glob
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time

SYSTEM = platform.system()
_cpu_lock = threading.Lock()
_last_cpu = None  # (monotonic time, idle, total) from /proc/stat


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------- CPU

def _proc_stat():
    line = (_read("/proc/stat") or "").split("\n", 1)[0].split()
    if not line or line[0] != "cpu":
        return None
    v = [int(x) for x in line[1:9]]  # user nice system idle iowait irq softirq steal
    return v[3] + v[4], sum(v)


def cpu_percent():
    """Busy % since the previous call (sampled over 0.3 s on the first call or after a long gap)."""
    global _last_cpu
    if SYSTEM == "Linux":
        with _cpu_lock:
            now, cur = time.monotonic(), _proc_stat()
            if cur is None:
                return None
            prev = _last_cpu
            if prev is None or now - prev[0] > 60:
                time.sleep(0.3)
                prev = (now, *cur)
                now, cur = time.monotonic(), _proc_stat()
            _last_cpu = (now, *cur)
            d_idle, d_total = cur[0] - prev[1], cur[1] - prev[2]
            return round(100 * (1 - d_idle / d_total), 1) if d_total > 0 else 0.0
    if SYSTEM == "Darwin":
        total = sum(float(x) for x in _run("ps", "-A", "-o", "%cpu=").split() if x.replace(".", "", 1).isdigit())
        return round(min(100.0, total / (os.cpu_count() or 1)), 1)
    return None


# Sensors that measure the CPU package, best first (hwmon driver names and labels).
_HWMON_CPU = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal", "scpi_sensors")
_PREFERRED_LABELS = ("Package id 0", "Tdie", "Tctl", "CPU")
_THERMAL_ZONE_TYPES = ("x86_pkg_temp", "cpu", "soc", "acpitz")


def _plausible(c):
    return c is not None and 0 < c < 150


def cpu_temp():
    """(°C, sensor name) or None."""
    if SYSTEM != "Linux":
        return None
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = _read(f"{hw}/name") or ""
        if name not in _HWMON_CPU:
            continue
        temps = []
        for inp in sorted(glob.glob(f"{hw}/temp*_input")):
            raw = _read(inp)
            if raw and raw.lstrip("-").isdigit():
                temps.append((_read(inp.replace("_input", "_label")) or "", int(raw) / 1000))
        for pref in _PREFERRED_LABELS:
            for label, c in temps:
                if label.startswith(pref) and _plausible(c):
                    return round(c, 1), name
        vals = [c for _, c in temps if _plausible(c)]
        if vals:
            return round(max(vals), 1), name
    zones = sorted(glob.glob("/sys/class/thermal/thermal_zone*"))
    for want in _THERMAL_ZONE_TYPES:
        for tz in zones:
            kind = (_read(f"{tz}/type") or "").lower()
            raw = _read(f"{tz}/temp")
            if want in kind and raw and raw.lstrip("-").isdigit() and _plausible(int(raw) / 1000):
                return round(int(raw) / 1000, 1), kind
    return None


# ---------------------------------------------------------------- memory, load, uptime

def memory():
    """{"total", "available"} in bytes, or None."""
    if SYSTEM == "Linux":
        info = {}
        for line in (_read("/proc/meminfo") or "").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                info[key] = int(parts[0]) * 1024
        if "MemTotal" in info:
            avail = info.get("MemAvailable", info.get("MemFree", 0) + info.get("Cached", 0))
            return {"total": info["MemTotal"], "available": avail}
    if SYSTEM == "Darwin":
        total = _run("sysctl", "-n", "hw.memsize").strip()
        vm = _run("vm_stat")
        page = re.search(r"page size of (\d+)", vm)
        if total.isdigit() and page:
            pages = {k.strip(): int(v.strip(" .")) for k, v in re.findall(r"Pages ([^:]+):\s+(\d+)", vm)}
            free = sum(pages.get(k, 0) for k in ("free", "inactive", "speculative", "purgeable"))
            return {"total": int(total), "available": free * int(page.group(1))}
    return None


def load_average():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def uptime_seconds():
    if SYSTEM == "Linux":
        raw = _read("/proc/uptime")
        return int(float(raw.split()[0])) if raw else None
    if SYSTEM == "Darwin":
        m = re.search(r"sec = (\d+)", _run("sysctl", "-n", "kern.boottime"))
        return int(time.time()) - int(m.group(1)) if m else None
    return None


def disk(path, label):
    try:
        u = shutil.disk_usage(path)
    except OSError as e:
        return {"path": path, "label": label, "error": e.strerror or str(e)}
    return {"path": path, "label": label, "total": u.total, "free": u.free, "used": u.total - u.free}


def collect(disks):
    """Everything the stats bar can show. disks: [{"path", "label"}] from the config."""
    mem = memory()
    temp = cpu_temp()
    return {
        "hostname": os.environ.get("HOST_NAME") or socket.gethostname().split(".")[0],
        "platform": f"{SYSTEM} {platform.machine()}",
        "cores": os.cpu_count(),
        "cpu": cpu_percent(),
        "temp": {"c": temp[0], "sensor": temp[1]} if temp else None,
        "memory": mem,
        "load": load_average(),
        "uptime": uptime_seconds(),
        "disks": [disk(d.get("path") or "/", d.get("label") or d.get("path") or "/") for d in disks],
    }
