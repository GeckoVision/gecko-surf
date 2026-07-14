"""Live x402 facilitator adapter — the HTTP relay behind ``X402_MODE=live``.

``HttpFacilitatorClient`` implements the neutral ``FacilitatorClient`` Protocol
(gecko/x402_pay.py) against a real x402 facilitator over stdlib urllib. It is a RELAY,
nothing more: it signs nothing, broadcasts nothing, and holds no funds or keys — the
facilitator verifies/settles; Gecko passes payloads through and keeps only the opaque
settlement reference (control plane, invariant #1).

The x402 facilitator wire convention — kept in THIS one place; the in-repo Protocol is
the source of truth for everything above it:

    POST {facilitator_url}/verify   {"x402Version": 1,
                                     "paymentPayload": <the X-PAYMENT payload>,
                                     "paymentRequirements": <the served terms>}
        -> 200 {"isValid": bool, "invalidReason": str|null}
    POST {facilitator_url}/settle   (same body shape)
        -> 200 {"success": bool, "errorReason": str|null, "transaction": str|null,
                "network": str|null, "payer": str|null}

Protocol mapping: ``verify`` returns ``isValid``; a successful ``settle`` becomes
``Settlement(reference=<transaction>)``. The Protocol's ``settle`` takes only the
payment, so the client remembers the requirements from the payment's last SUCCESSFUL
``verify`` (``settle_subscription`` always verifies first) and refuses to settle a
payment it never verified — a structural verify-before-settle gate.

FAIL CLOSED: a non-200 answer, malformed JSON, a missing/mistyped field, a transport
error, or an unverified payment raises ``FacilitatorError`` (``verify`` returns ``False``
only when the facilitator explicitly answers ``isValid: false``). Either way the billing
path grants NO entitlement on any doubt.

REDACTION: no exception message ever carries the bearer token or the X-PAYMENT payload —
errors carry the endpoint, an HTTP status, and a short scrubbed reason only.

SSRF: ``facilitator_url`` goes through ``validate_public_url`` at construction (fail
fast) and the default transport re-validates per call — a private/loopback/metadata
facilitator is rejected before any socket opens.

Mode semantics stay in gecko/x402_pay.py: ``stub`` is the shipped default; flipping
``live`` happens via deploy env only, with founder go-ahead (docs/x402-go-live.md).
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from .netguard import USER_AGENT, Resolver, validate_public_url

if TYPE_CHECKING:  # runtime import happens inside settle() — x402_pay re-exports us
    from .x402_pay import Settlement

# --- env contract (the deploy-side flip switch; see .env.example) ----------------------
FACILITATOR_URL_ENV = "X402_FACILITATOR_URL"
FACILITATOR_TOKEN_ENV = "X402_FACILITATOR_TOKEN"  # optional bearer  # noqa: S105
PAY_TO_ENV = "X402_PAY_TO"
ASSET_ENV = "X402_ASSET"
NETWORK_ENV = "X402_NETWORK"

# pay_to/asset/network are consumed by the Plan/policy the server injects, not by the
# HTTP client — but a live deploy without them would mint no valid 402, so the factory
# gates on ALL of them: a half-configured deploy fails at startup, not at the first 402.
_REQUIRED_LIVE_ENVS = (FACILITATOR_URL_ENV, PAY_TO_ENV, ASSET_ENV, NETWORK_ENV)

# Facilitator verdicts are tiny JSON bodies; cap reads defensively.
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024
# Bound the verified-requirements memory (digest -> requirements), FIFO-evicted.
_VERIFIED_CACHE_MAX = 32
_REASON_MAX_CHARS = 160

#: Transport seam (template: ``login.py``'s injected ``Post``): a callable
#: ``(url, json_body, headers, timeout_s) -> (status, raw_text)``. Injectable so the
#: whole adapter is falsifiable offline; the default is SSRF-validated stdlib urllib.
PostJson = Callable[[str, Mapping[str, Any], Mapping[str, str], float], tuple[int, str]]


class X402ConfigError(Exception):
    """``X402_MODE=live`` env config is incomplete/unusable. Names env VAR NAMES only —
    never a value, never a secret."""


class FacilitatorError(Exception):
    """A live facilitator interaction failed (transport, protocol, or refusal).

    FAIL CLOSED: the billing path treats this as no-verification / no-settlement — no
    entitlement is granted. The message carries the endpoint + status + a short scrubbed
    reason ONLY — never the bearer token, never the X-PAYMENT payload."""


def _default_post_json(
    url: str,
    body: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout_s: float,
) -> tuple[int, str]:
    """SSRF-validated JSON POST over stdlib urllib. Returns ``(status, raw_text)``.

    4xx/5xx are ANSWERS (returned with their body), not exceptions — the client decides
    what fails closed. Response reads are size-capped."""
    validate_public_url(url)  # defense-in-depth per call (mirrors login._default_post)
    data = json.dumps(dict(body)).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:  # noqa: S310 (validated)
            raw = resp.read(_MAX_RESPONSE_BYTES).decode("utf-8", "replace")
            return int(getattr(resp, "status", 200)), raw
    except urllib.error.HTTPError as exc:  # a real status; its body may carry a reason
        raw = exc.read(_MAX_RESPONSE_BYTES).decode("utf-8", "replace")
        return int(exc.code), raw


def _canonical_digest(payment: Mapping[str, Any]) -> str:
    """Canonical payment digest — the verify-then-settle correlation key.

    Mirrors ``x402_pay._canonical`` (kept local: x402_pay imports this module, so the
    dependency must not point back at module scope)."""
    try:
        canonical = json.dumps(payment, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        # Redact: name the failure class, never echo the payload.
        raise FacilitatorError(
            "payment payload is not JSON-serializable; refusing to relay"
        ) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class HttpFacilitatorClient:
    """The live ``FacilitatorClient`` adapter: relays verify/settle to an x402
    facilitator over HTTP. Signs nothing, broadcasts nothing, holds nothing.

    ``facilitator_url`` is validated (SSRF) at construction — fail fast, before any
    request. ``auth_token`` (optional) is sent as ``Authorization: Bearer`` and never
    appears in errors or logs. ``post`` is the injectable transport seam."""

    def __init__(
        self,
        facilitator_url: str,
        *,
        auth_token: str | None = None,
        timeout_s: float = 10.0,
        post: PostJson | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        validate_public_url(facilitator_url, resolver=resolver)
        self.facilitator_url = facilitator_url.rstrip("/")
        self.timeout_s = timeout_s
        self._auth_token = (auth_token or "").strip() or None
        self._post = post or _default_post_json
        # digest(payment) -> the requirements it verified against (FIFO, capped).
        self._verified: dict[str, dict[str, Any]] = {}

    # --- Protocol surface ---------------------------------------------------------
    def verify(
        self, payment: Mapping[str, Any], requirements: Mapping[str, Any]
    ) -> bool:
        """Relay to ``POST /verify``. ``True``/``False`` mirror the facilitator's
        explicit ``isValid``; every other outcome raises (fail closed)."""
        data = self._call("/verify", payment, requirements)
        is_valid = data.get("isValid")
        if not isinstance(is_valid, bool):
            raise FacilitatorError(
                "facilitator /verify answered without a boolean isValid; failing closed"
            )
        if is_valid:
            self._remember(payment, requirements)
        return is_valid

    def settle(self, payment: Mapping[str, Any]) -> Settlement:
        """Relay to ``POST /settle``; map success to ``Settlement(reference=tx)``.

        Only a payment this client VERIFIED can settle (the Protocol's ``settle``
        carries no requirements — we re-send the ones the payment verified against)."""
        from .x402_pay import (
            Settlement,
        )  # late import — x402_pay re-exports this module

        requirements = self._verified.get(_canonical_digest(payment))
        if requirements is None:
            raise FacilitatorError(
                "settle refused: payment was not verified by this client (verify first)"
            )
        data = self._call("/settle", payment, requirements)
        if data.get("success") is not True:
            raise FacilitatorError(
                "facilitator /settle failed: "
                + self._short_reason(data.get("errorReason"))
            )
        transaction = data.get("transaction")
        if not isinstance(transaction, str) or not transaction.strip():
            raise FacilitatorError(
                "facilitator /settle succeeded without a transaction reference; "
                "failing closed (no reference, no entitlement)"
            )
        return Settlement(reference=transaction)

    # --- wire plumbing ------------------------------------------------------------
    def _call(
        self, path: str, payment: Mapping[str, Any], requirements: Mapping[str, Any]
    ) -> dict[str, Any]:
        """POST the x402 wire envelope; return the parsed 200 body or raise
        ``FacilitatorError`` (non-200, malformed JSON, transport failure)."""
        envelope = {
            "x402Version": 1,
            "paymentPayload": payment,
            "paymentRequirements": requirements,
        }
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        try:
            status, raw = self._post(
                f"{self.facilitator_url}{path}", envelope, headers, self.timeout_s
            )
        except FacilitatorError:
            raise
        except Exception as exc:
            # REDACT: a transport error message is untrusted and could echo request
            # headers — sever the chain and keep only the failure class.
            raise FacilitatorError(
                f"facilitator {path} unreachable: {type(exc).__name__}"
            ) from None
        if status != 200:
            raise FacilitatorError(
                f"facilitator {path} answered HTTP {status}; failing closed"
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise FacilitatorError(
                f"facilitator {path} answered non-JSON; failing closed"
            ) from None
        if not isinstance(parsed, dict):
            raise FacilitatorError(
                f"facilitator {path} answered a non-object body; failing closed"
            )
        return parsed

    def _remember(
        self, payment: Mapping[str, Any], requirements: Mapping[str, Any]
    ) -> None:
        """Retain the requirements a payment verified against (bounded, FIFO-evicted),
        so the Protocol's requirement-less ``settle`` can re-send the exact terms."""
        if len(self._verified) >= _VERIFIED_CACHE_MAX:
            self._verified.pop(next(iter(self._verified)))
        # Deep copy: the stored terms must not drift if the caller mutates theirs.
        self._verified[_canonical_digest(payment)] = json.loads(
            json.dumps(dict(requirements))
        )

    def _short_reason(self, raw: Any) -> str:
        """A short, scrubbed reason for error messages — truncated, bearer redacted."""
        reason = str(raw).strip() if raw else "no reason given"
        if self._auth_token:
            reason = reason.replace(self._auth_token, "[redacted]")
        return reason[:_REASON_MAX_CHARS]


