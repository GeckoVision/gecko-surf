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

import http.client
import ipaddress
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

# 3xx statuses safe_get follows manually (re-validating each hop for SSRF).
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

# Defaults are conservative; spec docs are small and should resolve fast.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
DEFAULT_TIMEOUT = 30  # seconds
DEFAULT_MAX_REDIRECTS = 5

#: Self-identifying User-Agent — the single source of truth (caller.py imports it).
#: urllib's default ("Python-urllib/x.y") is 403'd by many WAFs (Cloudflare et al.),
#: which broke live calls (0.2.1 caller fix) and then ``gecko from-docs`` on the SAME
#: Cloudflare front (Mintlify docs pages) — every stdlib fetch path needs a real UA.
USER_AGENT = "gecko/0.2 (+https://geckovision.tech)"

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


def _resolve_public(url: str, resolver: Resolver | None) -> tuple[str, list[str]]:
    """Validate scheme + host and resolve the host EXACTLY ONCE, returning
    ``(host, [validated public IPs])``. For an IP-literal host the list is just that IP.

    This single resolution is what ``safe_get`` pins the socket to — closing the
    DNS-rebind TOCTOU where the validator resolves one (public) IP and urllib then
    independently re-resolves a different (private/metadata) one. Raises ``UnsafeUrlError``
    on anything non-public.
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
        return host, [host]

    ips = resolve(host)
    if not ips:
        raise UnsafeUrlError(f"host {host!r} did not resolve to any address")
    for raw_ip in ips:
        _check_ip(raw_ip, host=host)
    return host, ips


def validate_public_url(url: str, *, resolver: Resolver | None = None) -> None:
    """Validate that ``url`` is a safe, public http(s) target. Raises ``UnsafeUrlError``.

    Returns ``None`` on success. ``resolver`` is injectable for offline tests.
    """
    _resolve_public(url, resolver)


#: An opener factory: given the validated IP to pin the socket to (``None`` for a host
#: needing no pin), return a urllib-style opener with ``.open(request, timeout=...)``.
#: Injectable so the rebind pin is falsifiable offline without real sockets.
OpenerFactory = Callable[[str | None], Any]


def safe_get(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: int = DEFAULT_TIMEOUT,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    resolver: Resolver | None = None,
    opener_factory: OpenerFactory | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """SSRF-safe GET for a spec document. Validates every redirect hop, caps size.

    Each hop resolves the host EXACTLY ONCE and PINS the socket to that validated IP, so
    urllib cannot independently re-resolve onto a private/metadata address in the window
    between validation and connection (DNS-rebind TOCTOU). Redirects are followed manually
    (not by urllib) so each new target is validated + pinned afresh. Returns the decoded
    body. Never persists it.

    ``headers`` lets a caller pass extra request headers (e.g. an auth header for a
    registry fetch); a caller-supplied ``User-Agent`` overrides the default below.
    """
    factory = opener_factory or _pinned_opener
    current = url
    # Pinned once: the ORIGINAL host. Caller-supplied headers (e.g. a bearer
    # X-Gecko-Key) must never follow a redirect off this host — a malicious or
    # compromised upstream could 302 us to an attacker host and harvest the
    # credential otherwise. Same-host hops (incl. scheme/port changes) keep them.
    original_host = urlsplit(url).hostname
    for _ in range(max_redirects + 1):
        # Resolve ONCE; the returned IP is what we pin the connection to (rebind-proof).
        _, ips = _resolve_public(current, resolver)
        opener = factory(ips[0] if ips else None)
        # Real UA by default: the stdlib default "Python-urllib/x.y" is 403'd by
        # WAF-fronted docs hosts. A caller-supplied UA (if any) wins via update().
        request_headers = {"User-Agent": USER_AGENT}
        if urlsplit(current).hostname == original_host:
            request_headers.update(headers or {})
        request = urllib.request.Request(current, method="GET", headers=request_headers)
        try:
            with opener.open(request, timeout=timeout) as resp:  # noqa: S310 (validated+pinned)
                status = getattr(resp, "status", 200)
                if status in _REDIRECT_CODES:
                    current = _redirect_target(current, resp.headers)
                    continue
                chunk = resp.read(max_bytes + 1)
                if len(chunk) > max_bytes:
                    raise UnsafeUrlError(
                        f"document exceeds size cap of {max_bytes} bytes; refusing to load"
                    )
                return chunk.decode("utf-8")
        except urllib.error.HTTPError as exc:
            # With auto-follow disabled, urllib RAISES on a 3xx instead of returning it.
            # Follow it ourselves (re-validated at the top of the loop); other statuses
            # (404, 5xx) propagate to the caller as the OSError subclass they are.
            if exc.code in _REDIRECT_CODES:
                current = _redirect_target(current, exc.headers)
                continue
            raise
    raise UnsafeUrlError(f"too many redirects (>{max_redirects})")


def _pinned_opener(pinned_ip: str | None) -> urllib.request.OpenerDirector:
    """Build a urllib opener that (a) does NOT auto-follow redirects (we re-validate each
    hop ourselves) and (b) pins every connection to ``pinned_ip`` — the validated address
    from the single resolution — while keeping the original hostname for the ``Host``
    header and TLS SNI/cert check. This is the socket-level defense against DNS rebind."""
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    if pinned_ip is not None:
        handlers.append(_PinnedHTTPHandler(pinned_ip))
        handlers.append(_PinnedHTTPSHandler(pinned_ip))
    return urllib.request.build_opener(*handlers)


def _pinned_http_connection(base: type, pinned_ip: str) -> type:
    """Subclass an http.client connection so ``connect`` dials the validated ``pinned_ip``
    instead of re-resolving ``self.host``. ``self.host`` stays the original name, so the
    ``Host`` header and (for HTTPS) SNI + certificate verification use it — only the socket
    target is pinned."""

    class _Pinned(base):  # type: ignore[valid-type,misc]
        def connect(self) -> None:
            sock = socket.create_connection(
                (pinned_ip, self.port),
                self.timeout,
                self.source_address,
            )
            if getattr(self, "_tunnel_host", None):
                self.sock = sock
                self._tunnel()
                sock = self.sock
            context = getattr(self, "_context", None)
            if context is not None:  # HTTPS: wrap with SNI/cert = the ORIGINAL host
                self.sock = context.wrap_socket(sock, server_hostname=self.host)
            else:
                self.sock = sock

    return _Pinned


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned_ip: str) -> None:
        super().__init__()
        self._conn = _pinned_http_connection(http.client.HTTPConnection, pinned_ip)

    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(self._conn, req)  # type: ignore[arg-type]


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_ip: str) -> None:
        super().__init__()
        self._conn = _pinned_http_connection(http.client.HTTPSConnection, pinned_ip)

    def https_open(self, req: urllib.request.Request) -> Any:
        # HTTPSHandler carries the SSL context as ``_context`` at runtime (not in the stub).
        context = getattr(self, "_context", None)
        return self.do_open(self._conn, req, context=context)  # type: ignore[arg-type]


def _redirect_target(current: str, headers: Any) -> str:
    """Resolve a redirect's ``Location`` against the current URL (absolute or relative)."""
    location = headers.get("Location") if headers is not None else None
    if not location:
        raise UnsafeUrlError("redirect without a Location header")
    return urljoin(current, location)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable urllib's automatic redirect following so we can re-validate hops."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None
