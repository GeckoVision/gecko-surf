"""Registry HTTP surface — mounted into the existing multi-surface server.

Anonymous fetch for free surfaces; ``X-Gecko-Key`` + flat per-surface
entitlement for premium ones (402 entitlement_required otherwise).
"""

from __future__ import annotations

import json
import time
from pathlib import Path as _Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gecko.access import public_session
from gecko.client import AgentApiClient
from gecko.preflight_corpus import PreflightCorpusError, assert_classes_closed

from .class_wallets import ClassWalletError, ClassWalletRegistrar
from .devnet_faucet import DevnetFaucet, FaucetError
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

# The class-wallet routes get their OWN bucket with a higher cap: on demo day a whole
# classroom sits behind one NAT address, and sharing the OTP bucket (10/h) would lock
# the room out after five students. Still bounded; every hit is key-authenticated.
_CLASS_WALLET_MAX_PER_HOUR = 120
_class_wallet_ip_counts: dict[str, tuple[int, float]] = {}

# The devnet faucet is keyless, so this bucket is all that bounds how fast it drains.
# A classroom shares one NAT address; 40/h lets a room fund itself, and every call is
# idempotent on-chain, so a retry costs a request, never tokens.
_FAUCET_MAX_PER_HOUR = 40
_faucet_ip_counts: dict[str, tuple[int, float]] = {}


class _BodyTooLarge(Exception):
    """Raised by ``_json`` when the request body exceeds the registry cap."""


