"""Registry HTTP surface — mounted into the existing multi-surface server.

Anonymous fetch for free surfaces; ``X-Gecko-Key`` + flat per-surface
entitlement for premium ones (402 entitlement_required otherwise).
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .keys import KeyStore, RegistryAuthError
from .store import SurfaceStore

KEY_HEADER = "X-Gecko-Key"


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
        body = await _json(request)
        email = str(body.get("email", "")).strip()
        if "@" not in email or len(email) > 254:
            return JSONResponse({"error": "invalid_email"}, status_code=400)
        try:
            keys.start_otp(email)
        except RegistryAuthError:
            pass  # do not leak rate-limit state to enumerators
        return JSONResponse({"status": "code_sent_if_valid"}, status_code=202)

    async def _keys_verify(request: Request) -> JSONResponse:
        if keys is None:
            return JSONResponse({"error": "issuance_disabled"}, status_code=503)
        body = await _json(request)
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
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed body is a client error, not ours
        return {}
    return body if isinstance(body, dict) else {}
