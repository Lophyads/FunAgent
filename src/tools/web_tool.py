"""Keyless isolated search and bounded HTTP HTML/PDF extraction."""

from __future__ import annotations

import hashlib
import html
import http.client
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, Field

from agentcore.interfaces import ToolContext, TurnCancelled
from .network_safety import validate_url
from .network_http import open_web_connection, search_proxy, validate_web_url
from .registry import ToolEntry, ToolRegistry


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=100)


class WebExtractArgs(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=5)
    char_limit: int = Field(default=15_000, ge=500, le=100_000)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg", "noscript"}:
            self.skip_depth += 1
        elif tag in {"p", "br", "div", "h1", "h2", "h3", "li", "article"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag in {"p", "div", "li", "article"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def _minimal_env() -> dict[str, str]:
    allowed = (
        "PATH", "Path", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
        "HOME", "USERPROFILE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "COMSPEC",
        "USERNAME", "USERDOMAIN", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
        "DISPLAY", "WAYLAND_DISPLAY",
    )
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _search_subprocess(
    query: str, limit: int, context: ToolContext, timeout: float,
) -> dict:
    context.check_active()
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "tools._ddgs_worker"],
            stdin=subprocess.PIPE, stdout=output,
            stderr=subprocess.DEVNULL, env=_minimal_env(),
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        try:
            assert process.stdin is not None
            process.stdin.write(
                json.dumps({"query": query, "limit": limit,
                            "proxy": search_proxy()}).encode("utf-8")
            )
            process.stdin.close()
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                context.check_active()
                if time.monotonic() >= deadline:
                    raise TimeoutError("search timed out")
                time.sleep(0.05)
            output.seek(0)
            raw = output.read(256_001)
            if len(raw) > 256_000:
                raise ValueError("search response exceeded size limit")
            response = json.loads(raw.decode("utf-8"))
            if process.returncode != 0:
                detail = response.get("message", "")
                raise RuntimeError(
                    f"DDGS search failed: {response.get('error', 'unknown')}"
                    + (f": {detail}" if detail else "")
                )
            return response
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def _fetch_http(
    url: str, *, allow_private: bool, max_bytes: int,
    use_proxy: bool = False,
) -> tuple[str, str, bytes]:
    current = url
    for _ in range(6):
        validator = validate_web_url if use_proxy else validate_url
        safe_url, addresses = validator(current, allow_private=allow_private)
        parts = urlsplit(safe_url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        pinned_ip = addresses[0]
        if use_proxy:
            conn = open_web_connection(safe_url, pinned_ip)
        else:
            connection_type = (
                http.client.HTTPSConnection if parts.scheme == "https"
                else http.client.HTTPConnection
            )
            conn = connection_type(parts.hostname, port, timeout=15)
            conn._create_connection = (
                lambda _address, timeout=15, source_address=None:
                socket.create_connection((pinned_ip, port), timeout, source_address)
            )
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            conn.request(
                "GET", path,
                headers={
                    "User-Agent": "FunAgent/0.1",
                    "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.1",
                    "Accept-Encoding": "identity",
                },
            )
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("redirect omitted Location")
                current = urljoin(safe_url, location)
                continue
            if response.status >= 400:
                raise ValueError(f"HTTP {response.status}")
            content_type = response.getheader("Content-Type", "").lower()
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("response exceeded byte limit")
            return safe_url, content_type, data
        finally:
            conn.close()
    raise ValueError("too many redirects")


def _decode_content(content_type: str, data: bytes) -> str:
    if "application/pdf" in content_type or data.startswith(b"%PDF"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data), strict=False)
        return "\n".join(page.extract_text() or "" for page in reader.pages[:100])
    if (
        "text/html" not in content_type and "text/plain" not in content_type
        and "application/xhtml" not in content_type
    ):
        raise ValueError("unsupported content type")
    charset = "utf-8"
    if "charset=" in content_type:
        charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
    decoded = data.decode(charset, errors="replace")
    if "text/plain" in content_type:
        return decoded
    parser = _TextExtractor()
    parser.feed(decoded)
    return html.unescape(parser.text())


def _save_artifact(root: Path, text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    truncated = len(encoded) > max_bytes
    encoded = encoded[:max_bytes]
    while encoded:
        try:
            encoded.decode("utf-8")
            break
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    digest = hashlib.sha256(encoded).hexdigest()
    directory = root / ".funagent" / "artifacts"
    if not directory.resolve().is_relative_to(root):
        raise ValueError("artifact directory escapes the workspace")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"web-{digest[:20]}.txt"
    if not destination.exists():
        temporary = directory / (
            f".web-{digest[:20]}-{secrets.token_hex(8)}.tmp"
        )
        try:
            with temporary.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return destination.relative_to(root).as_posix(), truncated


def search_available(context) -> bool:
    return context.backends.available("search")


def web_search(args: WebSearchArgs, context: ToolContext) -> dict:
    return _bound_search(
        context.services.search(args.query, args.limit, context), args.limit,
    )


def web_extract(args: WebExtractArgs, context: ToolContext) -> dict:
    config = context.services.config.tools
    root = context.workspace.cwd
    max_bytes = config.artifact_max_bytes
    allow_private = config.allow_private_web_addresses
    page_budget = min(args.char_limit, max(1000, 23_000 // len(args.urls)))
    pages = []
    for url in args.urls:
        context.check_active()
        try:
            validate_web_url(url, allow_private=allow_private)
            final_url, content_type, data = context.services.fetch(url, context)
            validate_web_url(final_url, allow_private=allow_private)
            context.check_active()
            text = _decode_content(content_type, data)
            if len(text) > page_budget:
                artifact, artifact_truncated = _save_artifact(
                    root, text, max_bytes
                )
                head = page_budget // 2
                excerpt = text[:head] + "\n[…]\n" + text[-head:]
            else:
                artifact = None
                artifact_truncated = False
                excerpt = text
            pages.append({
                "url": final_url, "content": excerpt,
                "truncated": len(text) > page_budget,
                "artifact_path": artifact,
                "artifact_truncated": artifact_truncated,
            })
        except (TurnCancelled, KeyboardInterrupt):
            raise
        except Exception as exc:
            pages.append({
                "url": url, "error": f"{type(exc).__name__}: {exc}"
            })
    return {"source_untrusted": True, "pages": pages}


def register_tools(registry: ToolRegistry) -> None:
    registry.register(ToolEntry(
        tool_id="builtin.web_search", implementation_version="1",
        name="web_search",
        description="Search the public web with keyless DDGS; returns ordered titles, URLs and descriptions.",
        args_model=WebSearchArgs, handler=web_search, toolset="web",
        check_fn=search_available, side_effects="none", timeout_seconds=40,
    ))
    registry.register(ToolEntry(
        tool_id="builtin.web_extract", implementation_version="1",
        name="web_extract",
        description="Extract text from up to five HTTP/HTTPS HTML or PDF pages; long pages include a read_file artifact path.",
        args_model=WebExtractArgs, handler=web_extract, toolset="web",
        side_effects="none", timeout_seconds=90,
    ))


def _bound_search(response: dict, limit: int) -> dict:
    results = response.get("results", [])
    if not isinstance(results, list):
        raise ValueError("search backend must return a results list")
    selected = []
    for index, item in enumerate(results[:limit]):
        if not isinstance(item, dict):
            continue
        candidate = {
            "position": index + 1,
            "title": str(item.get("title", ""))[:300],
            "url": str(item.get("url", ""))[:2048],
            "description": str(item.get("description", ""))[:500],
        }
        trial = {"results": [*selected, candidate]}
        if len(json.dumps(trial, ensure_ascii=False)) > 24_000:
            break
        selected.append(candidate)
    return {
        "source_untrusted": True,
        "results": selected,
        "truncated": len(selected) < min(len(results), limit),
    }


__all__ = ["WebExtractArgs", "WebSearchArgs", "register_tools"]
