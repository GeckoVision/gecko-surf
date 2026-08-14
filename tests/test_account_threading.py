"""From the gate that verified the caller to the tool that needs to know who they are.

`gecko.wallet_binding` (#420) resolves `account_id -> wallet` so a buyer is LOOKED UP
rather than taken at the caller's word, and `gecko.keyauth` has resolved a stable account
id at the gate all along. Between them there was no wire: `_call_tool` passed a session id
and nothing else, so `account` was `None` on every served call and the binding could never
fire. This is that wire.

The one rule it exists to keep: **the account comes from the gate that verified it, and
from nowhere else.** Not from the arguments — those are the caller's own word, which is
precisely what the binding refuses to trust — and not from a scope key that arrived
already filled in.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import pytest

pytest.importorskip("mcp")

from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from gecko import http_server  # noqa: E402
from gecko.keyauth import KeyGate  # noqa: E402

BASE = "http://test"
TOKEN = "a-valid-gecko-key"
ACCOUNT = "did:privy:the-real-caller"
IMPOSTOR = "did:privy:somebody-else"


class _OneAccount:
    def is_enabled(self, account: str) -> bool:
        return account == ACCOUNT


def _gate() -> KeyGate:
    return KeyGate(
        resolve_account=lambda token: ACCOUNT if token == TOKEN else None,
        allowlist=_OneAccount(),
    )


class _RecordingSurface:
    """A duck-typed surface shaped like `OrquestraCatalogSurface`: `account` is
    keyword-only and defaults to None, because it can only ever come from the transport."""

    def __init__(self) -> None:
        self.seen: list[tuple[dict[str, Any], str | None]] = []

    def list_tools(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "name": "who_am_i",
                "description": "echo the account the transport resolved",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]

    def call_tool(
        self, name: str, arguments: dict[str, Any], *, account: str | None = None
    ) -> Any:
        self.seen.append((dict(arguments), account))
        return {"account": account}


class _OldSurface:
    """The shape every other duck-typed surface still has: no `account` parameter at all.
    Passing one blindly would TypeError every call on every surface we did not update."""

    def list_tools(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "name": "ping",
                "description": "answer without knowing who asked",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return {"ok": True}


def _call(surface: Any, name: str, args: dict[str, Any], token: str | None) -> Any:
    app = http_server.build_http_app(
        surface,
        allowed_hosts=["test"],
        allowed_origins=[BASE],
        gate=_gate() if token else None,
    )
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def go() -> str:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE, headers=headers
            ) as http_client:
                async with streamable_http_client(
                    f"{BASE}/mcp", http_client=http_client
                ) as (read, write, _sid):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(name, args)
                        return result.content[0].text  # type: ignore[union-attr]

    return json.loads(anyio.run(go))


# --- the wire itself ---------------------------------------------------------


def test_the_surface_is_told_which_account_called_it() -> None:
    """The whole point. Without this the wallet binding is unreachable code: `account` is
    always None, so `prepare_purchase` can only ever be in mode A and the lookup that
    exists to stop a caller naming somebody else's wallet never runs."""
    surface = _RecordingSurface()

    body = _call(surface, "who_am_i", {}, token=TOKEN)

    assert body["account"] == ACCOUNT
    assert surface.seen[-1][1] == ACCOUNT


def test_an_ungated_surface_is_told_nothing() -> None:
    """None is the honest answer for the public mount, not a defect. It is unauthenticated,
    so it HAS no account — and mode A (the caller supplies the buyer) is correct there,
    because those bytes are for somebody else's wallet to sign."""
    surface = _RecordingSurface()

    body = _call(surface, "who_am_i", {}, token=None)

    assert body["account"] is None
    assert surface.seen[-1][1] is None


def test_the_account_cannot_be_supplied_in_the_arguments() -> None:
    """The arguments are the caller's own word. If naming yourself in them worked, the
    binding would be theatre: anyone could claim any account and be handed the buyer bound
    to it."""
    surface = _RecordingSurface()

    body = _call(surface, "who_am_i", {"account": IMPOSTOR}, token=TOKEN)

    assert body["account"] == ACCOUNT
    args_seen, account_seen = surface.seen[-1]
    assert account_seen == ACCOUNT
    # The claim is passed through untouched and simply never read — the tool decides what
    # its own arguments mean, and no argument named `account` is one of them.
    assert args_seen.get("account") == IMPOSTOR


def test_naming_an_account_in_the_arguments_of_an_UNGATED_call_gives_nothing() -> None:
    """The same claim with no gate in front of it. `None`, not the claim — otherwise the
    public mount would be a free impersonation endpoint for anyone who read the schema."""
    surface = _RecordingSurface()

    assert (
        _call(surface, "who_am_i", {"account": IMPOSTOR}, token=None)["account"] is None
    )


def test_a_surface_that_does_not_take_an_account_is_called_unchanged() -> None:
    """Every other duck-typed surface has the two-argument shape. Passing `account=`
    blindly would TypeError all of them; asking first is why this is a widening, not a
    breaking change."""
    assert _call(_OldSurface(), "ping", {}, token=TOKEN) == {"ok": True}
    assert _call(_OldSurface(), "ping", {}, token=None) == {"ok": True}


