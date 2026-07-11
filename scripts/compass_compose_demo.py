#!/usr/bin/env python3
"""Gecko brain -> Compass tx-firewall — the compose prototype.

Gecko comprehends an API and classifies each op's TIER (read / write / transfer). Only
`transfer`-tier calls need the on-chain firewall, so Gecko *routes* them to Compass's
/v1/verify; a read/write flows straight through. Gecko's AgentPolicy.recipient_allowlist
feeds Compass's `recipientKnown`. Compass returns allow / review / deny; the agent honors it.

  Gecko: "this call is a transfer, recipient X, $N"  ->  Compass: "allow / review / deny"

Live against compassguard.xyz using the `compass` key in the OS keychain. $0, read-only
(only /v1/verify — no signing, no on-chain state).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from gecko.credentials import CredentialRef, default_resolver
from gecko.ingest import extract_operations
from gecko.policy import AgentPolicy
from gecko.risk import classify_tier

# ── a tiny wallet API: a transfer + a swap (mutating) and a read + a policy (write) ──
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Wallet API", "version": "1.0.0"},
    "servers": [{"url": "https://api.wallet.example.com"}],
    "paths": {
        "/transfer": {
            "post": {
                "operationId": "transferUsdc",
                "summary": "Transfer USDC to a recipient wallet",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "recipient": {"type": "string"},
                                    "amountUsd": {"type": "number"},
                                },
                                "required": ["recipient", "amountUsd"],
                            }
                        }
                    },
                },
            }
        },
        "/swap": {
            "post": {
                "operationId": "swapTokens",
                "summary": "Swap one token for another",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "inputMint": {"type": "string"},
                                    "outputMint": {"type": "string"},
                                    "amount": {"type": "number"},
                                },
                            }
                        }
                    },
                },
            }
        },
        "/balance": {
            "get": {
                "operationId": "getBalance",
                "summary": "Get a wallet's balance",
                "parameters": [
                    {
                        "name": "wallet",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
            }
        },
        "/policy": {
            "post": {
                "operationId": "createPolicy",
                "summary": "Create a spending policy",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"rules": {"type": "array"}},
                            }
                        }
                    }
                },
            }
        },
    },
}

# ── Gecko's operator policy (this is what feeds Compass's recipientKnown / caps) ──
KNOWN = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"  # an allowlisted recipient
POLICY = AgentPolicy(spend_cap=10.0, recipient_allowlist={KNOWN})

try:
    _KEY = default_resolver().resolve(CredentialRef(api="compass"))
except Exception:  # noqa: BLE001
    raise SystemExit(
        "No Compass key. Store it once:  gecko auth set compass  "
        "(or export COMPASS_HOSTED_API_KEY / mint one at https://compassguard.xyz/signup)."
    )
_H = {
    "Authorization": f"Bearer {_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "gecko/0.3.0",
}


def tier_of(op):
    r = classify_tier(
        method=op.method,
        path=op.path,
        operation_id=op.operation_id,
        request_body=getattr(op, "request_body", None),
        parameters=op.parameters,
    )
    return r.tier


def compass_verify(tool_name, kind, args):
    body = {"toolName": tool_name, "intent": {"kind": kind}, "arguments": args}
    req = urllib.request.Request(
        "https://compassguard.xyz/v1/verify",
        data=json.dumps(body).encode(),
        headers=_H,
        method="POST",
    )
    try:
        return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e:
        return {"decision": f"HTTP{e.code}", "reasons": [e.read().decode()[:80]]}


def guarded(op, tool_name, kind, args):
    """The compose gate. Gecko classifies the tier; only transfers/swaps reach the firewall."""
    tier = tier_of(op)
    if tier != "transfer":
        return tier, "—", f"proceed · {tier}-tier, no tx firewall needed"
    # feed Gecko's allowlist -> Compass's recipientKnown
    if "recipient" in args and "recipientKnown" not in args:
        args = {
            **args,
            "recipientKnown": args["recipient"] in POLICY.recipient_allowlist,
        }
    v = compass_verify(tool_name, kind, args)
    dec = v.get("decision", "?")
    action = {"allow": "SIGN", "review": "HOLD (human approval)", "deny": "REFUSE"}.get(
        dec, "HOLD"
    )
    return tier, dec, f"{action} · {','.join(v.get('reasons', []))}"


def main():
    ops = {o.operation_id: o for o in extract_operations(SPEC)}

    print("\n=== 1) GECKO ROUTES BY TIER — which ops even need the tx firewall? ===")
    for name, op in ops.items():
        t = tier_of(op)
        route = "→ Compass /v1/verify" if t == "transfer" else "→ (straight through)"
        print(f"  {name:14s} {op.method.upper():5s} {op.path:10s}  tier={t:9s} {route}")

    print(
        "\n=== 2) THE FULL VERDICT MATRIX (live Compass, transfers routed by Gecko) ==="
    )
    T = ops["transferUsdc"]
    SW = ops["swapTokens"]
    RD = ops["getBalance"]
    PL = ops["createPolicy"]
    UNK = "3xNweLHLqrbx4zo1waDvgWJHgsUpPj8Y8ymTiG3fV9pz"  # not on the allowlist
    cases = [
        (RD, "getBalance", "read", {"wallet": KNOWN}),
        (PL, "createPolicy", "write", {"rules": []}),
        (T, "transfer", "transfer", {"recipient": KNOWN, "amountUsd": 5}),
        (T, "transfer", "transfer", {"recipient": KNOWN, "amountUsd": 10}),
        (T, "transfer", "transfer", {"recipient": KNOWN, "amountUsd": 11}),
        (T, "transfer", "transfer", {"recipient": KNOWN, "amountUsd": 500}),
        (T, "transfer", "transfer", {"recipient": UNK, "amountUsd": 5}),
        (T, "transfer", "transfer", {"recipient": UNK, "amountUsd": 500}),
        (SW, "swap", "swap", {"recipient": KNOWN, "amountUsd": 5}),
        (
            T,
            "solana_transfer",
            "transfer",
            {"recipient": KNOWN, "amountUsd": 5},
        ),  # unknown tool name
    ]
    print(f"  {'op':13s} {'call':40s} {'gecko tier':10s} {'compass':7s} action")
    print("  " + "─" * 100)
    for op, tool, kind, args in cases:
        tier, dec, action = guarded(op, tool, kind, args)
        label = f"{tool}({', '.join(f'{k}={str(v)[:8]}' for k, v in args.items() if k != 'recipientKnown')})"
        print(f"  {op.operation_id:13s} {label:40s} {tier:10s} {dec:7s} {action}")


if __name__ == "__main__":
    main()
