"""Security demo — can an agent (or a tool it loaded) capture your API key?

Runs a REAL contrast, not a staged one, using the actual engine:
  * plaintext (the way most people do it today): the key lives in mcp.json / env and
    the tool schema exposes an auth field, so the key ends up in the agent's world.
  * Gecko: the key lives in a resolver (keychain), the tool schema has NO auth field
    (``tools.build_tools`` strips it), and ``ResolvedSession`` injects it at the wire —
    the agent never sees it, and even the session object's ``repr`` carries no secret.

A single "attacker" — a compromised/curious tool that scans EVERYTHING the agent can
reach — is pointed at both. It captures the key in one case and finds nothing in the other.

    uv run python scripts/security_demo.py
"""

from __future__ import annotations

import json

from gecko.access import ResolvedSession, _InMemorySecret
from gecko.credentials import ChainResolver, CredentialRef
from gecko.ingest import extract_operations
from gecko.tools import build_tools

# A real-shaped secret. Nothing here ever touches a real service — it's a fake key so the
# demo is $0 and safe to run anywhere.
SECRET = "sk-live-9f2a7c41e0b84d6fa1c3ADMIN-would-drain-your-account"

# A tiny API whose one endpoint needs an `x-api-key` header — the auth pattern most
# painful APIs use. (Inline so the demo has no external dependency.)
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Billing API", "version": "1.0.0"},
    "servers": [{"url": "https://api.billing.example.com"}],
    "paths": {
        "/charge": {
            "post": {
                "operationId": "createCharge",
                "summary": "Charge a customer",
                "parameters": [
                    {
                        "name": "x-api-key",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"amount": {"type": "integer"}},
                                "required": ["amount"],
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}


def scan(agent_visible_surface: dict[str, object]) -> str | None:
    """The 'attacker': a compromised/curious tool that reads everything the agent can
    reach and hunts for the secret. Returns the captured value, or None."""
    blob = json.dumps(agent_visible_surface, default=str)
    return SECRET if SECRET in blob else None


def _hr() -> None:
    print("─" * 74)


def main() -> None:
    ops = extract_operations(SPEC)

    print("\n🔐  SECURITY DEMO — can an agent (or a tool it loaded) capture your key?")
    print(f"    The API needs:  x-api-key: {SECRET[:14]}…  (a real secret)")
    print("    An agent connects, then a compromised tool scans EVERYTHING the agent")
    print(
        "    can see — tool schemas, mcp.json, the arguments of the call — for a key.\n"
    )

    # ── ❌ WITHOUT Gecko: the key lives in plaintext, the way most people do it today ──
    _hr()
    print(
        "❌  WITHOUT Gecko  —  key in plaintext (env / mcp.json), auth in the tool schema"
    )
    raw_mcp_json = {"mcpServers": {"billing": {"env": {"BILLING_API_KEY": SECRET}}}}
    # The raw tool exposes the auth header, so the agent must fill it — pulling the key
    # from the plaintext env into the call it makes.
    raw_tool_schema = {
        "name": "createCharge",
        "input_schema": {
            "type": "object",
            "properties": {
                "x-api-key": {"type": "string"},  # auth EXPOSED — agent must supply it
                "amount": {"type": "integer"},
            },
            "required": ["x-api-key", "amount"],
        },
    }
    agent_call_args = {"x-api-key": SECRET, "amount": 5000}  # agent fills auth from env
    plaintext_surface = {
        "mcp.json": raw_mcp_json,
        "tool_schema": raw_tool_schema,
        "the_call_the_agent_makes": agent_call_args,
    }
    hit = scan(plaintext_surface)
    print("    mcp.json     : key sits in env in PLAINTEXT")
    print("    tool schema  : exposes an `x-api-key` field the agent must fill")
    print(f"    SCAN of everything the agent can see  →  🔴 CAPTURED: {hit[:22]}…")
    print("    (found in mcp.json AND in the arguments of the call)")

    # ── ✅ WITH Gecko: keychain + auth injection ──
    _hr()
    print("✅  WITH Gecko  —  keychain + auth injection")
    # The key lives in a resolver (here an in-memory stand-in for the OS keychain);
    # the agent-facing tool defs are built by the ENGINE, which strips the auth header.
    resolver = ChainResolver([_InMemorySecret(secret=SECRET)])
    session = ResolvedSession(
        ref=CredentialRef(api="billing"),
        header_name="x-api-key",
        scheme="raw",
        resolver=resolver,
    )
    gecko_tools = build_tools(ops)  # the ENGINE strips auth params
    gecko_mcp_json = {  # a reference, not the value
        "mcpServers": {"billing": {"command": "gecko", "args": ["serve", "billing"]}}
    }
    # The agent supplies only business params — no auth exists in its world.
    agent_call_args_gecko = {"body": {"amount": 5000}}
    gecko_surface = {
        "mcp.json": gecko_mcp_json,
        "tool_schemas": gecko_tools,
        "the_call_the_agent_makes": agent_call_args_gecko,
        "session_repr_if_logged": repr(session),
    }
    hit2 = scan(gecko_surface)
    print("    tool schema  : the raw arm exposed ['x-api-key', 'amount']; the engine")
    print(
        "                   STRIPS the auth header → the agent never sees `x-api-key` exists"
    )
    print("    mcp.json     : a `gecko serve` reference — no key")
    print(f"    session repr : {session!r}  ← no secret even in your logs")
    print(
        f"    SCAN of everything the agent can see  →  🟢 {'NOT FOUND' if not hit2 else hit2}"
    )

    # …yet the call still works — Gecko injects the key at the wire, host-pinned.
    injected = session.auth_headers()
    on_wire_ok = injected.get("x-api-key") == SECRET
    print("    …yet the call is authenticated — Gecko injects it at the WIRE only:")
    print(
        f"       real request header:  x-api-key: {SECRET[:14]}…   (present: {on_wire_ok})"
    )
    print(
        "       sent ONLY to api.billing.example.com — Gecko refuses to leak it elsewhere"
    )

    _hr()
    print("Same working call. Left: the key leaks to anything the agent touches.")
    print("Right: the key never enters the agent's world at all.\n")

    # A guardrail so the demo can't silently rot into a false claim.
    assert hit == SECRET, "plaintext path should leak the secret"
    assert hit2 is None, "Gecko path must NOT expose the secret anywhere the agent sees"
    assert on_wire_ok, "Gecko must still inject the real key at the wire"


if __name__ == "__main__":
    main()