def registry_routes(
    store: SurfaceStore,
    keys: KeyStore | None,
    feedback_path: str | None = None,
    class_wallets: ClassWalletRegistrar | None = None,
    faucet: DevnetFaucet | None = None,
) -> list[Route]:
    # One AgentApiClient per surface, built lazily on first search and cached — search
    # runs the full ingest+catalog build, so this avoids redoing that on every request.
    _clients: dict[str, AgentApiClient] = {}

    def _make_client(name: str) -> AgentApiClient:
        surface = store.get(name)
        assert surface is not None
        return AgentApiClient(surface.spec, session=public_session())

    def _entitled(request: Request, name: str) -> bool:
        """Free surfaces are always entitled; premium needs a keyed entitlement.

        Shared by ``_fetch`` (which turns a False into a 402) and ``_search``
        (which silently skips unentitled surfaces) so the paywall can't drift
        between the two read paths.
        """
        surface = store.get(name)
        if surface is None:
            return False
        if surface.tier == "free":
            return True
        plain = request.headers.get(KEY_HEADER, "")
        rec = keys.check(plain) if (keys and plain) else None
        return rec is not None and name in rec.get("surfaces", [])

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
        if not _entitled(request, name):
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

    async def _search(request: Request) -> JSONResponse:
        intent = request.query_params.get("intent", "").strip()[:200]
        if not intent:
            return JSONResponse({"error": "missing intent"}, status_code=400)
        results = []
        for name in store.names():
            if not _entitled(request, name):
                continue
            if name not in _clients:
                _clients[name] = _make_client(name)
            client = _clients[name]
            hits = client.search(intent, limit=3)
            if hits:
                results.append({"surface": name, "hits": hits})
        return JSONResponse({"results": results})

    async def _feedback(request: Request) -> JSONResponse:
        # Closed-vocabulary corpus classes ONLY — same control-plane invariant as
        # preflight_corpus: a record's `classes` must all be on the known vocabulary, and
        # nothing else from the request body reaches disk (see the allowlisted `record`
        # dict below — a stray field never smuggles a value out).
        if feedback_path is None:
            return JSONResponse({"error": "feedback_disabled"}, status_code=503)
        ip = request.client.host if request.client else "unknown"
        if _ip_throttled(ip):
            # Silent, no oracle: same 204 shape as success, nothing written to disk.
            return JSONResponse(None, status_code=204)
        try:
            body = await _json(request)
        except _BodyTooLarge:
            return JSONResponse({"error": "too_large"}, status_code=413)
        classes = body.get("classes")
        if not isinstance(classes, list) or not classes:
            return JSONResponse({"error": "classes required"}, status_code=400)
        class_strs = [str(c) for c in classes]
        try:
            assert_classes_closed(class_strs)
        except PreflightCorpusError:
            return JSONResponse({"error": "unknown class"}, status_code=400)
        record = {
            "surface": str(body.get("surface", ""))[:64],
            "surface_rev": str(body.get("surface_rev", ""))[:64],
            "classes": class_strs,
        }
        target = _Path(feedback_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        return JSONResponse(None, status_code=204)

    def _class_wallet_refusal(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse({"error": message, "code": code}, status_code=status)

    def _class_wallet_throttled(request: Request) -> bool:
        ip = request.client.host if request.client else "unknown"
        return _throttled(_class_wallet_ip_counts, ip, _CLASS_WALLET_MAX_PER_HOUR)

    async def _class_wallet_challenge(request: Request) -> JSONResponse:
        # Same stance as the keys routes: no MONGODB_URI -> the door says so, no 404.
        if class_wallets is None:
            return _class_wallet_refusal(503, "not-enabled", "not enabled")
        if _class_wallet_throttled(request):
            return _class_wallet_refusal(429, "rate-limited", "too many requests")
        try:
            issued = class_wallets.issue_challenge(
                _bearer(request), request.query_params.get("cohort")
            )
        except ClassWalletError as exc:
            return _class_wallet_refusal(exc.status, exc.code, exc.message)
        return JSONResponse(issued)

    async def _class_wallet_register(request: Request) -> JSONResponse:
        # Same stance as the keys routes: no MONGODB_URI -> the door says so, no 404.
        if class_wallets is None:
            return _class_wallet_refusal(503, "not-enabled", "not enabled")
        if _class_wallet_throttled(request):
            return _class_wallet_refusal(429, "rate-limited", "too many requests")
        try:
            body = await _json(request)
        except _BodyTooLarge:
            return _class_wallet_refusal(413, "too-large", "request body too large")
        try:
            registration = class_wallets.register(_bearer(request), body)
        except ClassWalletError as exc:
            return _class_wallet_refusal(exc.status, exc.code, exc.message)
        return JSONResponse(registration.as_json())

    async def _faucet_fund(request: Request) -> JSONResponse:
        if faucet is None:
            return _class_wallet_refusal(
                503, "not-enabled", "the devnet faucet is not enabled"
            )
        ip = request.client.host if request.client else "unknown"
        if _throttled(_faucet_ip_counts, ip, _FAUCET_MAX_PER_HOUR):
            return _class_wallet_refusal(429, "rate-limited", "too many requests")
        try:
            body = await _json(request)
        except _BodyTooLarge:
            return _class_wallet_refusal(413, "too-large", "request body too large")
        buyer = body.get("buyer")
        if not isinstance(buyer, str) or not buyer.strip():
            return _class_wallet_refusal(
                400, "bad-address", 'send {"buyer": "<your buyer address>"}'
            )
        try:
            funded = await run_in_threadpool(faucet.fund, buyer.strip())
        except FaucetError as exc:
            return _class_wallet_refusal(exc.status, exc.code, exc.message)
        return JSONResponse(funded.as_json())

    return [
        Route("/registry/surfaces", endpoint=_list),
        Route("/registry/surfaces/{name}", endpoint=_fetch),
        Route("/registry/keys", endpoint=_keys_start, methods=["POST"]),
        Route("/registry/keys/verify", endpoint=_keys_verify, methods=["POST"]),
        Route("/registry/search", endpoint=_search),
        Route("/registry/feedback", endpoint=_feedback, methods=["POST"]),
        Route(
            "/registry/class-wallet/challenge",
            endpoint=_class_wallet_challenge,
            methods=["GET"],
        ),
        Route(
            "/registry/class-wallet",
            endpoint=_class_wallet_register,
            methods=["POST"],
        ),
        Route("/registry/class-wallet/faucet", endpoint=_faucet_fund, methods=["POST"]),
    ]


def _bearer(request: Request) -> str:
    """The presented Gecko key from ``Authorization: Bearer``, or "" — never logged."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


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
    """Return True (and record the attempt) if ``ip`` has hit the hourly OTP cap."""
    return _throttled(_ip_counts, ip, _IP_THROTTLE_MAX_PER_HOUR)


def _throttled(counts: dict[str, tuple[int, float]], ip: str, cap: int) -> bool:
    """Return True (and record the attempt) if ``ip`` has hit ``cap`` this hour.

    Bounded in-memory map keyed by client IP -> (count, window_start). When
    the map grows past ``_IP_THROTTLE_MAX_ENTRIES`` (a slow-drip DoS on this
    process's memory), expired windows are swept before recording the new one.
    If the map still exceeds the limit after sweep, oldest-window-first eviction
    is applied to maintain the hard cap.
    """
    now = time.time()
    if len(counts) > _IP_THROTTLE_MAX_ENTRIES:
        for key, (_, window_start) in list(counts.items()):
            if now - window_start >= _IP_THROTTLE_WINDOW_SECONDS:
                del counts[key]
        # Hard-cap: if still above limit after expiry sweep, evict oldest-first.
        if len(counts) > _IP_THROTTLE_MAX_ENTRIES:
            to_delete = len(counts) - _IP_THROTTLE_MAX_ENTRIES
            for ip_key, _ in sorted(counts.items(), key=lambda kv: kv[1][1])[
                :to_delete
            ]:
                del counts[ip_key]
    count, window_start = counts.get(ip, (0, now))
    if now - window_start >= _IP_THROTTLE_WINDOW_SECONDS:
        count, window_start = 0, now
    if count >= cap:
        counts[ip] = (count, window_start)
        return True
    counts[ip] = (count + 1, window_start)
    return False
