"""Mode B, the whole chain, with no network and no key: does a BOUND account actually buy
with its own wallet when the call arrives over the transport we deploy?

Every link has been proven on its own — the gate resolves an account (`keyauth`), the
record maps it to a wallet (`wallet_binding`, #420), the scope carries it to the tool
(#422), and `prepare_purchase` refuses a buyer that is not the bound one
(`test_wallet_binding`). Four green files still do not say the chain is connected, and
until this ran, it was not: the wire was the piece missing, and nothing served would have
noticed, because the only surface that reads an account is public and therefore always in
mode A.

So this asserts the join, from an HTTP request with a bearer key down to the derived
account plan, on the shape production serves — a surface mounted under a name, gated at
the Mount. It signs nothing and reaches no network: the builder and the RPC are fakes,
which is the point (Pattern B — the free local simulation is the first deliverable, not
the last).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("mcp")

from starlette.testclient import TestClient  # noqa: E402

from gecko.http_server import build_multi_surface_app  # noqa: E402
from gecko.keyauth import KeyGate  # noqa: E402
from gecko.providers.catalog_surface import OrquestraCatalogSurface  # noqa: E402
from gecko.wallet_binding import (  # noqa: E402
    InMemoryWalletDirectory,
    WalletBinding,
)

from tests.test_prepare_purchase_tool import (  # noqa: E402
    BUYER,
    RPC_URL,
    FakeBuilder,
    FakeRpc,
)
from tests.test_wallet_binding import (  # noqa: E402
    ACCOUNT,
    OTHER_PUBKEY,
    WALLET_ID,
)

TOKEN = "a-valid-gecko-key"
SURFACE = "store"


class _OneAccount:
    def is_enabled(self, account: str) -> bool:
        return account == ACCOUNT

    def may_access(self, account: str, surface: str) -> bool:
        return self.is_enabled(account)


def _gate() -> KeyGate:
    return KeyGate(
        resolve_account=lambda token: ACCOUNT if token == TOKEN else None,
        allowlist=_OneAccount(),
    )


def _bound() -> InMemoryWalletDirectory:
    directory = InMemoryWalletDirectory()
    directory.bind(
        WalletBinding(account_id=ACCOUNT, wallet_id=WALLET_ID, pubkey=BUYER)
    )
    return directory


def _purchase(*, gated: bool, token: str | None, **overrides: Any) -> dict[str, Any]:
    """Drive `prepare_purchase` over the served MCP transport and return its result."""
    surface = OrquestraCatalogSurface(
        wallets=_bound(),
        purchase_build_call=FakeBuilder(),
        purchase_rpc_call=FakeRpc(),
        find_start_pages=0,  # never touch the partner's catalog from a test
    )
    app = build_multi_surface_app(
        [(SURFACE, surface)],
        allowed_hosts=["testserver"],
        require_gecko_key=gated,
        gated_surfaces=[SURFACE] if gated else None,
        key_gate=_gate() if gated else None,
    )
    args: dict[str, Any] = {
        "store": "jonasbar",
        "product": "Water",
        "network": "mainnet",
        "rpc_url": RPC_URL,
        "table": 11,
    }
    args.update(overrides)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with TestClient(app) as client:
        init = client.post(
            f"/{SURFACE}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "1"},
                },
            },
            headers=headers,
        )
        assert init.status_code == 200, init.text
        session = {"mcp-session-id": init.headers["mcp-session-id"]}
        client.post(
            f"/{SURFACE}/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**headers, **session},
        )
        response = client.post(
            f"/{SURFACE}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "prepare_purchase", "arguments": args},
            },
            headers={**headers, **session},
        )
    return _tool_result(response.text)


def _tool_result(raw: str) -> dict[str, Any]:
    """Pull the tool's JSON out of the SSE frame the transport answers with."""
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        message = json.loads(line[len("data:") :].strip())
        content = (message.get("result") or {}).get("content") or []
        if content:
            return dict(json.loads(content[0]["text"]))
    raise AssertionError(f"no tool result in the response: {raw[:400]}")


def _signer_of(result: dict[str, Any]) -> str:
    return next(e["address"] for e in result["accounts"] if e["account"] == "signer")


# --- mode B: the buyer comes from the record ---------------------------------


def test_a_bound_account_buys_with_its_own_wallet_and_never_says_which() -> None:
    """No `buyer` in the arguments at all. The wallet is reached from the bearer key alone
    — gate -> account -> record -> derived plan — which is the entire hosted-signer
    premise: the caller does not name the wallet, so the caller cannot name a wrong one."""
    result = _purchase(gated=True, token=TOKEN)

    assert result.get("refused") is not True, result
    assert _signer_of(result) == BUYER


def test_naming_somebody_elses_address_is_refused_over_the_wire_too() -> None:
    """The refusal is the package's (`buyer-not-bound`) and it survives the transport
    intact — a boundary that only holds when called directly is not a boundary."""
    result = _purchase(gated=True, token=TOKEN, buyer=OTHER_PUBKEY)

    assert result["refused"] is True
    assert result["code"] == "buyer-not-bound"
    assert "transaction" not in result
    assert OTHER_PUBKEY not in json.dumps(result)


# --- mode A: the same surface, no key in front of it -------------------------


def test_without_a_gate_the_caller_still_supplies_the_buyer() -> None:
    """The public mount's behaviour, unchanged and correct: nobody proved who they are, so
    the bytes are for whoever the caller says, and they are unsigned."""
    result = _purchase(gated=False, token=None, buyer=OTHER_PUBKEY)

    assert result.get("refused") is not True, result
    assert _signer_of(result) == OTHER_PUBKEY


def test_an_unauthenticated_caller_cannot_reach_the_binding_by_omitting_the_buyer() -> (
    None
):
    """The impersonation attempt that costs nothing to try: leave the buyer out and hope
    the surface fills it in from a record. With no gate there is no account, so there is
    nothing to look up — and the refusal must be about the MISSING buyer, never a wallet."""
    result = _purchase(gated=False, token=None)

    assert result["refused"] is True
    assert BUYER not in json.dumps(result)
