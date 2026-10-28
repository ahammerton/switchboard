"""Host stats on Linux, read from fake /proc and /sys trees, plus the parts that work anywhere."""
import glob
import os
import tempfile
import unittest
from unittest import mock

import sysstats

PROC = {
    "/proc/stat": "cpu  1000 0 500 8000 500 0 0 0 0 0\ncpu0 1 1 1 1\n",
    "/proc/meminfo": "MemTotal:       16303128 kB\nMemFree:         1200000 kB\nMemAvailable:    9800000 kB\n",
    "/proc/uptime": "1036800.55 7000000.00\n",
}
INTEL = {"/sys/class/hwmon/hwmon0/name": "acpitz\n", "/sys/class/hwmon/hwmon0/temp1_input": "27800\n",
         "/sys/class/hwmon/hwmon1/name": "coretemp\n",
         "/sys/class/hwmon/hwmon1/temp1_input": "52000\n", "/sys/class/hwmon/hwmon1/temp1_label": "Package id 0\n",
         "/sys/class/hwmon/hwmon1/temp2_input": "61000\n", "/sys/class/hwmon/hwmon1/temp2_label": "Core 0\n"}
AMD = {"/sys/class/hwmon/hwmon2/name": "k10temp\n",
       "/sys/class/hwmon/hwmon2/temp1_input": "48250\n", "/sys/class/hwmon/hwmon2/temp1_label": "Tctl\n",
       "/sys/class/hwmon/hwmon2/temp3_input": "39000\n", "/sys/class/hwmon/hwmon2/temp3_label": "Tccd1\n"}
PI = {"/sys/class/thermal/thermal_zone0/type": "cpu-thermal\n", "/sys/class/thermal/thermal_zone0/temp": "45622\n"}


class FakeLinux:
    """Points sysstats at a temporary directory laid out like / on Linux."""

    def __init__(self, files):
        self.root = tempfile.mkdtemp()
        self.write(files)
        real_read, real_glob = sysstats._read, glob.glob
        self.patches = [
            mock.patch.object(sysstats, "SYSTEM", "Linux"),
            mock.patch.object(sysstats, "_read", lambda p: real_read(self.root + p)),
            mock.patch.object(sysstats.glob, "glob", lambda pat: [x[len(self.root):] for x in real_glob(self.root + pat)]),
            mock.patch.object(sysstats, "_last_cpu", None),
        ]

    def write(self, files):
        for path, content in files.items():
            full = self.root + path
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(content)

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


class LinuxTests(unittest.TestCase):
    def test_cpu_temperature_sensors(self):
        for files, expected in ((INTEL, (52.0, "coretemp")), (AMD, (48.2, "k10temp")),
                                (PI, (45.6, "cpu-thermal")), ({}, None)):
            with FakeLinux({**PROC, **files}):
                self.assertEqual(sysstats.cpu_temp(), expected)

    def test_memory_and_uptime(self):
        with FakeLinux(PROC):
            self.assertEqual(sysstats.memory(), {"total": 16303128 * 1024, "available": 9800000 * 1024})
            self.assertEqual(sysstats.uptime_seconds(), 1036800)

    def test_cpu_percent_between_two_samples(self):
        with FakeLinux(PROC) as fake:
            sysstats.cpu_percent()  # first sample
            fake.write({"/proc/stat": "cpu  1200 0 600 8600 600 0 0 0 0 0\n"})  # +300 busy, +700 idle
            self.assertEqual(sysstats.cpu_percent(), 30.0)


class AnywhereTests(unittest.TestCase):
    def test_disks(self):
        d = sysstats.disk(tempfile.gettempdir(), "Temp")
        self.assertEqual(d["label"], "Temp")
        self.assertGreater(d["total"], 0)
        self.assertEqual(d["used"], d["total"] - d["free"])
        self.assertIn("error", sysstats.disk("/definitely/not/here", "Missing"))

    def test_collect_shape(self):
        s = sysstats.collect([{"path": tempfile.gettempdir(), "label": "Temp"}])
        self.assertTrue({"hostname", "platform", "cores", "cpu", "temp", "memory", "load", "uptime", "disks"} <= set(s))
        self.assertEqual(s["disks"][0]["label"], "Temp")

    def test_host_name_override(self):
        with mock.patch.dict(os.environ, {"HOST_NAME": "lab-box"}):
            self.assertEqual(sysstats.collect([])["hostname"], "lab-box")


if __name__ == "__main__":
    unittest.main()
