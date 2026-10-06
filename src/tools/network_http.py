"""Use configured proxies while retaining validated, pinned web destinations."""

from __future__ import annotations

import base64
import http.client
import ipaddress
import os
import socket
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies, proxy_bypass

from .network_safety import validate_url


_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def outbound_proxy(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.hostname and proxy_bypass(parts.hostname):
        return None
    proxies = getproxies()
    return proxies.get(parts.scheme) or proxies.get("all")


def search_proxy() -> str | None:
    proxies = getproxies()
    return os.environ.get("DDGS_PROXY") or proxies.get("https") or proxies.get("all")


def _proxy_socket(proxy: str, address: str, port: int, timeout: float):
    parts = urlsplit(proxy)
    if parts.scheme != "http" or not parts.hostname:
        raise ValueError("web extraction requires an http:// CONNECT proxy")
    headers = {}
    if parts.username is not None:
        credentials = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
        token = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        headers["Proxy-Authorization"] = f"Basic {token}"
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        # The configured proxy connects to the validated IP, without another DNS lookup.
        connection.set_tunnel(address, port, headers=headers)
        connection.connect()
        stream = connection.sock
        connection.sock = None
        return stream
    finally:
        connection.close()


def open_web_connection(url: str, address: str, *, timeout: float = 15):
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    connection_type = (
        http.client.HTTPSConnection if parts.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(parts.hostname, port, timeout=timeout)
    proxy = outbound_proxy(url)
    # HTTPSConnection verifies TLS and sends SNI for the original hostname.
    connection._create_connection = (
        lambda _address, timeout=timeout, source_address=None:
        _proxy_socket(proxy, address, port, timeout) if proxy
        else socket.create_connection((address, port), timeout, source_address)
    )
    return connection


def _resolve_addresses(hostname: str, port: int) -> tuple[str, ...]:
    infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    addresses = tuple(dict.fromkeys(info[4][0] for info in infos))
    if addresses and all(ipaddress.ip_address(a) in _FAKE_IP_NETWORK for a in addresses):
        raise ValueError(
            "DNS returned a proxy Fake-IP (198.18.0.0/15); configure real-IP DNS "
            "or a fake-ip-filter for this hostname in your proxy"
        )
    return addresses


def validate_web_url(
    url: str, *, allow_private: bool = False,
) -> tuple[str, tuple[str, ...]]:
    return validate_url(
        url, allow_private=allow_private,
        resolver=None if allow_private else _resolve_addresses,
    )
