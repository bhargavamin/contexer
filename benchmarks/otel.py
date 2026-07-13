"""Tiny OTLP/HTTP JSON receiver: an independent second measurement of each
session's tokens/cost via Claude Code's OpenTelemetry export. Stdlib only."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_TYPES = ("input", "output", "cacheRead", "cacheCreation")


class OtelReceiver:
    def __init__(self):
        self.port = 0
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self.reset()

    def reset(self):
        with getattr(self, "_lock", threading.Lock()):
            self._tokens = dict.fromkeys(_TYPES, 0)
            self._cost = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return {"tokens": dict(self._tokens), "cost_usd": self._cost}

    def _ingest(self, body: bytes):
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        with self._lock:
            for rm in data.get("resourceMetrics", []):
                for sm in rm.get("scopeMetrics", []):
                    for metric in sm.get("metrics", []):
                        pts = metric.get("sum", {}).get("dataPoints", [])
                        if metric.get("name") == "claude_code.token.usage":
                            for p in pts:
                                typ = next((a["value"].get("stringValue")
                                            for a in p.get("attributes", [])
                                            if a.get("key") == "type"), None)
                                if typ in self._tokens:
                                    self._tokens[typ] += int(float(
                                        p.get("asInt", p.get("asDouble", 0))))
                        elif metric.get("name") == "claude_code.cost.usage":
                            for p in pts:
                                self._cost += float(p.get("asDouble", p.get("asInt", 0)))

    def start(self) -> int:
        rx = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/v1/metrics":
                    rx._ingest(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):  # silence request logging
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
