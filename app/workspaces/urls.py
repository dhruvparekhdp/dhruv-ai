"""Clone URL policy. The host list is exact, and the addresses must be public."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from app.workspaces.models import WorkspaceError

_APPROVED_HOSTS = frozenset({"github.com"})


def validate_public_url(url: str, *, resolve: bool = False) -> str:
    """Accept only HTTPS GitHub URLs on port 443, with no embedded credentials."""
    try:
        parsed = urlsplit(url)
        # urlsplit lowercases the host. A trailing dot is a different name, so it stays.
        hostname = parsed.hostname or ""
    except ValueError as exc:
        raise WorkspaceError("source_url is invalid") from exc
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise WorkspaceError("source_url must be an HTTPS URL without embedded credentials")
    if parsed.query or parsed.fragment:
        raise WorkspaceError("source_url cannot include query strings or fragments")
    if hostname not in _APPROVED_HOSTS:
        raise WorkspaceError("source host is not an approved repository host")
    if parsed.port not in {None, 443}:
        raise WorkspaceError("source host must use port 443")
    if resolve:
        _require_public_addresses(hostname)
    return url


def _require_public_addresses(hostname: str) -> None:
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WorkspaceError("source host could not be resolved") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError as exc:
            raise WorkspaceError("source host could not be resolved") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise WorkspaceError("source host does not resolve to a public address")
