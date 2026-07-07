"""Registry HTTP surface — mounted into the existing multi-surface server.

Anonymous fetch for free surfaces; ``X-Gecko-Key`` + flat per-surface
entitlement for premium ones (402 entitlement_required otherwise).
"""

from __future__ import annotations

import json
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .keys import KeyStore, RegistryAuthError
from .store import SurfaceStore

KEY_HEADER = "X-Gecko-Key"

# Same convention as http_server.MAX_COMPREHEND_REQUEST_BYTES: these POSTs are
# unauthenticated and internet-reachable, so cap the body before it's parsed.
MAX_REGISTRY_REQUEST_BYTES = 4096

# Per-IP issuance throttle for POST /registry/keys — complements the per-email
# cap in KeyStore. Bounded in-memory map; the real mailer goes live later and
# this endpoint is the internet-reachable email-send door until then.
_IP_THROTTLE_MAX_PER_HOUR = 10
_IP_THROTTLE_WINDOW_SECONDS = 3600
_IP_THROTTLE_MAX_ENTRIES = 10_000
_ip_counts: dict[str, tuple[int, float]] = {}


class _BodyTooLarge(Exception):
    """Raised by ``_json`` when the request body exceeds the registry cap."""


def registry_routes(store: SurfaceStore, keys: KeyStore | None) -> list[Route]:
    async def _list(_request: Request) -> JSONResponse:
        out = []
        for name in store.names():
            m = store.manifest(name)
            out.append(
                {"name": m["name"], "tier": m["tier"], "surface_rev": m["surface_rev"]}
            )
        return JSONResponse({"surfaces": out})

    async def _fetch(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        surface = store.get(name)
        if surface is None:
            return JSONResponse({"error": "unknown_surface"}, status_code=404)
        if surface.tier != "free":
            plain = request.headers.get(KEY_HEADER, "")
            rec = keys.check(plain) if (keys and plain) else None
            if rec is None or name not in rec.get("surfaces", []):
                return JSONResponse(
                    {
                        "error": "entitlement_required",
                        "remediation": "POST /registry/keys {email} then ask for "
                        f"access to {name!r} — flat per-surface entitlement.",
                    },
                    status_code=402,
                )
        return JSONResponse(store.manifest(name))

    async def _keys_start(request: Request) -> JSONResponse:
        if keys is None:
            return JSONResponse({"error": "issuance_disabled"}, status_code=503)
        try:
            body = await _json(request)
        except _BodyTooLarge:
            return JSONResponse({"error": "too_large"}, status_code=413)
        email = str(body.get("email", "")).strip()
        if "@" not in email or len(email) > 254:
            return JSONResponse({"error": "invalid_email"}, status_code=400)
        ip = request.client.host if request.client else "unknown"
        if _ip_throttled(ip):
            # Same 202 shape as the real path — never reveal throttling to enumerators.
            return JSONResponse({"status": "code_sent_if_valid"}, status_code=202)
        try:
            keys.start_otp(email)
        except RegistryAuthError:
            pass  # do not leak rate-limit state to enumerators
        return JSONResponse({"status": "code_sent_if_valid"}, status_code=202)

    async def _keys_verify(request: Request) -> JSONResponse:
        if keys is None:
            return JSONResponse({"error": "issuance_disabled"}, status_code=503)
        try:
            body = await _json(request)
        except _BodyTooLarge:
            return JSONResponse({"error": "too_large"}, status_code=413)
        try:
            plain = keys.verify_otp(
                str(body.get("email", "")), str(body.get("otp", ""))
            )
        except RegistryAuthError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        return JSONResponse({"key": plain})

    return [
        Route("/registry/surfaces", endpoint=_list),
        Route("/registry/surfaces/{name}", endpoint=_fetch),
        Route("/registry/keys", endpoint=_keys_start, methods=["POST"]),
        Route("/registry/keys/verify", endpoint=_keys_verify, methods=["POST"]),
    ]


async def _json(request: Request) -> dict[str, Any]:
    # Size cap BEFORE reading the body (Content-Length hint) and again after —
    # same convention as http_server._comprehend's MAX_COMPREHEND_REQUEST_BYTES.
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit():
        if int(declared) > MAX_REGISTRY_REQUEST_BYTES:
            raise _BodyTooLarge()
    raw = await request.body()
    if len(raw) > MAX_REGISTRY_REQUEST_BYTES:
        raise _BodyTooLarge()
    try:
        body = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001 - malformed body is a client error, not ours
        return {}
    return body if isinstance(body, dict) else {}


def _ip_throttled(ip: str) -> bool:
    """Return True (and record the attempt) if ``ip`` has hit the hourly cap.

    Bounded in-memory map keyed by client IP -> (count, window_start). When
    the map grows past ``_IP_THROTTLE_MAX_ENTRIES`` (a slow-drip DoS on this
    process's memory), expired windows are swept before recording the new one.
    If the map still exceeds the limit after sweep, oldest-window-first eviction
    is applied to maintain the hard cap.
    """
    now = time.time()
    if len(_ip_counts) > _IP_THROTTLE_MAX_ENTRIES:
        for key, (_, window_start) in list(_ip_counts.items()):
            if now - window_start >= _IP_THROTTLE_WINDOW_SECONDS:
                del _ip_counts[key]
        # Hard-cap: if still above limit after expiry sweep, evict oldest-first.
        if len(_ip_counts) > _IP_THROTTLE_MAX_ENTRIES:
            to_delete = len(_ip_counts) - _IP_THROTTLE_MAX_ENTRIES
            for ip_key, _ in sorted(_ip_counts.items(), key=lambda kv: kv[1][1])[
                :to_delete
            ]:
                del _ip_counts[ip_key]
    count, window_start = _ip_counts.get(ip, (0, now))
    if now - window_start >= _IP_THROTTLE_WINDOW_SECONDS:
        count, window_start = 0, now
    if count >= _IP_THROTTLE_MAX_PER_HOUR:
        _ip_counts[ip] = (count, window_start)
        return True
    _ip_counts[ip] = (count + 1, window_start)
    return False
