"""HTTP parses and presents data; main and review workers own all model IO."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread, Event
from urllib.parse import parse_qs, urlsplit
import json
import re
import socket
import time

from ..frame.conf_mgr import ConfMgr
from ..frame.contracts import DomainError, SessionCreate, TurnRequest, PageQuery, HistoryQuery, thaw
from ..frame.session_mgr import SessionMgr
from ..task.task_mgr import TaskMgr
from ..frame.interaction_mgr import InteractionMgr
from ..frame.clarification import QuestionData
from ..frame.event_mgr import EventMgr
from .request_validation import Fields as _fields, Text as _text
from . import memory_routes, diagnostic_service, data_routes, trace_routes
from ..frame.web_projection import TaskView


ERRORS = {
    "INVALID_REQUEST": (400, "Invalid request."),
    "NOT_FOUND": (404, "Object or route not found."),
    "METHOD_NOT_ALLOWED": (405, "Method not allowed."),
    "CAPACITY": (429, "Task capacity reached; try again later."),
    "STOPPING": (503, "Application is stopping."),
    "NOT_READY": (503, "Application is not ready."),
    "DENIED": (403, "Memory access denied."),
    "UNSUPPORTED": (400, "Operation is unsupported."),
    "MANAGEMENT_STALE": (409, "This management change is no longer pending."),
    "MANAGEMENT_EXPIRED": (409, "This management change has expired."),
    "MEMORY_STORAGE": (500, "Memory operation failed."),
    "EMBEDDING_SOURCE": (503, "文档向量化失败，尚未写入。请检查 embedding 服务、模型与超时配置。"),
    "EMBEDDING_PROTOCOL": (502, "向量化服务返回的数据无效，尚未写入。请检查 embedding 模型和向量维度。"),
    "CONFIG_CREDENTIAL": (401, "记忆服务认证失败，请检查服务端凭据配置。"),
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
    "AUTH", "MODEL_NOT_FOUND", "EMPTY_REPLY", "RATE_LIMIT", "SERVER", "CONFIG_REFERENCE", "INVALID_STRATEGY", "UNSUPPORTED"}


def _code(value):
    return value if value is None or value in RESULT_CODES else "INTERNAL_ERROR"


def _session(session):
    return {"session_id": session.session_id, "title": session.title, "created_at": session.created_at}


def _message(message):
    return {"message_id": message.message_id, "task_id": message.task_id, "role": message.role, "content": message.content,
            "calls": [{"call_id": c.call_id, "name": c.name, "args": thaw(c.args)} for c in message.calls],
            "call_id": message.call_id, "status": message.status, "effect": message.effect}


def _page(query, kind):
    allowed = {"limit", "cursor", "direction"} if kind is HistoryQuery else {"limit", "cursor"}
    if query.keys() - allowed or any(len(values) != 1 for values in query.values()):
        raise DomainError("INVALID_REQUEST", "Invalid page query")
    try:
        limit = int(query.get("limit", ["50"])[0])
    except ValueError:
        raise DomainError("INVALID_REQUEST", "Invalid limit") from None
    if not 1 <= limit <= 1000:
        raise DomainError("INVALID_REQUEST", "Invalid limit")
    options = {"direction": query.get("direction", ["forward"])[0]} if kind is HistoryQuery else {}
    return kind(limit=limit, cursor=query.get("cursor", [None])[0], **options)


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
        if route.startswith("/diagnostics/"):
            if query:
                raise DomainError("INVALID_REQUEST", "Unexpected query")
            if method != "GET":
                raise DomainError("METHOD_NOT_ALLOWED", "Invalid method")
            return 200, diagnostic_service.Read(route.removeprefix("/diagnostics/"))
        if route.startswith("/memory/"):
            return memory_routes.Handle(method, route, query, body)
        if route.startswith("/data/"):
            return data_routes.Handle(method, route, query)
        if route in {'/traces', '/trace-sessions'} or re.fullmatch(r'/tasks/[^/]+/trace(?:/[^/]+)?', route):
            return trace_routes.Handle(method, route, query)
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
                _fields(body, {"text", "request_key"}, {"text"})
                key = _text(body["request_key"], 128) if "request_key" in body else None
                task_id = TaskMgr.GetInstance().Submit(TurnRequest(identifier, _text(body["text"], 16000), key))
                return 202, {"task_id": task_id}
            if operation == "tasks" and method == "GET":
                page = TaskMgr.GetInstance().List(identifier, _page(query, PageQuery))
                return 200, {"items": [TaskView(t) for t in page.items], "next_cursor": page.next_cursor}
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
                    "run_id": r.run_id, "kind": r.kind, "prompt": r.prompt, "expires_at": time.time() + max(0, r.expires_at - time.monotonic()),
                    "questions": [QuestionData(q) for q in r.questions],
                    "approval": None if r.approval is None else {"call_id": r.approval.call_id,
                        "tool_name": r.approval.tool_name, "action": r.approval.action,
                        "arguments_digest": r.approval.arguments_digest}} for r in requests]}
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


ASSET_NAMES = ("index.html", "app.js", "api.js", "store.js", "style.css", "tokens.css", "overview.css", "observability.css", "favicon.svg",
               "components/chat.js", "components/ui.js", "pages/catalog.js", "pages/data.js", "pages/memory.js",
               "pages/overview.js", "pages/overview-model.js", "pages/observability.js",
               "pages/observability-model.js", "components/trace-detail.js")
ASSETS = {"/" + name: (name, {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                              ".svg": "image/svg+xml"}[Path(name).suffix] + "; charset=utf-8") for name in ASSET_NAMES}
ASSETS["/"] = ASSETS["/index.html"]


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
        body_consumed = False
        try:
            session_match = re.fullmatch(r'/sessions/([A-Za-z0-9_-]{1,128})/events', self.path)
            if session_match:
                if self.command != 'GET':
                    raise DomainError('METHOD_NOT_ALLOWED', 'Invalid method')
                self._session_sse(session_match[1])
                return
            match = re.fullmatch(r'/tasks/([A-Za-z0-9_-]{1,128})/events', self.path)
            if match:
                if self.command != 'GET':
                    raise DomainError('METHOD_NOT_ALLOWED', 'Invalid method')
                self._sse(match[1])
                return
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
                    raw = self.rfile.read(length)
                    body_consumed = True
                    body = json.loads(raw, object_pairs_hook=_json_object,
                                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
                except (ValueError, UnicodeError):
                    raise DomainError("INVALID_REQUEST", "Invalid JSON") from None
            status, payload = HTTPController.Handle(self.command, self.path, body)
            if isinstance(payload, trace_routes.TraceDownload):
                self._send(status, payload.data, payload.content_type)
            else:
                self._send(status, payload)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as error:
            code = error.code if isinstance(error, DomainError) and error.code in ERRORS else "INTERNAL_ERROR"
            status, message = ERRORS[code]
            try:
                self._send(status, {"error": {"code": code, "message": message}})
                if self.command == "POST" and not body_consumed:
                    self._drain_rejected_body()
            except OSError:
                self.close_connection = True

    def _sse(self, task_id):
        HTTPController.Handle('GET', f'/tasks/{task_id}')
        values = self.headers.get_all('Last-Event-ID', [])
        after = None
        if values:
            match = re.fullmatch(re.escape(task_id) + r':([0-9]{1,20})', values[0])
            if len(values) != 1 or not match:
                raise DomainError('INVALID_REQUEST', 'Invalid event cursor')
            after = int(match[1])
        reader = EventMgr.GetInstance()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.end_headers()
        subscription = reader.Subscribe(task_id, after)
        try:
            for event in subscription:
                if self.server.sse_stopped.is_set():
                    break
                _, task = HTTPController.Handle('GET', f'/tasks/{task_id}')
                kind, payload = event.kind, thaw(event.payload)
                if task['status'] not in {'queued', 'running'}:
                    kind, payload = 'task_committed', {'task': task}
                elif kind == 'stream_reset':
                    payload['task'] = task
                data = {'task_id': task_id, 'run_id': event.run_id, 'purpose': 'main',
                        'iteration': event.iteration, 'attempt_id': event.attempt_id, **payload}
                wire = (f'id: {task_id}:{event.seq}\n' if event.seq else '')
                wire += f'event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'
                self.wfile.write(wire.encode('utf8'))
                self.wfile.flush()
                if kind == 'task_committed':
                    break
        finally:
            subscription.close()
            self.close_connection = True

    def _session_sse(self, sid):
        manager, reader = TaskMgr.GetInstance(), EventMgr.GetInstance()
        values = self.headers.get_all('Last-Event-ID', [])
        cursor = values[0] if values else None
        if values and (len(values) != 1 or not re.fullmatch(re.escape(sid) + r':[a-f0-9]{32}:[0-9]{1,20}', cursor)):
            raise DomainError('INVALID_REQUEST', 'Invalid session event cursor')
        tasks = manager.List(sid, PageQuery(limit=200)).items
        reader.Reconcile(tasks)
        signature = tuple((t.task_id, t.status, t.finished_at) for t in tasks)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.end_headers()
        subscription = reader.SubscribeSession(sid, cursor)
        try:
            for event in subscription:
                if self.server.sse_stopped.is_set():
                    break
                if event['kind'] == 'heartbeat':
                    tasks = manager.List(sid, PageQuery(limit=200)).items
                    current = tuple((t.task_id, t.status, t.finished_at) for t in tasks)
                    if current == signature:
                        self.wfile.write(b': keep-alive\n\n')
                        self.wfile.flush()
                        continue
                    signature = current
                    reader.Reconcile(tasks)
                    event = reader.Snapshot(sid)
                elif event['kind'] == 'turn.finished':
                    task = manager.Get(event['turn_id'])
                    reader.Reconcile((task,))
                    event = {**event, 'payload': TaskView(task)['result'] or {'status': task.status}}
                wire = f"id: {sid}:{reader.epoch}:{event['seq']}\nevent: update\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                self.wfile.write(wire.encode('utf8'))
                self.wfile.flush()
        finally:
            subscription.close()
            self.close_connection = True

    def _drain_rejected_body(self):
        # Flush the rejection before draining. Closing a socket with unread
        # request data can reset the connection and discard that response.
        # Both bytes and total time are bounded even for a stalled sender.
        try:
            remaining = min(max(0, int(self.headers.get("Content-Length", "0"))), 65537)
            if not remaining:
                return
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + 0.1
            while remaining and time.monotonic() < deadline:
                self.connection.settimeout(max(0.001, deadline - time.monotonic()))
                chunk = self.rfile.read1(min(remaining, 8192))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _handle


class HttpServer:
    """One listening resource, owned exclusively by the startup entry."""
    def __init__(self, host, port):
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._server.daemon_threads = False  # server_close must drain real request borrowers.
        self._server.sse_stopped = Event()
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
        self._server.sse_stopped.set()
        if self._thread.is_alive():
            self._server.shutdown()
            self._thread.join()
        self._server.server_close()  # Wait for bounded request handlers before managers close.
