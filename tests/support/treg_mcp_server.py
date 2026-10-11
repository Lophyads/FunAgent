"""Controlled HTTP MCP provider; never connects to external services."""
import asyncio
import json
from pathlib import Path
import sys

from mcp.server.fastmcp import FastMCP
import uvicorn


port, audit = int(sys.argv[1]), Path(sys.argv[2])
server = FastMCP("offline-treg", host="127.0.0.1", port=port, streamable_http_path="/mcp/", json_response=True)


def record(name, **args):
    with audit.open("a", encoding="utf8") as handle:
        handle.write(json.dumps({"name": name, **args}) + "\n")


@server.tool()
def catalog_get(endpoint_id: str) -> dict:
    record("catalog_get", endpoint_id=endpoint_id)
    return {"endpoint": {"id": endpoint_id, "method": "GET" if endpoint_id.endswith("search") else "POST",
                         "cost": {"type": "free", "value": 0, "usd": 0, "currency": "USD"}},
            "usd_per_call": 0}


@server.tool()
async def call(endpoint_id: str, params: dict, method: str) -> dict:
    record("call", endpoint_id=endpoint_id, params=params, method=method)
    if params.get("query") == "__slow__":
        try:
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            record("cancelled", endpoint_id=endpoint_id)
            raise
    if endpoint_id == "tinyfish.web.search":
        body = {"results": [{"title": "Python", "url": "https://www.python.org/", "snippet": "Official site"}],
                "total_results": 1}
    elif endpoint_id == "tinyfish.web.fetch":
        body = {"results": [{"url": url, "final_url": url, "text": "Example domain正文", "format": "markdown"}
                            for url in params["urls"]], "errors": []}
    else:
        raise ValueError("Unexpected endpoint")
    return {"status": 200, "endpoint_id": endpoint_id, "body": body, "cost_usd": 0}


@server.tool()
def unrelated_paid_tool() -> dict:
    raise ValueError("This must never be called or exposed")


class CheckToken:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if dict(scope["headers"]).get(b"authorization") != b"Bearer protocol-test-token":
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


app = server.streamable_http_app()
app.add_middleware(CheckToken)
uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
