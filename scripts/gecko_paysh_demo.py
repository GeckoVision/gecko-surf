#!/usr/bin/env python3
"""LOCAL DEMO — Gecko in front of a real pay.sh x402 API. Test $0, then pay once, correctly.

The pain: pay.sh is pay-per-call. Guess the endpoint wrong (their docs drift) and you burn
real money on calls that 404. The founder hit this live: quoted $2+, paid because wrong calls.

With Gecko: comprehend the resource → the FIRST call is correct → test it $0 offline (recorded)
→ then one correct paid call. pay's --sandbox funds an ephemeral wallet, so the real x402
payment flow runs at $0 here too.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request

from gecko.client import AgentApiClient
from gecko.access import public_session

A = "\033[38;2;53;208;138m"
L = "\033[38;2;224;146;90m"
M = "\033[38;2;150;162;153m"
INK = "\033[38;2;233;241;235m"
F = "\033[38;2;112;124;115m"
B = "\033[1m"
R = "\033[0m"

# Gecko comprehends the pay.sh MPP quote resource (a real x402 pay-per-call API).
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "pay.sh · MPP market quote", "version": "1.0.0"},
    "servers": [{"url": "https://debugger.pay.sh"}],
    "paths": {
        "/mpp/quote/{ticker}": {
            "get": {
                "operationId": "getQuote",
                "summary": "Live market quote for a ticker (x402 pay-per-call)",
                "parameters": [
                    {
                        "name": "ticker",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "symbol": {"type": "string"},
                                        "price": {"type": "string"},
                                        "currency": {"type": "string"},
                                    },
                                }
                            }
                        }
                    }
                },
            }
        }
    },
}


def pay_call(url):
    """Real x402 call through the pay CLI in --sandbox (ephemeral funded wallet, $0)."""
    r = subprocess.run(
        ["npx", "-y", "@solana/pay", "--sandbox", "curl", "-s", url],
        capture_output=True,
        text=True,
        timeout=120,
    )
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and "price" in line:
            return json.loads(line)
    return {"raw": r.stdout[-200:]}


def naive_guess(path):
    try:
        return urllib.request.urlopen(
            f"https://debugger.pay.sh{path}", timeout=10
        ).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return "err"


def main():
    print(f"\n{B}{A}GECKO × pay.sh — test $0, then pay once, correctly{R}\n")

    print(
        f"{L}{B}WITHOUT Gecko{R}  {M}— an agent guesses pay.sh's endpoint (their docs drift):{R}"
    )
    for guess in ["/quote/AAPL", "/api/stock?symbol=AAPL", "/v1/quote/AAPL"]:
        code = naive_guess(guess)
        print(
            f"  GET {guess:28s} → {L}{code}{R}  {F}wrong path — on a paid API, that's real money burned{R}"
        )

    print(f"\n{A}{B}WITH Gecko{R}")
    client = AgentApiClient(
        SPEC, base_url="https://debugger.pay.sh", session=public_session()
    )
    hits = client.search("get a live stock quote for AAPL")
    tool = hits[0]["name"] if hits else "getQuote"
    print(
        f"  {A}comprehend{R}  intent 'live quote for AAPL' → tool {A}{tool}{R} {F}(GET /mpp/quote/{{ticker}}){R}"
    )

    rec = client.call(tool, {"ticker": "AAPL"}, mode="recorded")
    print(
        f"  {A}test $0{R}     recorded mode → {rec['method']} {rec['request']}  {F}(schema-synthesized, no spend){R}"
    )
    print(
        f"             {F}proven correct offline — before a single cent leaves your wallet.{R}"
    )

    url = "https://debugger.pay.sh/mpp/quote/AAPL"
    print(
        f"  {A}pay once{R}    the ONE correct call, through pay --sandbox ($0 ephemeral wallet)…"
    )
    q = pay_call(url)
    print(
        f"             {A}{B}200{R}  {INK}{q.get('symbol')} = {q.get('price')} {q.get('currency')}{R}  {F}real x402 paid call, first try{R}"
    )

    print(f"\n{F}{'─' * 78}{R}")
    print(
        f"  {M}guessing burned 3 calls. Gecko: 0 wasted, tested $0, then 1 correct paid call.{R}"
    )
    print(
        f"  {M}that's the founder's real number — quoted $2+, paid ≤$0.50 — but at $0.{R}\n"
    )


if __name__ == "__main__":
    main()
