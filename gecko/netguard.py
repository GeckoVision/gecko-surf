"""Network guard — SSRF defense for every URL Gecko fetches on behalf of an agent.

Two responsibilities, both prerequisites for ingesting *untrusted* spec URLs and
making live upstream calls:

1. ``validate_public_url`` — reject anything that isn't a plain http(s) URL pointing
   at a routable public host: non-http schemes, ``file://``, and any hostname that
   resolves (or is an IP literal) into loopback / private / link-local / multicast /
   reserved space, including the cloud-metadata IP ``169.254.169.254``.
2. ``safe_get`` — an SSRF-safe GET for spec documents: caps redirects (re-validating
   every hop, so a public URL can't 302 you onto the metadata endpoint), caps the
   response size, and caps the timeout.

DNS resolution is injectable (``resolver``) so the validator is unit-testable with
zero real network traffic.

Control plane: this module fetches the API *surface* (the spec). It never persists
the bytes it reads — the caller parses and discards.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.request
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

# Defaults are conservative; spec docs are small and should resolve fast.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
DEFAULT_TIMEOUT = 30  # seconds
DEFAULT_MAX_REDIRECTS = 5

_ALLOWED_SCHEMES = {"http", "https"}

# Explicit defense-in-depth: cloud metadata endpoints. These also fall under the
# is_link_local / is_private checks below, but naming them documents the intent.
_BLOCKED_IPS = {
    ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure IMDS
    ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDS over IPv6
}

# resolver(host) -> list of IP strings. Defaults to real DNS.
Resolver = Callable[[str], list[str]]


class UnsafeUrlError(ValueError):
    """Raised when a URL is not a safe, public http(s) target (SSRF defense)."""


def _default_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise UnsafeUrlError(f"could not resolve host: {host}") from exc
    return [str(info[4][0]) for info in infos]


def _check_ip(raw_ip: str, *, host: str) -> None:
    """Raise if an IP is anything other than a routable public address."""
    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError as exc:
        raise UnsafeUrlError(f"invalid IP for host {host!r}: {raw_ip}") from exc
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) would otherwise dodge the v4 checks.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if (
        ip in _BLOCKED_IPS
        or ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise UnsafeUrlError(
            f"host {host!r} resolves to a non-public address ({ip}); refusing to fetch"
        )


def validate_public_url(url: str, *, resolver: Resolver | None = None) -> None:
    """Validate that ``url`` is a safe, public http(s) target. Raises ``UnsafeUrlError``.

    Returns ``None`` on success. ``resolver`` is injectable for offline tests.
    """
    resolve = resolver or _default_resolver
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            f"unsupported URL scheme {scheme!r}; only http/https are allowed"
        )
    host = parts.hostname
    if not host:
        raise UnsafeUrlError("URL has no host")

    # If the host is an IP literal, check it directly — never resolve.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        is_ip_literal = False
    else:
        is_ip_literal = True

    if is_ip_literal:
        _check_ip(host, host=host)
        return

    ips = resolve(host)
    if not ips:
        raise UnsafeUrlError(f"host {host!r} did not resolve to any address")
    for raw_ip in ips:
        _check_ip(raw_ip, host=host)


def safe_get(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: int = DEFAULT_TIMEOUT,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    resolver: Resolver | None = None,
) -> str:
    """SSRF-safe GET for a spec document. Validates every redirect hop, caps size.

    Redirects are followed manually (not by urllib) so each new target is
    re-validated — a public URL cannot 302 the fetch onto a private/metadata host.
    Returns the decoded body. Never persists it.
    """
    current = url
    for _ in range(max_redirects + 1):
        validate_public_url(current, resolver=resolver)
        request = urllib.request.Request(current, method="GET")
        # Do not auto-follow redirects: handle them ourselves so each hop is checked.
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=timeout) as resp:  # noqa: S310 (validated above)
            status = getattr(resp, "status", 200)
            if status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    raise UnsafeUrlError("redirect without a Location header")
                current = urljoin(current, location)
                continue
            chunk = resp.read(max_bytes + 1)
            if len(chunk) > max_bytes:
                raise UnsafeUrlError(
                    f"document exceeds size cap of {max_bytes} bytes; refusing to load"
                )
            return chunk.decode("utf-8")
    raise UnsafeUrlError(f"too many redirects (>{max_redirects})")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable urllib's automatic redirect following so we can re-validate hops."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None
