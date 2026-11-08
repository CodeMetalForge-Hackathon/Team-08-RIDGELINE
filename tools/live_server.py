"""RIDGELINE live demo server. Python standard library only.

    python tools/live_server.py                 # http://localhost:8768/ on this computer only
    python tools/live_server.py --lan           # also reachable from other devices on the network
    python tools/live_server.py --lan --cert cert.pem --key key.pem   # HTTPS, phone sensors need a secure page

Serves the repo root, so these work:
    http://localhost:8768/mockups/phone_screen/    the phone app (live solver when it can load ../../shared/solver.js)
    http://localhost:8768/terrain_viewer/          the big-screen map, polls /state every 0.5 s

API (JSON):
    POST /lock    the phone sends its whole live state after every Lock, undo, re-aim or settings change:
                  {event, session, seq, source, locks: [...], baro, opts, result}. The server keeps the latest one.
    POST /reset   clear the state (the phone also sends event "reset" through /lock when a new fix starts)
    GET  /state   {"seq": n, "received_at": ..., "event": ..., "locks": [...], "result": {...}, "history": [...]}

Nothing leaves this computer. The state lives in memory and is gone when the server stops.
"""
import argparse
import json
import os
import ssl
import sys
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_BODY = 512 * 1024
HISTORY = 40

_lock = threading.Lock()
_state = {"seq": 0, "received_at": None, "event": None, "session": None, "locks": [], "result": None, "history": []}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _store(body, event=None):
    with _lock:
        seq = _state["seq"] + 1
        hist = _state["history"]
        entry = {"seq": seq, "event": event or body.get("event") or "lock", "at": _now(), "locks": len(body.get("locks") or []),
                 "radius_m": ((body.get("result") or {}).get("hdr95") or {}).get("radius_m")}
        hist = (hist + [entry])[-HISTORY:]
        _state.clear()
        _state.update(body)
        _state.update({"seq": seq, "received_at": entry["at"], "event": entry["event"], "history": hist})
        return seq


class Handler(SimpleHTTPRequestHandler):
    # Windows can map .js to text/plain from the registry, which breaks ES modules, so pin the types we serve
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript", ".mjs": "text/javascript",
                      ".json": "application/json", ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml",
                      ".png": "image/png", ".jpg": "image/jpeg", ".woff2": "font/woff2", ".html": "text/html; charset=utf-8"}

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _json(self, obj, status=HTTPStatus.OK):
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/state":
            with _lock:
                return self._json(dict(_state))
        if path == "/health":
            return self._json({"ok": True, "root": os.path.basename(ROOT)})
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/lock", "/reset"):
            return self._json({"ok": False, "error": "unknown endpoint"}, HTTPStatus.NOT_FOUND)
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._json({"ok": False, "error": "body too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return self._json({"ok": False, "error": f"bad JSON: {e}"}, HTTPStatus.BAD_REQUEST)
        if not isinstance(body, dict):
            return self._json({"ok": False, "error": "send a JSON object"}, HTTPStatus.BAD_REQUEST)
        if path == "/reset":
            body = {"locks": [], "result": None, "session": body.get("session")}
            seq = _store(body, "reset")
        else:
            locks = body.get("locks")
            if not isinstance(locks, list) or not all(isinstance(x, dict) for x in locks):
                return self._json({"ok": False, "error": "locks must be a list of objects"}, HTTPStatus.BAD_REQUEST)
            seq = _store(body)
        r = (body.get("result") or {})
        print(f"[{_now()}] {path} #{seq} {body.get('event', 'reset' if path == '/reset' else 'lock')}: "
              f"{len(body.get('locks') or [])} locks, 95% radius {((r.get('hdr95') or {}).get('radius_m'))} m", flush=True)
        return self._json({"ok": True, "seq": seq})

    def log_message(self, fmt, *args):
        if os.environ.get("RIDGELINE_VERBOSE"):
            super().log_message(fmt, *args)


def main():
    ap = argparse.ArgumentParser(description="RIDGELINE live demo server (standard library only)")
    ap.add_argument("--port", type=int, default=8768)
    ap.add_argument("--lan", action="store_true", help="listen on all interfaces, not only this computer")
    ap.add_argument("--cert", help="PEM certificate for HTTPS (phone sensors need a secure page)")
    ap.add_argument("--key", help="PEM private key for HTTPS")
    a = ap.parse_args()
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    if os.name == "nt":
        # on Windows SO_REUSEADDR lets a second server bind the same port and steal requests, so refuse instead
        ThreadingHTTPServer.allow_reuse_address = False
    try:
        httpd = ThreadingHTTPServer((host, a.port), Handler)
    except OSError as e:
        print(f"Cannot listen on port {a.port}: {e}. Is another server running? Try --port 8769.", file=sys.stderr)
        return 2
    scheme = "http"
    if a.cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(a.cert, a.key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    base = f"{scheme}://localhost:{a.port}"
    print(f"RIDGELINE live server, serving {ROOT}", flush=True)
    print(f"  phone   {base}/mockups/phone_screen/", flush=True)
    print(f"  viewer  {base}/terrain_viewer/", flush=True)
    print(f"  state   {base}/state", flush=True)
    if a.lan:
        print("  listening on all interfaces. Phone sensors need HTTPS (--cert/--key) or localhost.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