def test_the_account_survives_the_shape_production_actually_serves() -> None:
    """The hosted host gates at the MOUNT, not at `/mcp` — that is how a paid surface's
    discovery siblings got covered (#419's R1). So the stamp is written one layer further
    out than the tests above exercise, and has to survive Starlette's routing into the
    sub-app to reach the tool. A wire proven only on the single-surface shape is proven on
    the shape nothing deploys.
    """
    from starlette.testclient import TestClient

    surface = _RecordingSurface()
    app = http_server.build_multi_surface_app(
        [("private", surface)],
        allowed_hosts=["testserver"],
        require_gecko_key=True,
        gated_surfaces=["private"],
        key_gate=_gate(),
    )
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "who_am_i", "arguments": {"account": IMPOSTOR}},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {TOKEN}",
    }

    with TestClient(app) as client:
        init = client.post(
            "/private/mcp",
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
        assert init.status_code == 200
        session = init.headers["mcp-session-id"]
        client.post(
            "/private/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**headers, "mcp-session-id": session},
        )
        client.post(
            "/private/mcp", json=call, headers={**headers, "mcp-session-id": session}
        )

    assert surface.seen, "the gated mount never reached the surface at all"
    assert surface.seen[-1][1] == ACCOUNT


# --- where the account is allowed to come from -------------------------------


def _run_gate(
    scope: dict[str, Any], token: str | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Returns (scopes the inner app saw, the scope object the gate was handed)."""
    seen: list[dict[str, Any]] = []

    async def inner(scope: Any, _receive: Any, _send: Any) -> None:
        seen.append(scope)

    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    app = http_server._GeckoKeyGateASGI(inner, _gate())
    handed = {**scope, "headers": headers}

    async def send(_message: Any) -> None:
        return None

    async def receive() -> Any:
        return {"type": "http.request", "body": b"", "more_body": False}

    anyio.run(lambda: app(handed, receive, send))
    return seen, handed


def test_the_gate_stamps_the_account_it_resolved() -> None:
    seen, _ = _run_gate({"type": "http"}, TOKEN)

    assert seen[0][http_server.ACCOUNT_SCOPE_KEY] == ACCOUNT


def test_a_scope_that_arrives_already_claiming_an_account_is_overwritten() -> None:
    """Defence in depth. Nothing upstream sets this key today, and an HTTP client cannot
    reach the scope at all — but the value is only ever trustworthy because THIS gate
    wrote it, so it is written unconditionally rather than filled in when absent."""
    seen, _ = _run_gate(
        {"type": "http", http_server.ACCOUNT_SCOPE_KEY: IMPOSTOR}, TOKEN
    )

    assert seen[0][http_server.ACCOUNT_SCOPE_KEY] == ACCOUNT


def test_a_refused_caller_never_reaches_the_inner_app_at_all() -> None:
    seen, _ = _run_gate({"type": "http"}, "not-a-real-key")

    assert seen == []


def test_a_refusal_leaves_no_identity_behind_on_the_scope() -> None:
    """`not_enabled` is a REFUSAL that knows exactly who you are — the decision carries the
    account it resolved before rejecting it. Stamping on the way out would make this key
    mean "somebody was identified" instead of "somebody was let in", and those differ for
    every caller we deliberately turned away."""
    gate = KeyGate(
        resolve_account=lambda _token: IMPOSTOR,  # resolves fine…
        allowlist=_OneAccount(),  # …and is refused
    )
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"authorization", b"Bearer anything")],
    }

    async def inner(*_args: Any) -> None:  # pragma: no cover - must never run
        raise AssertionError("a refused caller reached the inner app")

    async def send(_message: Any) -> None:
        return None

    async def receive() -> Any:
        return {"type": "http.request", "body": b"", "more_body": False}

    anyio.run(lambda: http_server._GeckoKeyGateASGI(inner, gate)(scope, receive, send))

    assert http_server.ACCOUNT_SCOPE_KEY not in scope


def test_a_catch_all_kwargs_does_not_count_as_taking_an_account() -> None:
    """A surface that swallows every keyword would be handed the identity and ignore it in
    silence — indistinguishable, from the outside, from threading it correctly. The seam
    has to be an explicit parameter so it is greppable and so a surface that means to
    receive an account says so."""

    class _Swallows:
        def call_tool(
            self, name: str, arguments: dict[str, Any], **_kwargs: Any
        ) -> Any:
            return {}

    class _Declares:
        def call_tool(
            self, name: str, arguments: dict[str, Any], *, account: str | None = None
        ) -> Any:
            return {}

    class _PositionalOnly:
        # Named right, unusable: `account=` cannot fill a positional-only parameter, so
        # counting it would TypeError every call on that surface.
        def call_tool(
            self, name: str, arguments: dict[str, Any], account: str | None = None, /
        ) -> Any:
            return {}

    assert http_server._accepts_account(_Swallows()) is False
    assert http_server._accepts_account(_PositionalOnly()) is False
    assert http_server._accepts_account(_Declares()) is True
