#!/usr/bin/env python3
"""DEMO DAY E2E — an agent safely moves money through ANY API.

  intent  ->  GECKO comprehends the call + injects the key (safe) + reads the TIER
          ->  transfer? route to COMPASS firewall (/v1/verify)
          ->  allow  -> settle on Solana devnet (real tx)
              review -> HELD, nothing signed  (the drain prevented)

One flow: comprehension + key-safety + governance + the Compass compose + real settlement.
Devnet only (valueless test SOL). Simulates by default; pass --broadcast for the real tx.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from gecko.access import ResolvedSession, _InMemorySecret
from gecko.credentials import ChainResolver, CredentialRef, default_resolver
from gecko.ingest import extract_operations
from gecko.policy import AgentPolicy
from gecko.risk import classify_tier

A = "\033[38;2;53;208;138m"
L = "\033[38;2;224;146;90m"
M = "\033[38;2;150;162;153m"
INK = "\033[38;2;233;241;235m"
F = "\033[38;2;112;124;115m"
B = "\033[1m"
R = "\033[0m"

BROADCAST = "--broadcast" in sys.argv
RPC = "https://api.devnet.solana.com"
KEYPAIR = os.path.expanduser("~/.gecko/wallets/gecko-dev.json")
KNOWN = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"  # allowlisted recipient
UNKNOWN = "3xNweLHLqrbx4zo1waDvgWJHgsUpPj8Y8ymTiG3fV9pz"  # not on the allowlist

# The API the developer hands us — a payments API with human-shaped docs. Gecko onboards it.
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Payments API", "version": "1.0.0"},
    "servers": [{"url": "https://api.pay.example.com"}],
    "paths": {
        "/payments": {
            "post": {
                "operationId": "sendPayment",
                "summary": "Send a payment to a recipient wallet",
                "security": [{"apiKey": []}],
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
        }
    },
}
POLICY = AgentPolicy(spend_cap=10.0, recipient_allowlist={KNOWN})
_KEY = default_resolver().resolve(CredentialRef(api="compass"))
_CH = {
    "Authorization": f"Bearer {_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "gecko/0.3.0",
}


def compass(tool, args):
    body = {"toolName": tool, "intent": {"kind": "transfer"}, "arguments": args}
    req = urllib.request.Request(
        "https://compassguard.xyz/v1/verify",
        data=json.dumps(body).encode(),
        headers=_CH,
        method="POST",
    )
    try:
        return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e:
        return {"decision": "error", "reasons": [e.read().decode()[:80]]}


def settle_devnet(recipient, sol=0.01):
    """Real Solana-devnet System transfer. Simulate unless --broadcast."""
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction
    from solders.hash import Hash

    kp = Keypair.from_bytes(bytes(json.load(open(KEYPAIR))))
    ix = transfer(
        TransferParams(
            from_pubkey=kp.pubkey(),
            to_pubkey=Pubkey.from_string(recipient),
            lamports=int(sol * 1e9),
        )
    )
    bh = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(
                RPC,
                data=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getLatestBlockhash",
                        "params": [{"commitment": "finalized"}],
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
        ).read()
    )["result"]["value"]["blockhash"]
    tx = Transaction.new_signed_with_payer(
        [ix], kp.pubkey(), [kp], Hash.from_string(bh)
    )
    method = "sendTransaction" if BROADCAST else "simulateTransaction"
    import base64

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64"}],
    }
    res = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(
                RPC,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
        ).read()
    )
    if BROADCAST:
        sig = res.get("result")
        return (
            f"BROADCAST · https://explorer.solana.com/tx/{sig}?cluster=devnet"
            if sig
            else f"send error: {res.get('error')}"
        )
    err = res.get("result", {}).get("value", {}).get("err")
    return (
        "SIMULATED OK (valid tx — pass --broadcast to settle)"
        if err is None
        else f"sim err: {err}"
    )


def run(title, tool, recipient, amount_usd):
    print(f"\n{F}{'─' * 84}{R}\n{B}{INK}▶ {title}{R}")
    ops = {o.operation_id: o for o in extract_operations(SPEC)}
    op = ops["sendPayment"]
    # 1) Gecko comprehends + injects the key (safe) + reads the tier
    tier = classify_tier(
        method=op.method,
        path=op.path,
        operation_id=op.operation_id,
        request_body=getattr(op, "request_body", None),
        parameters=op.parameters,
    ).tier
    sess = ResolvedSession(
        ref=CredentialRef(api="pay"),
        header_name="x-api-key",
        scheme="raw",
        resolver=ChainResolver([_InMemorySecret(secret="sk-live-PAYMENTS-KEY")]),
    )
    print(
        f"  {A}gecko{R}   comprehended → tool {A}sendPayment{R}; key injected {F}({sess!r}){R}  tier={A if tier == 'transfer' else M}{tier}{R}"
    )
    if tier != "transfer":
        print(f"  {M}not a transfer → straight through, no firewall.{R}")
        return
    # 2) route to Compass
    known = recipient in POLICY.recipient_allowlist
    v = compass(
        tool, {"recipient": recipient, "amountUsd": amount_usd, "recipientKnown": known}
    )
    dec = v.get("decision")
    reasons = ",".join(v.get("reasons", []))
    col = {"allow": A, "review": L, "deny": L}.get(dec, M)
    print(
        f"  {A}compass{R} /v1/verify  recipient={'known' if known else 'UNKNOWN'} ${amount_usd} → {col}{B}{dec}{R} {F}({reasons}){R}"
    )
    # 3) settle or hold
    if dec == "allow":
        out = settle_devnet(recipient)
        print(f"  {A}{B}SIGN → devnet{R}  {A}{out}{R}")
    else:
        print(
            f"  {L}{B}HELD — nothing signed.{R} {M}the agent does not auto-sign a {dec}.{R}"
        )


def main():
    print(
        f"\n{B}{A}GECKO × COMPASS — an agent safely moves money through any API{R}   "
        f"{F}({'BROADCAST' if BROADCAST else 'simulate'} · devnet){R}"
    )
    run("Pay a known vendor, $5 (within policy)", "transfer", KNOWN, 5)
    run("Agent is steered: pay an UNKNOWN wallet, $500", "transfer", UNKNOWN, 500)
    print(
        f"\n{F}{'─' * 84}{R}\n  {M}comprehension + key-safety + governance + the Compass compose + real settlement.{R}\n"
    )


if __name__ == "__main__":
    main()
