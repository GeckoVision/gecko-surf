"""Founder-side: the Friday finalists, one email at a time or all at once.

    uv run --extra events python scripts/finalists.py status student@example.com other@example.com
    uv run --extra events python scripts/finalists.py enable student@example.com   # enable + grant bootcamp
    uv run --extra events python scripts/finalists.py fund-plan student@example.com  # what to send
    uv run --extra events python scripts/finalists.py fund-plan --address <wallet>   # not registered yet

`status` answers, per email: does a Gecko key exist (from `gecko login` or `gecko keys mint`),
is it enabled, does it hold the `bootcamp` grant, which mainnet wallet did they register
for the cohort, and what does that wallet hold on mainnet right now.

THE KEY ITSELF CANNOT BE SHOWN. The registry stores only `sha256(key)`, by design: a
student's key exists in their own keychain and nowhere else. This script can say a key
exists and whether it works; it can never print it. A student who lost theirs runs
`gecko login` again, or you `gecko keys mint` a new one.

`enable` is idempotent: it enables the account's keys and adds the `bootcamp` grant. An
email with no key at all is reported, never minted silently: a minted key prints once and
has to reach the student privately, so that stays an explicit `gecko keys mint`.

`fund-plan` NEVER SIGNS OR SENDS. It reads balances from a public mainnet RPC and prints
the `solana` and `spl-token` commands that top each wallet up to the target, skipping
what is already there. You run them.

MONGODB_URI comes from the keychain through gecko.credentials (slot `MONGODB_URI`), or the
process environment. It is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any

from gecko.credentials import CredentialRef, KeyringBackend

COHORT = "2026-09"
SURFACE = "bootcamp"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TARGET_USDC_RAW = 300_000  # three espressos at 100000 raw
TARGET_SOL_LAMPORTS = (
    9_400_000  # fees, and the first purchase's rent for the merchant's account
)
ATA_RENT_LAMPORTS = (
    2_039_280  # what --fund-recipient spends creating their USDC account
)
RPC = os.environ.get("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")


def _uri() -> str:
    uri = ""
    backend = KeyringBackend()
    if backend.available():
        uri = backend.get(CredentialRef("MONGODB_URI")) or ""
    uri = (uri or os.environ.get("MONGODB_URI") or "").strip()
    if not uri:
        raise SystemExit(
            "MONGODB_URI is in neither the keychain (slot MONGODB_URI) nor the environment."
        )
    return uri


def _db() -> Any:
    from pymongo import MongoClient

    return MongoClient(_uri(), serverSelectionTimeoutMS=8000)["gecko_registry"]


def _rpc(method: str, params: list[Any]) -> Any:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    request = urllib.request.Request(
        RPC, data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=20) as reply:
        return json.loads(reply.read()).get("result")


def balances(address: str) -> tuple[int, int | None]:
    """(lamports, USDC raw or None when the wallet has no USDC account yet)."""
    lamports = int((_rpc("getBalance", [address]) or {}).get("value", 0))
    accounts = (
        _rpc(
            "getTokenAccountsByOwner",
            [address, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}],
        )
        or {}
    ).get("value", [])
    if not accounts:
        return lamports, None
    raw = sum(
        int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        for a in accounts
    )
    return lamports, raw


def account_status(db: Any, email: str) -> dict[str, Any]:
    keys = list(
        db["gecko_keys"].find(
            {"account_id": email},
            {"enabled": 1, "surfaces": 1, "created": 1, "label": 1},
        )
    )
    wallet = db["class_wallets"].find_one(
        {"account": email, "cohort": COHORT}, {"address": 1, "verified_at": 1}
    )
    return {
        "email": email,
        "keys": len(keys),
        "enabled": any(k.get("enabled") for k in keys),
        "bootcamp": any(
            SURFACE in (k.get("surfaces") or []) for k in keys if k.get("enabled")
        ),
        "labels": sorted({str(k.get("label", "")) for k in keys}),
        "wallet": (wallet or {}).get("address"),
    }


def _sol(lamports: int) -> str:
    return f"{lamports / 1e9:.6f}"


def cmd_status(emails: list[str]) -> int:
    db = _db()
    for email in emails:
        s = account_status(db, email)
        key = (
            "no key yet"
            if not s["keys"]
            else (
                f"{s['keys']} key(s), {'enabled' if s['enabled'] else 'DISABLED'}, "
                f"{'bootcamp granted' if s['bootcamp'] else 'NO bootcamp grant'}"
            )
        )
        print(f"{email}\n  gecko:  {key}")
        if not s["wallet"]:
            print(
                "  wallet: not registered yet (they run `mainnet_wallet.py register` with an enabled key)"
            )
            continue
        lamports, usdc = balances(s["wallet"])
        usdc_text = "no USDC account yet" if usdc is None else f"{usdc} raw USDC"
        print(f"  wallet: {s['wallet']}  ->  {_sol(lamports)} SOL, {usdc_text}")
    return 0


def cmd_enable(emails: list[str]) -> int:
    from gecko.keyregistry import MongoKeyRegistry, RegistryAllowlist

    db = _db()
    allow = RegistryAllowlist(MongoKeyRegistry(collection=db["gecko_keys"]))
    for email in emails:
        if not db["gecko_keys"].count_documents({"account_id": email}):
            print(
                f"{email}: NO KEY. They run `uvx --from gecko-surf gecko login --email {email}`,"
                f" or you run `uv run gecko keys mint {email} --surface {SURFACE}` and send it privately."
            )
            continue
        allow.enable(email)
        allow.grant(email, SURFACE)
        s = account_status(db, email)
        print(f"{email}: enabled={s['enabled']} bootcamp={s['bootcamp']}")
    return 0


def plan_for(address: str, label: str) -> list[str]:
    lamports, usdc = balances(address)
    lines = [
        f"# {label}: {address}  now {_sol(lamports)} SOL, "
        f"{'no USDC account' if usdc is None else f'{usdc} raw USDC'}"
    ]
    sol_short = max(0, TARGET_SOL_LAMPORTS - lamports)
    usdc_short = max(0, TARGET_USDC_RAW - (usdc or 0))
    if sol_short:
        lines.append(
            f"solana transfer {address} {_sol(sol_short)} --allow-unfunded-recipient "
            f"--keypair $FUNDER --url mainnet-beta"
        )
    if usdc_short:
        fund = " --fund-recipient" if usdc is None else ""
        lines.append(
            f"spl-token transfer {USDC_MINT} {usdc_short / 1e6:.6f} {address}{fund} "
            f"--owner $FUNDER --fee-payer $FUNDER --url mainnet-beta"
        )
    if not sol_short and not usdc_short:
        lines.append("# already funded: nothing to send")
    return lines


def cmd_fund_plan(emails: list[str], addresses: list[str]) -> int:
    targets: list[tuple[str, str]] = [(a, "address") for a in addresses]
    if emails:
        db = _db()
        for email in emails:
            wallet = account_status(db, email)["wallet"]
            if wallet:
                targets.append((wallet, email))
            else:
                print(
                    f"# {email}: no registered wallet yet; pass --address if they sent you one"
                )
    print(
        "# Run these yourself. FUNDER is the keypair that holds the USDC. Nothing here sends."
    )
    print("FUNDER=<path to your funding keypair>")
    for address, label in targets:
        print("\n".join(plan_for(address, label)))
    print(
        f"# targets: {TARGET_USDC_RAW} raw USDC and {_sol(TARGET_SOL_LAMPORTS)} SOL per wallet;"
        f" a first USDC account also costs you about {_sol(ATA_RENT_LAMPORTS)} SOL of rent"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "enable"):
        p = sub.add_parser(name)
        p.add_argument("emails", nargs="+")
    p = sub.add_parser("fund-plan")
    p.add_argument("emails", nargs="*")
    p.add_argument(
        "--address",
        action="append",
        default=[],
        help="a wallet address to plan for directly",
    )
    args = parser.parse_args(argv)
    if args.command == "status":
        return cmd_status(args.emails)
    if args.command == "enable":
        return cmd_enable(args.emails)
    return cmd_fund_plan(args.emails, args.address)


if __name__ == "__main__":
    sys.exit(main())