def facilitator_from_env(
    env: Mapping[str, str] | None = None,
    *,
    resolver: Resolver | None = None,
    post: PostJson | None = None,
) -> HttpFacilitatorClient:
    """Build the live client from deploy env — the ONLY way ``X402_MODE=live`` resolves.

    Requires ``X402_FACILITATOR_URL`` plus the treasury/asset/network config the billing
    ``Plan``/policy injects (``X402_PAY_TO`` / ``X402_ASSET`` / ``X402_NETWORK``); raises
    ``X402ConfigError`` naming exactly the missing vars (names only, never values).
    ``X402_FACILITATOR_TOKEN`` is optional. An unsafe (private/loopback/non-http)
    facilitator URL raises ``UnsafeUrlError`` at construction. ``env``/``resolver``/
    ``post`` are injectable for offline tests; defaults are ``os.environ`` + real DNS +
    stdlib urllib."""
    source: Mapping[str, str] = os.environ if env is None else env
    missing = [
        name for name in _REQUIRED_LIVE_ENVS if not (source.get(name) or "").strip()
    ]
    if missing:
        raise X402ConfigError(
            "X402_MODE=live requires env config; missing: " + ", ".join(missing)
        )
    token = (source.get(FACILITATOR_TOKEN_ENV) or "").strip() or None
    return HttpFacilitatorClient(
        source[FACILITATOR_URL_ENV].strip(),
        auth_token=token,
        post=post,
        resolver=resolver,
    )
