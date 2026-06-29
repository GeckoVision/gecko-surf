"""Access layer — establish an authenticated TxODDS session for an agent.

Encodes the flow the agent never has to learn:
  guest JWT  ->  on-chain subscribe (txSig)  ->  sign(txSig:leagues:jwt)  ->  activate -> apiToken

and the two-token auth it produces:
  Authorization: Bearer <session JWT>   (httpAuth)
  X-Api-Token:   <long-lived apiToken>  (apiKeyAuth)

The on-chain `subscribe` itself is out of scope here (it's a wallet-signing,
network-specific step — see scripts/). This layer takes the resulting txSig +
a `signer` and finishes the session. Transport + signer are injected, so the
whole flow is unit-testable with no network and no keys.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

AUTH_JWT_HEADER = "Authorization"
AUTH_APITOKEN_HEADER = "X-Api-Token"

# transport(method, url, headers, json_body) -> (status, parsed_body)
Transport = Callable[[str, str, dict, Any], tuple[int, Any]]
# signer(message_bytes) -> base64 ed25519 detached signature
Signer = Callable[[bytes], str]


@runtime_checkable
class AuthSession(Protocol):
    """The whole engine/adapter seam: produce the auth headers for a request.

    Any object with ``auth_headers() -> dict[str, str]`` is a valid session. A
    paywalled API returns its tokens; a public API returns an empty dict.
    """

    def auth_headers(self) -> dict[str, str]: ...


@dataclass
class Session:
    jwt: str
    api_token: str

    def auth_headers(self) -> dict[str, str]:
        return {
            AUTH_JWT_HEADER: f"Bearer {self.jwt}",
            AUTH_APITOKEN_HEADER: self.api_token,
        }


def activation_message(tx_sig: str, leagues: list[int], jwt: str) -> bytes:
    """The exact message TxODDS expects the wallet to sign."""
    return f"{tx_sig}:{','.join(str(x) for x in leagues)}:{jwt}".encode("utf-8")


def live_transport(
    method: str, url: str, headers: dict, json_body: Any
) -> tuple[int, Any]:
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    hdrs = dict(headers)
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        body = resp.read().decode("utf-8")
        try:
            return resp.status, json.loads(body)
        except json.JSONDecodeError:
            return resp.status, body


def start_guest(base_url: str, transport: Transport = live_transport) -> str:
    status, body = transport(
        "POST", f"{base_url.rstrip('/')}/auth/guest/start", {}, None
    )
    if isinstance(body, dict) and "token" in body:
        return body["token"]
    raise RuntimeError(f"guest/start did not return a token (status {status})")


def activate(
    base_url: str,
    tx_sig: str,
    leagues: list[int],
    jwt: str,
    wallet_signature_b64: str,
    transport: Transport = live_transport,
) -> str:
    status, body = transport(
        "POST",
        f"{base_url.rstrip('/')}/api/token/activate",
        {AUTH_JWT_HEADER: f"Bearer {jwt}"},
        {"txSig": tx_sig, "walletSignature": wallet_signature_b64, "leagues": leagues},
    )
    # activate returns the api token (text/plain per the spec)
    if isinstance(body, dict):
        token = body.get("token")
    else:
        token = str(body).strip()
    if not token:
        raise RuntimeError(f"activate did not return an api token (status {status})")
    return token


def establish_session(
    base_url: str,
    tx_sig: str,
    leagues: list[int],
    signer: Signer,
    transport: Transport = live_transport,
) -> Session:
    jwt = start_guest(base_url, transport)
    signature = signer(activation_message(tx_sig, leagues, jwt))
    api_token = activate(base_url, tx_sig, leagues, jwt, signature, transport)
    return Session(jwt=jwt, api_token=api_token)


def stub_session() -> Session:
    """A non-live session for recorded-mode demos (auth headers present, no real token)."""
    return Session(jwt="STUB_SESSION_JWT", api_token="STUB_API_TOKEN")


@dataclass
class NoAuthSession:
    """Adapter for public, no-auth APIs (e.g. Pegana's public reads).

    The ~empty adapter the architecture promises: the engine consumes auth as an
    opaque header dict, and a no-auth API simply yields an empty one. An empty
    ``auth_headers()`` also signals the client to hide any auth-gated operations
    from the agent (it can't satisfy them).
    """

    def auth_headers(self) -> dict[str, str]:
        return {}


def public_session() -> NoAuthSession:
    """A session for APIs whose endpoints need no auth (public reads only)."""
    return NoAuthSession()
