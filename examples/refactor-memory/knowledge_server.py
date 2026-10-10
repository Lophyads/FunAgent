"""Controlled read-only knowledge endpoint for the P4 CLI example."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import unicodedata

DOCUMENTS = (
    {"record_id": "intro", "type": "document", "content": "FunAgent 支持中文检索。语义知识和情景事实统一由 MemoryMgr 查询。"},
    {"record_id": "history", "type": "document", "content": "召回使用临时消息；原始会话历史只保存主任务的用户输入、工具和完整回复。"},
)


def MakeServer(port=8765):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode("utf8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/query":
                return self._send(404, {"error": "not_found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError
                query = json.loads(self.rfile.read(length))
                if not isinstance(query["text"], str) or type(query["limit"]) is not int or not 1 <= query["limit"] <= 100:
                    raise ValueError
                text = unicodedata.normalize("NFKC", query["text"]).casefold()
                records = [{**r, "citation": f"http://127.0.0.1:{self.server.server_port}/documents/{r['record_id']}"}
                           for r in DOCUMENTS if text in unicodedata.normalize("NFKC", r["content"]).casefold()
                           and (not query.get("types") or r["type"] in query["types"])]
            except (ValueError, KeyError, TypeError):
                return self._send(400, {"error": "invalid_request"})
            self._send(200, {"records": records[:query["limit"]]})

        def do_GET(self):
            record = next((r for r in DOCUMENTS if self.path == "/documents/" + r["record_id"]), None)
            self._send(200 if record else 404, record or {"error": "not_found"})

        def log_message(self, *args):
            pass
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    with MakeServer(parser.parse_args().port) as server:
        print(f"Knowledge query: http://127.0.0.1:{server.server_port}/query", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
