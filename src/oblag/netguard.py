"""SSRF protection for outbound webhook targets (spec 07 Phase 2).

Once orgs can point webhooks anywhere, a naive POST is an SSRF primitive — it can
reach cloud metadata endpoints (169.254.169.254), internal services, or localhost.
We resolve the host and reject any target that maps to a non-public address, and
validate again at delivery time to blunt DNS rebinding.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    pass


def _ip_is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def assert_safe_url(url: str) -> None:
    """Raise UnsafeUrlError unless url is http(s) to a host resolving only to public IPs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError("webhook URL must be http or https")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("webhook URL has no host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"webhook host does not resolve: {host}") from exc
    resolved = {str(info[4][0]) for info in infos}
    if not resolved:
        raise UnsafeUrlError(f"webhook host does not resolve: {host}")
    for ip in resolved:
        if not _ip_is_public(ip):
            raise UnsafeUrlError(f"webhook host resolves to a non-public address ({ip})")
