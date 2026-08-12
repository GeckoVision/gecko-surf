#!/usr/bin/env python3
"""Hand someone a real mainnet transaction, in about eight seconds, for nothing.

    python3 demo/prepare_purchase_live.py

NO INSTALL, NO KEY, NO SPEND. Standard library only — it runs on a stranger's laptop with
whatever Python is already there. It calls the LIVE hosted surface at
https://mcp.geckovision.tech/orquestra/mcp, the same door a chat client uses, and prints
what came back.

WHAT IT IS FOR. A claim about first-call correctness is an argument; an unsigned mainnet
transaction with a compute-unit number on it is an object. This prints the object. The
accounts are derived offline from the store's seed recipe and the SPL associated-token
recipe, the transaction is simulated against real mainnet state, and the bytes handed back
are UNSIGNED — Gecko holds no key and this script cannot acquire one.

WHAT IT COSTS. Nothing. `simulateTransaction` is a read. Nothing here signs and nothing
here broadcasts, on any network.

HONEST LIMITS, printed on every run rather than hidden here: a passing receipt says these
exact bytes are well-formed and would land against the state observed at that slot. It does
NOT say the transaction is the one you meant. It is state-specific and it expires with its
blockhash (about a minute) — re-run it, that is free too.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

HOST = "https://mcp.geckovision.tech"
SURFACE = "orquestra"

#: The published mainnet record this run is compared against — see
#: https://docs.geckovision.tech/mainnet . Every published receipt's prediction matched what
#: the chain charged. 36,508 was the FIRST purchase, before the store had a receipts record;
#: 36,399 is every purchase after it. Both are correct for the state they were measured
#: against, which is the whole lesson.
#:
#: NO COUNT IS STATED HERE OR PRINTED. As of 2026-08-12 the page says ELEVEN and a twelfth
#: has landed but is not published yet, so any number written here would be wrong in one
#: direction or the other until the page catches up. The page carries its own count.
PUBLISHED_CU = {
    36_508: "the first mainnet purchase (2026-08-06)",
    36_399: "every mainnet purchase since",
}

#: A funded mainnet wallet, used only as a default so the demo needs no arguments. Its
#: PUBLIC key. Nothing here can use a secret one.
DEFAULT_BUYER = "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi"

BOLD, DIM, GREEN, YELLOW, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[32m",
    "\033[33m",
    "\033[0m",
)


def _post(payload: dict, session: str | None = None) -> tuple[dict, str | None]:
    request = urllib.request.Request(
        f"{HOST}/{SURFACE}/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "gecko-demo/1 (+https://geckovision.tech)",
            **({"mcp-session-id": session} if session else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        sid = response.headers.get("mcp-session-id") or session
        body = response.read().decode()
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:]), sid
    return (json.loads(body) if body.strip() else {}), sid


def _open_session() -> str | None:
    _, sid = _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "gecko-demo", "version": "1"},
            },
        }
    )
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    return sid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--buyer", default=DEFAULT_BUYER, help="the PUBLIC key that would pay"
    )
    parser.add_argument("--store", default="jonasbar")
    parser.add_argument("--product", default="Water")
    parser.add_argument("--table", type=int, default=0)
    parser.add_argument("--json", action="store_true", help="raw JSON, for piping")
    args = parser.parse_args(argv)

    started = time.monotonic()
    try:
        session = _open_session()
        answer, _ = _post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "prepare_purchase",
                    "arguments": {
                        "store": args.store,
                        "product": args.product,
                        "buyer": args.buyer,
                        "network": "mainnet",
                        "table": args.table,
                    },
                },
            },
            session,
        )
    except urllib.error.URLError as exc:
        print(f"could not reach {HOST} ({exc.reason}). This demo needs the network.")
        return 2
    elapsed = time.monotonic() - started

    blocks = ((answer.get("result") or {}).get("content")) or []
    if not blocks:
        print(json.dumps(answer, indent=2)[:2000])
        return 1
    result = json.loads(blocks[0]["text"])

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    if result.get("refused"):
        # The refusal is not the demo failing — it is the demo working. Print it as
        # prominently as a success, with the reason in full: a tool that answers everything
        # tells you nothing about when to trust it.
        print()
        print(f"{BOLD}REFUSED{RESET}  {YELLOW}{result.get('code')}{RESET}")
        print(f"\n   {result.get('reason')}\n")
        print(
            f"{DIM}No transaction was produced, and there is no field in this answer that"
        )
        print(f"could carry one. A refusal is an answer.{RESET}")
        print()
        return 0

    print()
    print(f"{BOLD}“{args.product} from {args.store}, table {args.table}.”{RESET}")
    print(f"{DIM}   plain words in. Nothing below was typed by hand.{RESET}\n")

    print(
        f"{BOLD}THE ACCOUNTS{RESET}  {DIM}— derived offline, before anything was asked of a chain{RESET}"
    )
    for account in result.get("accounts", []):
        how = account["derivation"].split(" — ")[0]
        flags = "".join(
            ("w" if account["writable"] else "-", "s" if account["signer"] else "-")
        )
        print(
            f"   {account['account']:<26} {account['address']:<45} {DIM}{flags}  {how}{RESET}"
        )

    receipt_cu = result.get("units_consumed")
    print(
        f"\n{BOLD}THE RECEIPT{RESET}  {DIM}— simulated against real mainnet state, read-only{RESET}"
    )
    print(f"   status          {GREEN}{result.get('status')}{RESET}")
    print(f"   compute units   {BOLD}{receipt_cu:,}{RESET}")
    if receipt_cu in PUBLISHED_CU:
        print(
            f"   {GREEN}↳ matches the published mainnet record: {PUBLISHED_CU[receipt_cu]}{RESET}"
        )
        # NO COUNT HERE, deliberately. A number printed next to a URL goes stale the moment
        # a transaction lands and the page has not been updated yet — which is exactly the
        # state this line was written in. Let the page carry its own count.
        print(
            f"   {DIM}  every published receipt matched what the chain charged:{RESET}"
        )
        print(f"   {DIM}  https://docs.geckovision.tech/mainnet{RESET}")
    print(
        f"   binding         {result.get('binding', '')[:32]}… ({result.get('binding_strength')})"
    )
    print(f"   network         {result.get('network_label')}")

    transaction = result.get("transaction") or {}
    expires = result.get("expires") or {}
    print(f"\n{BOLD}THE TRANSACTION{RESET}")
    print(f"   signed          {YELLOW}{transaction.get('signed')}{RESET}")
    print(f"   who signs       {transaction.get('who_signs')}")
    print(
        f"   blockhash       {expires.get('blockhash')}  {DIM}(valid to block {expires.get('last_valid_block_height')}){RESET}"
    )
    print(f"   bytes           {(transaction.get('unsigned_transaction') or '')[:56]}…")

    print(
        f"\n{DIM}{elapsed:.1f}s · $0 · nothing was signed, nothing was broadcast, no key exists here.{RESET}"
    )
    print(
        f"{DIM}A passing receipt says these bytes are well-formed against the state at that"
    )
    print(
        f"slot. It does not say this is the purchase you meant. Read the accounts.{RESET}"
    )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
