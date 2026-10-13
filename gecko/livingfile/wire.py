"""The two wire edges of the living file's free reads. Recorded mode swaps both.

``PublicFetch`` (url -> (status, text)) reads Jupiter and the legal pages through
``netguard.safe_get_status``: https only, every hop re-validated against private and
metadata addresses, the socket pinned to the validated IP, the size capped.

``live_rpc_call`` honours the engine's ``rpc.RpcCall`` contract (same request body, same
``RpcError`` that carries the method and the JSON-RPC code, never the params), so any
``RpcCall`` — ``rpc.default_rpc_call``, a test fake — plugs in. Its wire is
``netguard.safe_post_json`` rather than ``rpc.post_json``: the RPC URL is a configured
public host here, never loopback, so it gets the same SSRF guard as every other fetch.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from ..netguard import Resolver, UnsafeUrlError, safe_get_status, safe_post_json
from ..rpc import RpcError, validate_rpc_url

#: url -> (HTTP status, body text).
PublicFetch = Callable[[str], tuple[int, str]]
#: A docs page is tens of KB; a JSON-RPC mint reply is a few KB.
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_RPC_BYTES = 1024 * 1024
TIMEOUT_S = 30


def live_public_fetch(url: str, *, resolver: Resolver | None = None) -> tuple[int, str]:
    if urlsplit(url).scheme != "https":
        raise UnsafeUrlError("the living file fetches over https only")
    return safe_get_status(
        url, max_bytes=MAX_PAGE_BYTES, timeout=TIMEOUT_S, resolver=resolver
    )


def live_rpc_call(
    rpc_url: str,
    method: str,
    params: list[Any],
    *,
    resolver: Resolver | None = None,
) -> dict[str, Any]:
    validate_rpc_url(rpc_url)
    if urlsplit(rpc_url).scheme != "https":
        raise UnsafeUrlError("the living file reads Solana RPC over https only")
    text = safe_post_json(
        rpc_url,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        max_bytes=MAX_RPC_BYTES,
        timeout=TIMEOUT_S,
        resolver=resolver,
    )
    try:
        resp = json.loads(text)
    except ValueError:
        raise RpcError(
            f"JSON-RPC {method} answered something that is not JSON"
        ) from None
    if not isinstance(resp, dict):
        raise RpcError(f"JSON-RPC {method} answered something that is not an object")
    error = resp.get("error")
    if error is not None:
        code = error.get("code") if isinstance(error, dict) else None
        raise RpcError(f"JSON-RPC {method} failed: code={code}")
    return resp
