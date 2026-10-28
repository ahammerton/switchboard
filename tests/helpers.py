"""Shared test helpers: mock HTTP apps on free ports, and the real server in a temp folder.

Run the suite from the repository root:  python -m unittest discover -s tests -t .
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DROP = object()  # return this from a handler to close the connection without replying


class Request:
    def __init__(self, handler, method):
        u = urlsplit(handler.path)
        self.method = method
        self.path = u.path
        self.query = parse_qs(u.query, keep_blank_values=True)
        self.headers = handler.headers
        n = int(handler.headers.get("Content-Length") or 0)
        self.body = handler.rfile.read(n) if n else b""

    def q(self, key, default=None):
        return self.query.get(key, [default])[0]

    def json(self):
        return json.loads(self.body or b"null")

    def form(self):
        return {k: v[0] for k, v in parse_qs(self.body.decode()).items()}


class MockApp:
    """An HTTP server on a free local port. `handle(req)` returns one of:
    None (404), a dict/list/str/bytes body (200), (status, body) or
    (status, body, headers), or DROP."""

    def __init__(self, handle):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self, method):
                try:
                    res = handle(Request(self, method))
                except Exception as e:  # a bug in a mock shows up as a 500
                    res = (500, {"error": repr(e)})
                if res is DROP:
                    self.close_connection = True
                    return
                if res is None:
                    res = (404, {"error": "not found"})
                if not isinstance(res, tuple):
                    res = (200, res)
                status, body = res[0], res[1]
                headers = dict(res[2]) if len(res) > 2 else {}
                if isinstance(body, bytes):
                    raw, ctype = body, "text/html"
                elif isinstance(body, str):
                    raw, ctype = body.encode(), "text/html"
                else:
                    raw, ctype = json.dumps(body).encode(), "application/json"
                self.send_response(status)
                self.send_header("Content-Type", headers.pop("Content-Type", ctype))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

            def do_PUT(self):
                self._serve("PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Switchboard:
    """The real server.py in a subprocess, with its own temporary config folder."""

    def __init__(self, env=None, config=None, secrets=None):
        self.dir = tempfile.mkdtemp(prefix="switchboard-test-")
        if config is not None:
            with open(os.path.join(self.dir, "config.json"), "w") as f:
                json.dump(config, f)
        if secrets is not None:
            with open(os.path.join(self.dir, "secrets.json"), "w") as f:
                json.dump(secrets, f)
        self.port = free_port()
        e = {k: v for k, v in os.environ.items() if k not in ("EDIT_TOKEN", "ALLOWED_HOSTS")}
        e.update({"CONFIG_DIR": self.dir, "PORT": str(self.port), "HOST": "127.0.0.1", "QUIET": "1"})
        e.update(env or {})
        self.log = open(os.path.join(self.dir, "server.log"), "w")
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=e,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 15
        while True:
            try:
                if self.request("GET", "/api/meta")[0] == 200:
                    break
            except OSError:
                pass
            if time.time() > deadline or self.proc.poll() is not None:
                raise RuntimeError("server didn't start:\n" + self.read_log())
            time.sleep(0.1)
        token_file = os.path.join(self.dir, "edit-token")
        self.token = ""
        if os.path.exists(token_file):
            with open(token_file) as f:
                self.token = f.read().strip()

    def read_log(self):
        self.log.flush()
        with open(os.path.join(self.dir, "server.log")) as f:
            return f.read()

    def request(self, method, path, body=None, headers=None, token=None, host=None):
        """(status, parsed JSON or raw bytes, headers). Dicts/lists are sent as JSON."""
        hdrs = dict(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        if token:
            hdrs["X-Edit-Token"] = token
        if host:
            hdrs["Host"] = host
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            res = conn.getresponse()
            raw = res.read()
            try:
                data = json.loads(raw)
            except ValueError:
                data = raw
            return res.status, data, res.headers
        finally:
            conn.close()

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        shutil.rmtree(self.dir, ignore_errors=True)
