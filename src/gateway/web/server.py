"""Loopback HTTP, static ES modules and per-session server-sent events."""
from __future__ import annotations

import json
import logging
import mimetypes
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .read_model import WebError

logger = logging.getLogger(__name__)
ASSETS = Path(__file__).with_name("static")


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, host, port=7777):
        self.host = host
        self._serving = threading.Event()
        super().__init__(("127.0.0.1", port), RequestHandler)

    def serve_forever(self, poll_interval=0.2):
        self._serving.set()
        try:
            super().serve_forever(poll_interval)
        finally:
            self._serving.clear()

    def stop(self):
        self.host.accepting = False
        if self._serving.is_set():
            self.shutdown()


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, format, *args):
        # Paths/body may contain user content. Keep access logs to status only.
        pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def _json(self, status, value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _validate(self, method):
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed.update({"127.0.0.1", "localhost"})
        host = self.headers.get("Host", "").lower()
        if host not in allowed:
            raise WebError(403, "invalid_host", "Host is not this local service")
        if method != "GET":
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{host}"}:
                raise WebError(403, "invalid_origin", "write requests must be same-origin")
            if self.headers.get("Sec-Fetch-Site") not in {None, "same-origin", "none"}:
                raise WebError(403, "invalid_origin", "write requests must be same-origin")
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise WebError(422, "invalid_input", "JSON Content-Type required")
            if self.headers.get("Transfer-Encoding"):
                raise WebError(422, "invalid_input", "chunked requests not supported")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError()
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError()
                return body
            except (ValueError, UnicodeError):
                raise WebError(422, "invalid_input", "invalid JSON body (max 1 MiB)") from None
        return {}

    def _handle(self, method):
        try:
            body = self._validate(method)
            parsed = urlsplit(self.path)
            path = [unquote(part) for part in parsed.path.strip("/").split("/")]
            query = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
            host = self.server.host
            if method == "GET" and path[:1] != ["api"]:
                return self._static(parsed.path)
            if path[:1] != ["api"]:
                raise WebError(404, "not_found", "route not found")
            if method != "GET" and not host.accepting:
                raise WebError(503, "stopping", "service is stopping")
            if method == "GET":
                if path == ["api", "events"]:
                    sid = query.get("session_id", "")
                    if not sid or len(sid) > 256:
                        raise WebError(422, "invalid_input", "session_id required")
                    return self._sse(sid)
                if path == ["api", "status"]:
                    value = host.status()
                elif path == ["api", "sessions"]:
                    value = host.read.sessions()
                elif len(path) == 4 and path[1] == "sessions" and path[3] == "messages":
                    value = host.read.messages(path[2], query)
                elif len(path) == 3 and path[1] == "turns":
                    value = host.turn(path[2])
                elif path == ["api", "interactions"]:
                    value = host.interactions(query.get("session_id"))
                elif len(path) == 3 and path[1] == "memory":
                    value = host.memory_document(path[2])
                elif path == ["api", "data", "schema"]:
                    value = host.read.schema()
                elif len(path) in {4, 5} and path[1:3] == ["data", "tables"] and (len(path) == 4 or path[4] == "row"):
                    value = host.read.table(path[3], query, detail=len(path) == 5)
                elif len(path) == 2 and path[1] in {"tools", "connections", "models", "config"}:
                    value = host.read.catalog(path[1])
                else:
                    raise WebError(404, "not_found", "route not found")
                return self._json(200, value)
            if method == "POST" and path == ["api", "turns"]:
                return self._json(202, host.submit(body))
            if method == "POST" and len(path) == 4 and path[1] == "turns" and path[3] == "cancel":
                return self._json(200, host.cancel(path[2]))
            if method == "POST" and len(path) == 4 and path[1] == "interactions" and path[3] == "resolve":
                return self._json(200, host.resolve(path[2], body))
            if method == "PATCH" and len(path) == 3 and path[1] == "memory":
                return self._json(200, host.edit_memory(path[2], body))
            raise WebError(404, "not_found", "route not found")
        except WebError as exc:
            self.close_connection = True  # Invalid requests may leave unread bytes.
            self._json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:
            logger.exception("web.request_failed")
            self.close_connection = True
            self._json(500, {"error": {"code": "internal_error", "message": "request failed"}})

    def _static(self, raw):
        relative = unquote(raw).lstrip("/") or "index.html"
        path = (ASSETS / relative).resolve()
        if not path.is_relative_to(ASSETS.resolve()) or not path.is_file():
            raise WebError(404, "not_found", "file not found")
        data = path.read_bytes()
        content_type = "text/javascript" if path.suffix == ".js" else mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, sid):
        hub = self.server.host.events
        events, cursor = hub.connect(sid, self.headers.get("Last-Event-ID"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        while not hub.closed:
            for event in events:
                data = json.dumps(event, ensure_ascii=False, allow_nan=False)
                self.wfile.write(f'id: {hub.epoch}:{event["seq"]}\nevent: update\ndata: {data}\n\n'.encode("utf-8"))
            if not events:
                self.wfile.write(b": heartbeat\n\n")
            self.wfile.flush()
            events, cursor = hub.wait(sid, cursor)
