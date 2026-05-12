"""SSRF deny-list for outbound engine requests.

Slice-2 approach: literal-IP check only. If the URL's hostname parses
as an IPv4 or IPv6 address, block when it falls in any private /
loopback / link-local range. DNS names are NOT resolved in this slice
— resolving here AND letting httpx resolve at fire-time opens a DNS-
rebinding window (Reliability flagged it; pinned-IP transport ships
in slice-3).

Schemes other than ``http``/``https`` are rejected in
staging/production. ``development`` allows everything so the twin
override (``http://twins:8000``) keeps working — matches the twin
middleware's ``env == "development"`` gate.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

_ALLOWED_ENVS_FOR_DENY = {"staging", "production"}
_ALLOWED_SCHEMES = {"http", "https"}

# Literal-string host deny-set. Belt-and-suspenders against loopback
# names that resolve to 127.0.0.1 without us doing the resolution.
# Pinned-IP transport in slice-3 will cover the general DNS-rebinding
# case; this is the zero-cost block of the obvious offenders.
_DENIED_HOSTNAMES = frozenset({
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
    "localhost.localdomain",
})


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> tuple[bool, str | None]:
    if ip.is_loopback:
        return True, "loopback IP"
    if ip.is_link_local:
        return True, "link-local IP"
    if ip.is_private:
        return True, "private (RFC1918) IP"
    if ip.is_unspecified:
        return True, "unspecified IP (0.0.0.0)"
    if ip.is_multicast:
        return True, "multicast IP"
    if ip.is_reserved:
        return True, "reserved IP"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _ip_is_blocked(ip.ipv4_mapped)
    return False, None


def is_blocked_host(url: str, env: str) -> tuple[bool, str | None]:
    """Return ``(blocked, reason)``.

    Development: always allow. Staging/production: block non-http(s)
    schemes; for literal IP hostnames, block loopback / link-local /
    RFC1918 / unspecified / multicast / reserved. DNS hostnames pass
    through (rebinding mitigation is slice-3).
    """
    if env not in _ALLOWED_ENVS_FOR_DENY:
        return False, None

    try:
        parts = urlsplit(url)
    except ValueError as e:
        return True, f"unparseable URL: {e}"

    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return True, f"disallowed scheme: {scheme!r}"

    host = parts.hostname
    if host is None or host == "":
        return True, "empty host"

    # Literal-string deny before IP parse — catches names that resolve
    # to loopback (we don't do DNS resolution this slice; slice-3 will).
    if host.lower() in _DENIED_HOSTNAMES:
        return True, f"denied hostname: {host!r}"

    # Try parsing host as a literal IP. urlsplit strips IPv6 brackets.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # DNS name — pass through. See module docstring for rebinding caveat.
        return False, None

    return _ip_is_blocked(ip)
