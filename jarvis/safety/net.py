"""HTTPS targets that are safe to dial. The checked address is the one we connect to."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpcore
import httpx
from httpcore._backends.sync import SyncBackend

_NAME_BLOCK = (
    "localhost",
    "metadata.google.internal",
)


@dataclass(frozen=True)
class PublicTarget:
    hostname: str
    ip: str
    port: int


def _blocked_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return _blocked_ip(mapped)
        if address.sixtofour is not None or address.teredo is not None:
            return True
    return bool(
        not address.is_global
        or address.is_multicast
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


def _blocked_name(host: str) -> bool:
    name = host.strip().lower().rstrip(".")
    if not name or name in _NAME_BLOCK:
        return True
    return name.endswith(".local") or name.endswith(".localhost") or name.endswith(".internal") or name.endswith(".localdomain")


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def resolve_public_https(url: str, resolver=None) -> PublicTarget | None:
    """Return one public address, or nothing. Every answer must be public."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if _blocked_name(host) or parsed.port not in {None, 443}:
        return None
    literal = _literal_ip(host)
    if literal is not None:
        if _blocked_ip(literal):
            return None
        return PublicTarget(host, literal.compressed, 443)
    lookup = resolver or socket.getaddrinfo
    try:
        records = lookup(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    addresses: list[str] = []
    for record in records:
        sockaddr = record[4]
        if not sockaddr:
            return None
        addresses.append(str(sockaddr[0]))
    if not addresses:
        return None
    try:
        parsed_addresses = [ipaddress.ip_address(item) for item in addresses]
    except ValueError:
        return None
    if any(_blocked_ip(item) for item in parsed_addresses):
        return None
    return PublicTarget(host, parsed_addresses[0].compressed, 443)


def handoff_allowed(url: str) -> bool:
    """A test double may see this URL. Literals and names that are never public may not."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if _blocked_name(host) or parsed.port not in {None, 443}:
        return False
    literal = _literal_ip(host)
    return literal is None or not _blocked_ip(literal)


class PinnedBackend(SyncBackend):
    """Dial one already-checked address. Do not resolve the hostname again."""

    def __init__(self, ip: str, inner: SyncBackend | None = None) -> None:
        super().__init__()
        self._ip = ip
        self._inner = inner or SyncBackend()

    def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options=None):
        try:
            pinned = ipaddress.ip_address(self._ip)
        except ValueError as exc:
            raise httpcore.ConnectError("blocked") from exc
        if _blocked_ip(pinned) or host != self._ip or port != 443:
            raise httpcore.ConnectError("blocked")
        return self._inner.connect_tcp(self._ip, port, timeout, local_address, socket_options)

    def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        raise httpcore.ConnectError("blocked")


class PinnedTransport(httpx.BaseTransport):
    def __init__(self, ip: str, hostname: str, inner: httpx.BaseTransport | None = None) -> None:
        self._ip = ip
        self._hostname = hostname
        self._inner = inner if inner is not None else _real_transport(ip)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            pinned = ipaddress.ip_address(self._ip)
        except ValueError as exc:
            raise httpx.ConnectError("blocked") from exc
        if _blocked_ip(pinned):
            raise httpx.ConnectError("blocked")
        url = request.url.copy_with(host=self._ip, port=443)
        headers = [(key, value) for key, value in request.headers.multi_items() if key.lower() != "host"]
        headers.append(("host", self._hostname))
        rewritten = httpx.Request(request.method, url, headers=headers, content=request.read())
        rewritten.extensions = dict(request.extensions)
        rewritten.extensions["sni_hostname"] = self._hostname
        return self._inner.handle_request(rewritten)

    def close(self) -> None:
        self._inner.close()


def _real_transport(ip: str) -> httpx.HTTPTransport:
    transport = httpx.HTTPTransport(retries=0, trust_env=False)
    context = transport._pool._ssl_context
    transport._pool.close()
    transport._pool = httpcore.ConnectionPool(
        ssl_context=context,
        retries=0,
        network_backend=PinnedBackend(ip),
        max_connections=5,
        max_keepalive_connections=0,
    )
    return transport


def select_transport(url: str, transport: httpx.BaseTransport | None, resolver) -> httpx.BaseTransport | None:
    if transport is not None:
        if not handoff_allowed(url):
            return None
        return transport
    target = resolve_public_https(url, resolver)
    if target is None:
        return None
    return PinnedTransport(target.ip, target.hostname)
