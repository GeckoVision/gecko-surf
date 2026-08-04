"""The single canonical JSON-RPC transport seam for Gecko's on-chain reads.

Extracted from :mod:`gecko.pda_testkit` to fix a layering bug: the resolution engine
(:mod:`gecko.pda_resolve`) needs a transport, but was importing it from a *tests-only*
module. Transport is domain-neutral plumbing, so it lives here; both ``pda_testkit``
(the on-chain verify gate) and ``pda_resolve`` (the control-plane read engine) depend
DOWN onto this module, not sideways onto each other.

The transport is injectable everywhere (``RpcCall``) so every engine path is unit-testable
with no network; :func:`default_rpc_call` is the loopback/HTTP default.

Control-plane invariant #1 holds: callers read only PUBLIC on-chain metadata
(``getAccountInfo`` owner/lamports, ``simulateTransaction`` results); no payload data,
user data, or secret is ever stored — this module only moves bytes over the wire.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse

__all__ = [
    "RpcCall",
    "RpcError",
    "LOCAL_RPC",
    "validate_rpc_url",
    "default_rpc_call",
]

# A JSON-RPC caller: (rpc_url, method, params) -> the parsed response dict.
RpcCall = Callable[[str, str, list[Any]], dict[str, Any]]

LOCAL_RPC = "http://127.0.0.1:8899"

# Schemes we will POST JSON-RPC to. The RPC endpoint is a user-CONFIGURED transport
# target (surfpool fork on loopback OR a public mainnet RPC), not ingested spec content,
# so the anti-SSRF private-range block (which guards untrusted specs) deliberately does
# not apply — but a non-http scheme (file://, ftp://, ws://) is always rejected.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class RpcError(Exception):
    """A JSON-RPC transport or protocol failure — a bad URL scheme or an ``error`` field
    in the response. Messages carry only the method + JSON-RPC code/message (public
    metadata); the request params body (which may be a large serialized tx) is never
    echoed, so no secret can leak through an error."""


def validate_rpc_url(rpc_url: str) -> None:
    """Reject anything that is not an ``http``/``https`` endpoint.

    Loopback AND public hosts are both allowed on purpose (surfpool fork + mainnet);
    only the scheme is gated. Raises :class:`RpcError` otherwise.
    """
    scheme = urlparse(rpc_url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise RpcError(
            f"RPC url must be http/https, got scheme {scheme!r} — "
            "refusing non-http transport"
        )


def _http_post_json(url: str, body: bytes) -> dict[str, Any]:
    """POST ``body`` as JSON and parse the response. Seam kept tiny so tests inject here."""
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - scheme validated
        return json.loads(resp.read())  # type: ignore[no-any-return]


def default_rpc_call(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
    """POST a JSON-RPC request and return the parsed response.

    Validates the URL scheme first, then POSTs ``{jsonrpc, id, method, params}``. If the
    response carries a JSON-RPC ``error`` field, raises :class:`RpcError` with the method
    and the error code/message only — never the params body.
    """
    validate_rpc_url(rpc_url)
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    resp = _http_post_json(rpc_url, body)
    error = resp.get("error")
    if error is not None:
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else error
        raise RpcError(f"JSON-RPC {method} failed: code={code} message={message!r}")
    return resp
