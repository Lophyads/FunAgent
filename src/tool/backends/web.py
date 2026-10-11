from .managed import ManagedBackend
from ..contracts import Availability, BackendResult
from .network import Fetch, Address
from .process import RunProcess
from ...frame.contracts import ControlError, DomainError
from dataclasses import replace
import hashlib
import json
import socket
import sys
import time
import importlib.util
from urllib.parse import urlencode
from html.parser import HTMLParser
import io


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "svg", "noscript"}:
            self.skip += 1
        elif tag in {"p", "br", "div", "h1", "h2", "h3", "li", "article"}:
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in {"script", "style", "svg", "noscript"} and self.skip:
            self.skip -= 1
        elif tag in {"p", "div", "h1", "h2", "h3", "li", "article"}:
            self.parts.append("\n")
    def handle_data(self, text):
        if not self.skip:
            self.parts.append(text)
    def text(self):
        return "\n".join(line for line in (" ".join(line.split()) for line in "".join(self.parts).splitlines()) if line)


class WebBackend(ManagedBackend):
    def __init__(self, settings):
        super().__init__(settings)
        self._settings = settings
        self._timeout = settings.get("timeout", 10)
        self._private = settings.get("allow_private", False)
        self._hosts = settings.get("allowed_hosts", ())
    def Probe(self):
        capabilities = {"web.extract"}
        if self._settings.get("search_endpoint") or importlib.util.find_spec("ddgs") is not None:
            capabilities.add("web.search")
        return Availability(not self._closing, capabilities)
    def Execute(self, operation, args, context):
        context.CheckCancelled()
        deadline = time.monotonic() + self._timeout
        child = replace(context, deadline=min(context.deadline, deadline) if context.deadline is not None else deadline)
        try:
            with self.Borrow(context):
                if operation == "web_search":
                    data = self._search(args, child)
                elif operation == "web_extract":
                    data = self._extract(args, child)
                else:
                    return BackendResult("invalid", '{"code":"invalid_operation"}')
                return BackendResult("ok", json.dumps(data, ensure_ascii=False))
        except ControlError:
            context.CheckCancelled()
            return BackendResult("error", '{"code":"NETWORK_TIMEOUT"}')
        except DomainError as error:
            return BackendResult("denied" if error.code == "NETWORK_DENIED" else "error", json.dumps({"code": error.code}))
        except (socket.timeout, TimeoutError):
            context.CheckCancelled()
            return BackendResult("error", '{"code":"NETWORK_TIMEOUT"}')
        except Exception:
            context.CheckCancelled()
            return BackendResult("error", '{"code":"NETWORK_PROTOCOL"}')

    def _fetch(self, url, context):
        return Fetch(url, context, timeout=self._timeout, allow_private=self._private, allowed_hosts=self._hosts,
                     max_bytes=self._settings.get("max_bytes", 2000000))

    def _search(self, args, context):
        endpoint = self._settings.get("search_endpoint")
        limit = args.get("limit", 5)
        if endpoint:
            url, mime, body = self._fetch(endpoint + ("&" if "?" in endpoint else "?") +
                                          urlencode({"q": args["query"], "limit": limit}), context)
            if mime.split(";")[0] != "application/json":
                raise DomainError("NETWORK_PROTOCOL", "Search requires JSON")
            data = json.loads(body)
        else:
            value = RunProcess([sys.executable, "-X", "utf8", "-m", "src.tool.backends.web_search_worker",
                json.dumps({"query": args["query"], "limit": limit, "timeout": min(self._timeout, context.Remaining())})],
                context.workspace, context, min(self._timeout, context.Remaining()), 256000)
            context.CheckCancelled()
            if value["interrupted"]:
                raise DomainError("NETWORK_TIMEOUT", "Search timed out")
            if value["exit_code"] != 0 or value["truncated"]:
                raise DomainError("NETWORK_SOURCE", "Search provider failed")
            body, url = value["stdout"].encode("utf8"), "ddgs://search"
            data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list) or len(data["results"]) > 100:
            raise DomainError("NETWORK_PROTOCOL", "Invalid search response")
        rows = []
        for row in data["results"][:limit]:
            if not isinstance(row, dict) or any(not isinstance(row.get(f), str) for f in ("title", "url", "snippet")):
                raise DomainError("NETWORK_PROTOCOL", "Invalid search result")
            Address(row["url"], self._private, self._hosts)
            rows.append({"title": row["title"][:500], "url": row["url"], "snippet": row["snippet"][:4000]})
        return {"results": rows, "truncated": len(data["results"]) > limit,
                "artifact": {"source": url, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}}

    def _extract(self, args, context):
        pages, maximum = [], args.get("char_limit", 15000)
        for original in args["urls"]:
            url, mime, body = self._fetch(original, context)
            if mime.split(";")[0] in {"text/html", "application/xhtml+xml"}:
                parser = TextExtractor()
                parser.feed(body.decode("utf8"))
                text = parser.text()
            elif mime.split(";")[0] == "text/plain":
                text = body.decode("utf8")
            elif mime.split(";")[0] == "application/pdf":
                try:
                    from pypdf import PdfReader
                except ImportError:
                    raise DomainError("DRIVER_UNAVAILABLE", "PDF extraction dependency is unavailable") from None
                reader = PdfReader(io.BytesIO(body))
                if reader.is_encrypted or len(reader.pages) > 200:
                    raise DomainError("NETWORK_LIMIT", "PDF exceeds extraction limits")
                parts = []
                for page in reader.pages:
                    context.CheckCancelled()
                    parts.append(page.extract_text() or "")
                    if sum(map(len, parts)) > maximum:
                        break
                text = "\n".join(parts)
            else:
                raise DomainError("NETWORK_PROTOCOL", "Unsupported page content type")
            context.CheckCancelled()
            if not text.strip():
                raise DomainError("NETWORK_PROTOCOL", "Page has no extractable text")
            pages.append({"url": url, "text": text[:maximum], "truncated": len(text) > maximum,
                          "artifact": {"source": url, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}})
        return {"pages": pages}
