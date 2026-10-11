"""Validate every hop and connect to the validated DNS address."""
import http.client
import ipaddress
import socket
import ssl
import json
from urllib.parse import urlsplit, urljoin
from ...frame.contracts import DomainError


def Address(url, allow_private=False, allowed_hosts=()):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port == 0 or allowed_hosts and parsed.hostname not in allowed_hosts):
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses) and not allow_private:
            raise ValueError
        return parsed, addresses[0][4][0], port
    except (ValueError, OSError):
        raise DomainError("NETWORK_DENIED", "Network destination is not allowed") from None


def ReadBody(response, connection, context, timeout, max_bytes):
    declared = response.getheader("Content-Length")
    if declared and (not declared.isdigit() or int(declared) > max_bytes):
        raise DomainError("NETWORK_LIMIT", "Response exceeds byte limit")
    chunks, size = [], 0
    while True:
        context.CheckCancelled()
        if connection.sock is not None:
            connection.sock.settimeout(min(timeout, context.Remaining()))
        part = response.read1(min(65536, max_bytes + 1 - size))
        context.CheckCancelled()
        if not part:
            return b"".join(chunks)
        chunks.append(part)
        size += len(part)
        if size > max_bytes:
            raise DomainError("NETWORK_LIMIT", "Response exceeds byte limit")


def Fetch(url, context, *, timeout, allow_private=False, allowed_hosts=(), max_bytes=2000000,
          headers=None, follow_redirects=True):
    for _ in range(6):
        context.CheckCancelled()
        parsed, address, port = Address(url, allow_private, allowed_hosts)
        socket_timeout = min(timeout, context.Remaining())
        class Connection(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.create_connection((address, port), timeout=socket_timeout)
                if parsed.scheme == "https":
                    self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=parsed.hostname)
        connection = Connection(parsed.hostname, port, timeout=socket_timeout)
        response = None
        try:
            context.CheckCancelled()
            connection.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
                               headers={"User-Agent": "FunAgent/0.2", "Accept-Encoding": "identity", **(headers or {})})
            response = connection.getresponse()
            context.CheckCancelled()
            if response.status in {301, 302, 303, 307, 308}:
                if not follow_redirects or headers:
                    raise DomainError("NETWORK_PROTOCOL", "Operation backend redirects are not allowed")
                location = response.getheader("Location")
                if not location:
                    raise DomainError("NETWORK_PROTOCOL", "Invalid redirect")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise DomainError("NETWORK_HTTP", "Network request failed")
            body = ReadBody(response, connection, context, timeout, max_bytes)
            return url, response.getheader("Content-Type", ""), body
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                connection.close()
    raise DomainError("NETWORK_PROTOCOL", "Too many redirects")


def JSONPost(url, payload, context, *, timeout, headers=None, allow_private=False, max_bytes=256000):
    context.CheckCancelled()
    parsed, address, port = Address(url, allow_private)
    socket_timeout = min(timeout, context.Remaining())
    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((address, port), timeout=socket_timeout)
            if parsed.scheme == "https":
                self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=parsed.hostname)
    connection = Connection(parsed.hostname, port, timeout=socket_timeout)
    response = None
    try:
        context.CheckCancelled()
        connection.request("POST", parsed.path or "/", body=json.dumps(payload).encode("utf8"),
                           headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        context.CheckCancelled()
        if response.status != 200 or response.getheader("Content-Type", "").split(";")[0] != "application/json":
            raise DomainError("NETWORK_PROTOCOL", "Operation backend request failed")
        body = ReadBody(response, connection, context, timeout, max_bytes)
        return json.loads(body)
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            connection.close()
