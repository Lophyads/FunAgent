"""URL policy shared by web and browser tools."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit


_METADATA_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.100.100.200/32"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)


def validate_url(
    raw_url: str, *, allow_private: bool = False,
    resolve_dns: bool = True,
    resolver: Callable[[str, int], tuple[str, ...]] | None = None,
) -> tuple[str, tuple[str, ...]]:
    if len(raw_url) > 4096:
        raise ValueError("URL is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_url):
        raise ValueError("URL contains control characters")
    parts = urlsplit(raw_url)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("only http and https URLs are allowed")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("URL must have a host and must not contain credentials")
    hostname = parts.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        ".localhost"
    ):
        if not allow_private:
            raise ValueError("local addresses are not allowed")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ValueError("invalid URL port") from exc
    if port < 1 or port > 65535:
        raise ValueError("invalid URL port")
    if parts.scheme.lower() == "https" and port == 80:
        raise ValueError("HTTPS URL uses an invalid port")
    try:
        ip = ipaddress.ip_address(hostname)
        addresses = (str(ip),)
    except ValueError:
        if not resolve_dns:
            return urlunsplit(parts._replace(fragment="")), ()
        if resolver is None:
            infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            addresses = tuple(dict.fromkeys(info[4][0] for info in infos))
        else:
            addresses = resolver(hostname, port)
    if not addresses:
        raise ValueError("hostname did not resolve")
    for address in addresses:
        parsed = ipaddress.ip_address(address)
        if any(parsed in network for network in _METADATA_NETWORKS):
            raise ValueError("cloud metadata address is not allowed")
        if not allow_private and not parsed.is_global:
            raise ValueError("private or non-global address is not allowed")
    return urlunsplit(parts._replace(fragment="")), addresses


__all__ = ["validate_url"]
