"""HTTP parses and presents data; main and review workers own all model IO."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit
import json
import re

from ..conf import ConfMgr
from ..contracts import DomainError, SessionCreate, TurnRequest, PageQuery, HistoryQuery, thaw
from ..session import SessionMgr
from ..task import TaskMgr
from ..interaction import InteractionMgr


ERRORS = {
    "INVALID_REQUEST": (400, "Invalid request."),
    "NOT_FOUND": (404, "Object or route not found."),
    "METHOD_NOT_ALLOWED": (405, "Method not allowed."),
    "CAPACITY": (429, "Task capacity reached; try again later."),
    "STOPPING": (503, "Application is stopping."),
    "NOT_READY": (503, "Application is not ready."),
    "CONFLICT": (409, "Operation conflicts with current state."),
    "INTERACTION_STALE": (409, "This request is no longer pending for this task."),
    "INTERACTION_EXPIRED": (409, "This request has expired."),
    "FORBIDDEN": (403, "Request origin is not allowed."),
    "PAYLOAD_TOO_LARGE": (413, "Request body is too large."),
    "UNSUPPORTED_MEDIA_TYPE": (415, "Use application/json."),
    "STORAGE": (500, "Storage operation failed."),
    "SUBMIT_FAILED": (500, "Task submission failed."),
    "INTERNAL_ERROR": (500, "Request could not be completed."),
}
RESULT_CODES = frozenset(ERRORS) | {"CANCELLED", "DEADLINE", "INTERRUPTED", "MAX_ITERATIONS",
    "PROFILES_EXHAUSTED", "TIMEOUT", "TRANSPORT", "PROTOCOL", "TRUNCATED", "PROVIDER_FATAL",
    "AUTH", "RATE_LIMIT", "SERVER", "CONFIG_REFERENCE", "INVALID_STRATEGY"}


def _code(value):
    return value if value is None or value in RESULT_CODES else "INTERNAL_ERROR"


def _fields(body, allowed, required=()):
    if not isinstance(body, dict) or body.keys() - set(allowed) or set(required) - body.keys():
        raise DomainError("INVALID_REQUEST", "Invalid fields")


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DomainError("INVALID_REQUEST", "Invalid text")
    return value


def _session(session):
    return {"session_id": session.session_id, "title": session.title, "created_at": session.created_at}


def _message(message):
    return {"message_id": message.message_id, "role": message.role, "content": message.content,
            "calls": [{"call_id": c.call_id, "name": c.name, "args": thaw(c.args)} for c in message.calls],
            "call_id": message.call_id, "status": message.status, "effect": message.effect}


def _page(query, kind):
    if query.keys() - {"limit", "cursor"} or any(len(values) != 1 for values in query.values()):
        raise DomainError("INVALID_REQUEST", "Invalid page query")
    try:
        limit = int(query.get("limit", ["50"])[0])
    except ValueError:
        raise DomainError("INVALID_REQUEST", "Invalid limit") from None
    if not 1 <= limit <= 1000:
        raise DomainError("INVALID_REQUEST", "Invalid limit")
    return kind(limit=limit, cursor=query.get("cursor", [None])[0])


class HTTPController:
    @staticmethod
    def Handle(method, target, body=None):
        if method not in {"GET", "POST"}:
            raise DomainError("METHOD_NOT_ALLOWED", "Invalid method")
        try:
            parsed = urlsplit(target)
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=10)
        except ValueError:
            raise DomainError("INVALID_REQUEST", "Invalid request target") from None
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise DomainError("INVALID_REQUEST", "Invalid request target")
        route = parsed.path
        if method == "POST":
            if parsed.query:
                raise DomainError("INVALID_REQUEST", "Unexpected query")
            _fields(body, body.keys() if isinstance(body, dict) else ())
        if route == "/sessions":
            if method == "POST":
                _fields(body, {"title"})
                title = _text(body.get("title", "New session"), 200)
                workspace = ConfMgr.GetInstance().Serv().Options()["workspace"]
                return 201, _session(SessionMgr.Create(SessionCreate(title, workspace)))
            if method == "GET":
                page = SessionMgr.List(_page(query, PageQuery))
                return 200, {"items": [_session(s) for s in page.items], "next_cursor": page.next_cursor}
            raise DomainError("METHOD_NOT_ALLOWED", "Invalid method")
        match = re.fullmatch(r"/sessions/([A-Za-z0-9_-]{1,128})/(messages|tasks)", route)
        if match:
            identifier, operation = match.groups()
            if operation == "messages" and method == "GET":
                page = SessionMgr.ReadHistory(identifier, _page(query, HistoryQuery))
                return 200, {"items": [_message(m) for m in page.items], "next_cursor": page.next_cursor}
            if operation == "tasks" and method == "POST":
                _fields(body, {"text"}, {"text"})
                task_id = TaskMgr.GetInstance().Submit(TurnRequest(identifier, _text(body["text"], 16000)))
                return 202, {"task_id": task_id}
            raise DomainError("METHOD_NOT_ALLOWED", "Invalid method")
        if query:
            raise DomainError("INVALID_REQUEST", "Unexpected query")
        match = re.fullmatch(r"/tasks/([A-Za-z0-9_-]{1,128})(/(cancel|interactions))?", route)
        if match:
            identifier, _, operation = match.groups()
            manager = TaskMgr.GetInstance()
            if operation is None and method == "GET":
                task = manager.Get(identifier)
                result = None if task.result is None else {
                    "final": task.result.final, "reason": _code(task.result.reason), "error_code": _code(task.result.error_code)}
                return 200, {"task_id": task.task_id, "session_id": task.request.session_id, "status": task.status,
                             "result": result, "created_at": task.created_at, "finished_at": task.finished_at}
            if operation == "cancel" and method == "POST":
                _fields(body, ())
                result = manager.Cancel(identifier)
                return 200, {"task_id": result.task_id, "status": result.status, "requested": result.requested}
            if operation == "interactions" and method == "GET":
                manager.Get(identifier)
                requests = InteractionMgr.GetInstance().Pending(identifier)
                return 200, {"items": [{"interaction_id": r.interaction_id, "task_id": r.task_id,
                    "run_id": r.run_id, "kind": r.kind, "prompt": r.prompt} for r in requests]}
            raise DomainError("METHOD_NOT_ALLOWED", "Invalid method")
        match = re.fullmatch(r"/interactions/([A-Za-z0-9_-]{1,128})/answer", route)
        if match:
            if method != "POST":
                raise DomainError("METHOD_NOT_ALLOWED", "Invalid method")
            _fields(body, {"task_id", "run_id", "answer"}, {"task_id", "run_id", "answer"})
            task_id, run_id = _text(body["task_id"], 128), _text(body["run_id"], 128)
            TaskMgr.GetInstance().Get(task_id)
            InteractionMgr.GetInstance().Resolve(match[1], body["answer"], task_id=task_id, run_id=run_id)
            return 200, {"answered": True}
        raise DomainError("NOT_FOUND", "Route not found")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}


class _Handler(BaseHTTPRequestHandler):
    server_version, sys_version = "FunAgent", ""

    # A stalled/disconnected HTTP client never owns task control or model IO.
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass  # Never log user text, answers, or URLs containing data.

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _handle(self):
        try:
            if self.command == "GET" and self.path in ASSETS:
                name, content_type = ASSETS[self.path]
                self._send(200, (Path(__file__).with_name("web") / name).read_bytes(), content_type)
                return
            body = None
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if origin and origin not in self.server.allowed_origins:
                    raise DomainError("FORBIDDEN", "Invalid origin")
                if self.headers.get_content_type() != "application/json":
                    raise DomainError("UNSUPPORTED_MEDIA_TYPE", "Invalid media type")
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                    raise DomainError("INVALID_REQUEST", "Invalid body length")
                try:
                    length = int(lengths[0])
                except ValueError:
                    raise DomainError("INVALID_REQUEST", "Invalid body length") from None
                if length < 0:
                    raise DomainError("INVALID_REQUEST", "Invalid body length")
                if length > 65536:
                    raise DomainError("PAYLOAD_TOO_LARGE", "Body too large")
                try:
                    body = json.loads(self.rfile.read(length), object_pairs_hook=_json_object,
                                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
                except (ValueError, UnicodeError):
                    raise DomainError("INVALID_REQUEST", "Invalid JSON") from None
            status, payload = HTTPController.Handle(self.command, self.path, body)
            self._send(status, payload)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as error:
            code = error.code if isinstance(error, DomainError) and error.code in ERRORS else "INTERNAL_ERROR"
            status, message = ERRORS[code]
            try:
                self._send(status, {"error": {"code": code, "message": message}})
            except OSError:
                self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _handle


class HttpServer:
    """One listening resource, owned exclusively by the startup entry."""
    def __init__(self, host, port):
        self._server = ThreadingHTTPServer((host, port), _Handler)
        address, bound_port = self._server.server_address
        public_host = "127.0.0.1" if address == "0.0.0.0" else host
        self.url = f"http://{public_host}:{bound_port}"
        self._server.allowed_origins = {self.url}
        if address == "0.0.0.0" or address.startswith("127."):
            self._server.allowed_origins.add(f"http://localhost:{bound_port}")
        try:
            self._thread = Thread(target=lambda: self._server.serve_forever(poll_interval=0.05), name="funagent-http")
        except Exception:
            self._server.server_close()
            raise

    def Start(self):
        self._thread.start()

    def Wait(self):
        while self._thread.is_alive():
            self._thread.join(0.2)

    def Close(self):
        if self._thread.is_alive():
            self._server.shutdown()
            self._thread.join()
        self._server.server_close()  # Wait for bounded request handlers before managers close.
