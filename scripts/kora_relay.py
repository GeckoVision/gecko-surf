"""The Kora relay client — a fee payer that is NOT us, behind :class:`gecko.relay.FeePayerRelay`.

Kora holds a funded key and signs as fee payer so the buyer needs no SOL. This file holds
an API key that lets us ASK it to, and nothing else. It lives in ``scripts/`` for the same
reason ``privy_backend.py`` does: ``gecko/`` talks to no relay, and
``gecko/kora_surface.py`` keeps ``signTransaction`` RECORDED on every public mount because
serving it live would let any anonymous caller drain the fee payer. This client is the
operator's own, pointed at the operator's own node, with the operator's own key.

**``signTransaction``, NEVER ``signAndSendTransaction``.** The second is disabled in
``examples/kora_demo/kora.gecko.toml`` and this file cannot name it: the method is a
constant. Kora signs; :func:`gecko.autonomous_purchase.settle_sponsored` re-verifies,
collects the buyer's signature, and submits.

**WHAT COMES BACK IS NOT WHAT WAS SENT**, on purpose. With Lighthouse enabled Kora appends
a balance assertion before signing. This client hands the answer back untouched;
:func:`gecko.relay.accept_relay_signature` decides whether the difference is the one
permitted difference. Nothing here trims, repairs or re-encodes.

Three wire facts, verified against a running node and recorded nowhere in Kora's docs
(``gecko/kora_surface.py`` records them too): parameters are NAMED (a positional list is
refused with ``invalid type: map, expected a string``); a no-argument method takes ``{}``;
the API key travels in an ``x-api-key`` header.

**REDACTION.** The API key never enters an exception, a log line or a repr. Every failure
raises :class:`KoraRelayError` with a failure class and, at most, an HTTP status.

Pattern B: ``transport`` is injected, so every refusal here is falsifiable offline
(``tests/test_kora_relay.py``). The live relay is the last check, never the debugger.

Configure from the environment::

    KORA_RPC_URL=http://127.0.0.1:8080   # the operator's own node
    KORA_API_KEY=...                     # never logged
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.cosign import CosignRefused, signature_slots  # noqa: E402
from gecko.rpc import user_agent, validate_rpc_url  # noqa: E402

#: The signing method. Named once. ``signAndSendTransaction`` is deliberately absent.
SIGN_METHOD = "signTransaction"
PAYER_METHOD = "getPayerSigner"
TIMEOUT_SECONDS = 20


class KoraRelayError(Exception):
    """A Kora call did not produce a signature. Carries a class of failure, never a key."""


@dataclass(frozen=True)
class KoraRequest:
    """One outbound JSON-RPC call, as data, so a test can assert what would be sent."""

    method: str
    #: NAMED parameters. Kora refuses a positional list outright.
    params: Mapping[str, Any]


#: The transport seam: base URL, request, ready-made headers -> the parsed JSON-RPC reply.
#: The API key is IN the headers the transport receives, and nowhere else.
Transport = Callable[[str, KoraRequest, Mapping[str, str]], dict[str, Any]]


def _default_transport(
    base_url: str, request: KoraRequest, headers: Mapping[str, str]
) -> dict[str, Any]:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": request.method,
            "params": dict(request.params),
        }
    ).encode()
    req = urllib.request.Request(base_url, data=body, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310
            return json.loads(resp.read())  # type: ignore[no-any-return]
    except urllib.error.HTTPError as exc:
        # Status only. The body may echo the request, which carries the transaction and
        # the header context; neither belongs in an exception.
        raise KoraRelayError(
            f"kora answered HTTP {exc.code} to {request.method}"
        ) from None
    except urllib.error.URLError as exc:
        raise KoraRelayError(
            f"kora unreachable for {request.method} ({type(exc.reason).__name__})"
        ) from None


@dataclass(frozen=True)
class KoraRelay:
    """A :class:`gecko.relay.FeePayerRelay` over one Kora node."""

    base_url: str
    _api_key: str
    _pubkey: str
    transport: Transport = _default_transport

    def __repr__(self) -> str:  # never the key
        return f"KoraRelay(base_url={self.base_url!r}, pubkey={self._pubkey!r})"

    @classmethod
    def open(
        cls, base_url: str, *, api_key: str, transport: Transport | None = None
    ) -> KoraRelay:
        """Ask the node which account it signs as, and pin it.

        Fails early: a relay whose payer cannot be read at configuration time must say so
        before a caller has verified bytes in hand and a blockhash ticking.
        """
        validate_rpc_url(base_url)
        if not api_key:
            raise KoraRelayError("no API key; the relay refuses unauthenticated calls")
        carrier = transport or _default_transport
        reply = _call(carrier, base_url, api_key, KoraRequest(PAYER_METHOD, {}))
        payer = reply.get("signer_address")
        if not isinstance(payer, str) or not payer:
            raise KoraRelayError("getPayerSigner returned no signer_address")
        return cls(
            base_url=base_url, _api_key=api_key, _pubkey=payer, transport=carrier
        )

    @classmethod
    def from_env(cls, *, transport: Transport | None = None) -> KoraRelay:
        url = os.environ.get("KORA_RPC_URL", "").strip()
        key = os.environ.get("KORA_API_KEY", "").strip()
        if not url:
            raise KoraRelayError("KORA_RPC_URL is not set")
        return cls.open(url, api_key=key, transport=transport)

    @property
    def pubkey(self) -> str:
        return self._pubkey

    def sign_as_fee_payer(self, unsigned_transaction_base64: str) -> str:
        """``signTransaction``: Kora validates against its policy, signs slot 0, returns.

        ``signer_key`` pins which of the node's signers answers, so a multi-signer pool
        cannot hand back a signature from an account the bytes do not name.

        ``user_id`` is REQUIRED by Kora when usage tracking is on and pricing is free
        (measured 2026-09-16: ``ValidationError("user_id is required when usage tracking
        is enabled and pricing is free")``). It is the account Kora's per-wallet limit
        counts against, so it is read from the bytes rather than asked of the caller:
        the first required signer that is not the fee payer, the buyer who authorises
        the spend. A single-signer transaction names the payer itself.
        """
        reply = _call(
            self.transport,
            self.base_url,
            self._api_key,
            KoraRequest(
                SIGN_METHOD,
                {
                    "transaction": unsigned_transaction_base64,
                    "signer_key": self._pubkey,
                    "user_id": _authority_of(unsigned_transaction_base64),
                },
            ),
        )
        signed = reply.get("signed_transaction")
        if not isinstance(signed, str) or not signed:
            raise KoraRelayError("signTransaction returned no signed_transaction")
        who = reply.get("signer_pubkey")
        if who != self._pubkey:
            raise KoraRelayError(
                "signTransaction was answered by a signer other than the pinned payer"
            )
        return signed


def _authority_of(unsigned_transaction_base64: str) -> str:
    """The signer Kora's usage limit is charged to: the first non-payer signer."""
    try:
        slots = signature_slots(unsigned_transaction_base64)
    except CosignRefused as exc:
        raise KoraRelayError(
            f"the transaction's signers could not be read ({exc.code})"
        ) from None
    if not slots:
        raise KoraRelayError("the transaction names no signer")
    return slots[1] if len(slots) > 1 else slots[0]


def _call(
    transport: Transport, base_url: str, api_key: str, request: KoraRequest
) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": user_agent(),
        "x-api-key": api_key,
    }
    try:
        reply = transport(base_url, request, headers)
    except KoraRelayError:
        raise
    except Exception as exc:  # noqa: BLE001 - the transport's text is untrusted
        raise KoraRelayError(
            f"transport failed for {request.method} ({type(exc).__name__})"
        ) from None
    if not isinstance(reply, dict):
        raise KoraRelayError(f"{request.method} returned a non-object reply")
    error = reply.get("error")
    if error is not None:
        code = error.get("code") if isinstance(error, dict) else None
        raise KoraRelayError(f"kora refused {request.method} (code={code})")
    result = reply.get("result")
    if not isinstance(result, dict):
        raise KoraRelayError(f"{request.method} returned no result object")
    return result
