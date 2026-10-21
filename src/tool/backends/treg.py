"""Two fixed, free TinyFish endpoints behind the existing web tool contracts."""
from dataclasses import replace
import hashlib
import ipaddress
import json
import os
import time
from urllib.parse import urlsplit

from ...frame.contracts import ControlError, DomainError, RunContext
from ..contracts import Availability, BackendResult
from .managed import ManagedBackend
from ...observability.observation_mgr import ObservationMgr


MCP_ENDPOINT = "https://treg.to/mcp/"
ENDPOINTS = {"web_search": ("tinyfish.web.search", "GET"),
             "web_extract": ("tinyfish.web.fetch", "POST")}


def _zero(value):
    return type(value) in {int, float} and value == 0


class TregWebBackend(ManagedBackend):
    def __init__(self, settings):
        super().__init__(settings)
        if (settings.get("discover", False) or settings.get("allow_private", False)
                or settings.get("endpoint", MCP_ENDPOINT) != MCP_ENDPOINT):
            raise DomainError("CONFIG_INVALID", "Invalid fixed treg web configuration")
        self._timeout = settings.get("timeout", 30)
        self._max_bytes = min(settings.get("max_bytes", 1048576), 1048576)
        self._hosts = settings.get("allowed_hosts", ())
        self._credential_env = settings.get("credential_env", "TREG_TOKEN")
        self._mcp = None
        # Missing credentials hide these tools without breaking unrelated startup.
        if not os.environ.get(self._credential_env):
            return
        from .mcp import MCPBackend
        self._mcp = MCPBackend({"transport": "streamable_http", "endpoint": MCP_ENDPOINT,
            "credential_env": self._credential_env, "timeout": self._timeout,
            "drain_timeout": self._drain_timeout, "include": ["catalog_get", "call"],
            "read_only": ["catalog_get", "call"]})
        try:
            self._mcp.ListTools(RunContext(deadline=time.monotonic() + self._timeout))
            if any(self._mcp.ToolSpec(name) is None for name in ("catalog_get", "call")):
                raise DomainError("MCP_PROTOCOL", "Required treg protocol tools are missing")
        except BaseException:
            self._mcp.Close()
            raise

    def Probe(self):
        ready = not self._closing and self._mcp is not None and self._mcp.Probe().available
        return Availability(ready, {"web.search", "web.extract"}, None if ready else "NOT_READY")

    def _close(self):
        if self._mcp is not None:
            self._mcp.Close()

    def Execute(self, operation, args, context):
        context.CheckCancelled()
        if operation not in ENDPOINTS:
            return BackendResult("invalid", '{"code":"invalid_operation"}')
        deadline = time.monotonic() + self._timeout
        child = replace(context, deadline=min(context.deadline, deadline)
                        if context.deadline is not None else deadline)
        try:
            with self.Borrow(context):
                if self._mcp is None:
                    raise DomainError("CONFIG_CREDENTIAL", "Treg credential is missing")
                if operation == "web_search":
                    params = {"query": args["query"], "domain_type": "web", "page": 0}
                else:
                    urls = list(dict.fromkeys(args["urls"]))
                    for url in urls:
                        self._url(url)
                    params = {"urls": urls, "format": "markdown", "ttl": 0,
                              "per_url_timeout_ms": max(1, int(child.Remaining() * 1000))}
                endpoint, method = ENDPOINTS[operation]
                quote = self._request("catalog_get", {"endpoint_id": endpoint}, child)
                self._free(quote, endpoint, method)
                child.CheckCancelled()
                if operation == "web_extract":
                    params["per_url_timeout_ms"] = max(1, int(child.Remaining() * 1000))
                receipt = self._request("call", {"endpoint_id": endpoint, "method": method,
                                                "params": params}, child)
                body = self._body(receipt, endpoint)
                data = (self._search(body, args.get("limit", 5)) if operation == "web_search"
                        else self._extract(body, urls, args.get("char_limit", 15000)))
                data["provider"] = {"endpoint_id": endpoint}
                if receipt.get("cost_usd") is not None:
                    data["provider"]["cost_usd"] = receipt["cost_usd"]
                child.CheckCancelled()
                encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
                if len(encoded.encode("utf8")) > self._max_bytes:
                    raise DomainError("NETWORK_LIMIT", "Treg output exceeds byte limit")
                status = "error" if operation == "web_extract" and not data["pages"] else "ok"
                return BackendResult(status, encoded)
        except ControlError:
            context.CheckCancelled()
            return BackendResult("error", '{"code":"NETWORK_TIMEOUT"}')
        except DomainError as error:
            ObservationMgr.GetInstance().Update(error_code=error.code)
            status = "denied" if error.code in {"NETWORK_DENIED", "TREG_COST_POLICY", "NOT_READY"} else "error"
            return BackendResult(status, json.dumps({"code": error.code}))
        except Exception:
            context.CheckCancelled()
            return BackendResult("error", '{"code":"NETWORK_PROTOCOL"}')

    def _request(self, name, args, context):
        mgr = ObservationMgr.GetInstance()
        with mgr.Scope('tool.treg.request', method=name, endpoint_id=args['endpoint_id']):
            data = self._request_data(name, args, context)
            status = data.get('status')
            if type(status) is int:
                mgr.Update(upstream_http_status=status)
            mgr.Complete(status='ok')
            return data

    def _request_data(self, name, args, context):
        result = self._mcp.Execute(name, args, context)
        context.CheckCancelled()
        if result.status != "ok":
            try:
                code = json.loads(result.content).get("code")
            except (ValueError, AttributeError):
                code = None
            # Only preserve our known local codes; never copy remote error text.
            if isinstance(code, str) and code in {'MCP_TIMEOUT', 'MCP_CONNECTION', 'MCP_PROTOCOL',
                        'MCP_TOOL_ERROR', 'MCP_TOOL_UNAVAILABLE', 'MCP_INTERRUPTED'}:
                ObservationMgr.GetInstance().Update(cause_code=code)
            raise DomainError("NETWORK_TIMEOUT" if code == "MCP_TIMEOUT" else "TREG_PROTOCOL",
                              "Treg protocol request failed")
        if len(result.content.encode("utf8")) > self._max_bytes:
            raise DomainError("NETWORK_LIMIT", "Treg response exceeds byte limit")
        wire = json.loads(result.content)
        if not isinstance(wire, dict) or wire.get("isError"):
            raise DomainError("TREG_PROTOCOL", "Invalid treg protocol response")
        data = wire.get("structuredContent")
        if data is None:
            content = wire.get("content", [])
            if (not isinstance(content, list) or len(content) != 1
                    or not isinstance(content[0], dict) or content[0].get("type") != "text"):
                raise DomainError("TREG_PROTOCOL", "Expected treg JSON content")
            data = json.loads(content[0]["text"])
        if not isinstance(data, dict):
            raise DomainError("TREG_PROTOCOL", "Expected treg JSON object")
        if data.get("error"):
            raise DomainError("NETWORK_SOURCE", "Treg could not serve the endpoint")
        return data

    @staticmethod
    def _free(quote, endpoint, method):
        entry = quote.get("endpoint")
        if not isinstance(entry, dict) or entry.get("id") != endpoint or entry.get("method") != method:
            raise DomainError("TREG_PROTOCOL", "Treg catalog entry does not match")
        cost = entry.get("cost", {})
        if (not isinstance(cost, dict) or cost.get("type") != "free" or cost.get("currency") != "USD"
                or not _zero(cost.get("usd")) or not _zero(cost.get("value"))
                or quote.get("usd_per_call") is not None and not _zero(quote["usd_per_call"])
                or any(row.get("overflow_price_usd") is not None or row.get("overflow_via")
                       for row in (entry, quote))):
            raise DomainError("TREG_COST_POLICY", "Only free direct endpoints are allowed")

    @staticmethod
    def _body(receipt, endpoint):
        if receipt.get("endpoint_id") != endpoint:
            raise DomainError("TREG_PROTOCOL", "Treg returned a different endpoint")
        if (receipt.get("cost_usd") is not None and not _zero(receipt["cost_usd"])
                or str(receipt.get("served_via", "")).startswith("overflow:")):
            raise DomainError("TREG_COST_POLICY", "Treg reported a non-free or relayed call")
        status = receipt.get("status")
        if type(status) is int:
            ObservationMgr.GetInstance().Update(upstream_http_status=status)
        if type(status) is not int:
            raise DomainError("TREG_PROTOCOL", "Missing treg upstream status")
        if not 200 <= status < 300:
            code = "NETWORK_RATE_LIMIT" if status == 429 else "NETWORK_HTTP"
            raise DomainError(code, "Treg upstream request failed")
        body = receipt.get("body")
        if not isinstance(body, dict):
            raise DomainError("TREG_PROTOCOL", "Expected provider JSON object")
        return body

    def _url(self, url):
        # TinyFish resolves/fetches remotely. Local DNS (including proxy Fake-IP)
        # is not evidence about its destination; never open the target locally.
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower().rstrip(".")
            if (parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password
                    or parsed.fragment or parsed.port == 0 or "\\" in url
                    or any(char.isspace() or ord(char) < 32 for char in url)
                    or self._hosts and host not in self._hosts
                    or host == "localhost" or host.endswith((".localhost", ".local")) or "." not in host and ":" not in host):
                raise ValueError
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError
            return url
        except (ValueError, TypeError):
            raise DomainError("NETWORK_DENIED", "Network destination is not allowed") from None

    @staticmethod
    def _artifact(endpoint, data):
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf8")
        return {"source": "treg://" + endpoint, "kind": "provider_response",
                "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

    def _search(self, body, limit):
        results = body.get("results")
        if not isinstance(results, list) or len(results) > 100:
            raise DomainError("TREG_PROTOCOL", "Invalid search results")
        rows = []
        for row in results[:limit]:
            if not isinstance(row, dict) or any(not isinstance(row.get(key), str) for key in ("title", "url", "snippet")):
                raise DomainError("TREG_PROTOCOL", "Invalid search result")
            rows.append({"title": row["title"][:500], "url": self._url(row["url"]), "snippet": row["snippet"][:4000]})
        total = body.get("total_results", len(results))
        if type(total) is not int or total < 0:
            raise DomainError("TREG_PROTOCOL", "Invalid search count")
        return {"results": rows, "truncated": max(total, len(results)) > len(rows),
                "artifact": self._artifact("tinyfish.web.search", body)}

    def _extract(self, body, urls, maximum):
        results, errors = body.get("results"), body.get("errors", [])
        if not isinstance(results, list) or not isinstance(errors, list):
            raise DomainError("TREG_PROTOCOL", "Invalid fetch results")
        by_url, failures = {}, {}
        for error in errors:
            if not isinstance(error, dict) or not isinstance(error.get("url"), str) or error["url"] not in urls:
                continue
            code = error.get("error")
            failure = {"url": error["url"], "code": code[:128] if isinstance(code, str) and code else "NETWORK_SOURCE"}
            status = error.get("status")
            if type(status) is int and 100 <= status <= 599:
                failure["http_status"] = status
            failures[error["url"]] = failure
        for row in results:
            # Unattributable rows cannot supply evidence for a requested URL.
            if not isinstance(row, dict) or not isinstance(row.get("url"), str) or row["url"] not in urls:
                continue
            by_url.setdefault(row["url"], []).append(row)
        pages, errors = [], []
        for url in urls:
            rows = by_url.get(url, [])
            if not rows:
                errors.append(failures.get(url, {"url": url, "code": "MISSING_RESULT"}))
                continue
            row = rows[0]
            if (len(rows) != 1 or not isinstance(row.get("text"), str) or not row["text"].strip()
                    or row.get("final_url") is not None and not isinstance(row["final_url"], str)):
                errors.append(failures.get(url, {"url": url, "code": "TREG_PROTOCOL"}))
                continue
            final_url = self._url(row.get("final_url") or url)
            try:
                artifact = self._artifact("tinyfish.web.fetch", row)
            except (ValueError, TypeError):
                errors.append({"url": url, "code": "TREG_PROTOCOL"})
                continue
            pages.append({"url": final_url, "text": row["text"][:maximum],
                          "truncated": len(row["text"]) > maximum, "artifact": artifact})
        data = {"pages": pages, "errors": errors, "partial": bool(pages and errors)}
        if not pages:
            data["code"] = "NETWORK_SOURCE"
        return data
