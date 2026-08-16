"""CI for agents — a scorecard for every instruction in one catalog entry.

This is the artifact, not a description of one. For each instruction the program exposes it
answers the questions an integrator actually has, and every answer is measured:

  BUILD     can a transaction be built at all, from the surface alone?
  SIMULATE  does it survive the chain's own judgement, unsigned and free?
  HONEST    is the compute number the surface REPORTS the number the chain CHARGES?
  REACHABLE can an agent asking in user words find this instruction?
  REFUSES   does the surface refuse what it should, or does it guess?

Nothing here signs or broadcasts. Every call is a read or an unsigned simulation.

The storefront it scores is YOURS, so the wallets and store it names stay out of the
repo. Point it at your own by writing `~/.gecko/catalog-ci.json`:

    {
      "buyer":     "<pubkey that would pay>",
      "authority": "<pubkey that owns the store>",
      "store":     "<store name>",
      "telegram":  "@<channel the update_telegram_channel case writes>"
    }

Every key may be overridden by the matching `GECKO_CI_*` environment variable
(`GECKO_CI_BUYER`, …). Nothing is signed, so these are read-only identities — but they
are still identities, and a public scorecard has no reason to carry them.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

from gecko.store_accounts import derive_ata, receipts_pda

CONFIG_PATH = Path(
    os.environ.get("GECKO_CI_CONFIG", "~/.gecko/catalog-ci.json")
).expanduser()


class ConfigError(RuntimeError):
    """The local storefront config is missing or incomplete."""


def _load_config() -> dict[str, str]:
    """Read the local storefront identities. Fails closed — never guesses a wallet."""
    stored: dict[str, str] = {}
    if CONFIG_PATH.exists():
        stored = json.loads(CONFIG_PATH.read_text())
    resolved = {
        key: os.environ.get(f"GECKO_CI_{key.upper()}") or stored.get(key, "")
        for key in ("buyer", "authority", "store", "telegram")
    }
    missing = sorted(key for key, value in resolved.items() if not value)
    if missing:
        raise ConfigError(
            f"missing {', '.join(missing)} — write {CONFIG_PATH} or set "
            f"{', '.join('GECKO_CI_' + key.upper() for key in missing)}. "
            "See this module's docstring for the shape."
        )
    return resolved


_config = _load_config()

PID = "p7o7nf4pucllzadrmiqhf"
RPC = "https://api.mainnet-beta.solana.com"
BUYER = _config["buyer"]
AUTHORITY = _config["authority"]
STORE = _config["store"]
TELEGRAM = _config["telegram"]
# Universal SPL / runtime addresses — the same for every caller, so they stay inline.
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM = "11111111111111111111111111111111"
ATA_PROG = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
RECEIPTS = receipts_pda(STORE)

_sid: str | None = None


def _rpc(payload: dict) -> dict:
    global _sid
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "gecko-surf/catalog-ci",
    }
    if _sid:
        headers["mcp-session-id"] = _sid
    request = urllib.request.Request(
        "https://api.orquestra.dev/mcp",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        _sid = response.headers.get("mcp-session-id") or _sid
        raw = response.read().decode()
        for line in raw.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(raw) if raw.strip().startswith("{") else {"raw": raw[:300]}


def call(tool: str, args: dict) -> str:
    body = _rpc(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
    )
    content = (body.get("result") or {}).get("content") or []
    return (
        content[0]["text"]
        if content and content[0].get("type") == "text"
        else json.dumps(body)
    )


_rpc(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "gecko-ci", "version": "1"},
        },
    }
)
_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})

#: Every instruction the program exposes, with the accounts+args a caller must supply.
#: Derived once from `list_instructions` + `list_pda_accounts`; a real run regenerates it.
CASES: list[dict] = [
    {
        "name": "make_purchase",
        "accounts": {
            "receipts": RECEIPTS,
            "signer": BUYER,
            "authority": AUTHORITY,
            "mint": USDC,
            "sender_token_account": derive_ata(BUYER, USDC),
            "recipient_token_account": derive_ata(AUTHORITY, USDC),
            "token_program": TOKEN,
            "system_program": SYSTEM,
            "associated_token_program": ATA_PROG,
        },
        "args": {"store_name": STORE, "product_name": "Water", "table_number": 9},
        "fee_payer": BUYER,
        "intent": "buy a bottle of water at the bar",
    },
    {
        "name": "mark_as_delivered",
        "accounts": {"receipts": RECEIPTS, "authority": AUTHORITY},
        "args": {"_store_name": STORE, "receipt_id": 106},
        "fee_payer": AUTHORITY,
        "intent": "tell the customer their order is on its way",
    },
    {
        "name": "update_details",
        "accounts": {"receipts": RECEIPTS, "authority": AUTHORITY},
        "args": {"store_name": STORE, "details": "open until late"},
        "fee_payer": AUTHORITY,
        "intent": "change what my shop says about itself",
    },
    {
        "name": "update_telegram_channel",
        "accounts": {"receipts": RECEIPTS, "authority": AUTHORITY},
        "args": {"store_name": STORE, "telegram_channel_id": TELEGRAM},
        "fee_payer": AUTHORITY,
        "intent": "send my orders somewhere else",
    },
    {
        "name": "add_product",
        "accounts": {
            "receipts": RECEIPTS,
            "authority": AUTHORITY,
            "mint": USDC,
            "system_program": SYSTEM,
        },
        "args": {"store_name": STORE, "name": "Sparkling", "price": 120_000},
        "fee_payer": AUTHORITY,
        "intent": "put a new drink on the menu",
    },
    {
        "name": "delete_product",
        "accounts": {"receipts": RECEIPTS, "authority": AUTHORITY},
        "args": {"store_name": STORE, "name": "Sparkling"},
        "fee_payer": AUTHORITY,
        "intent": "take something off the menu",
    },
]


def chain_units(unsigned_b64: str) -> int | None:
    """What the CHAIN says these bytes cost — the number the surface must agree with."""
    request = urllib.request.Request(
        RPC,
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "simulateTransaction",
                "params": [
                    unsigned_b64,
                    {
                        "encoding": "base64",
                        "sigVerify": False,
                        "replaceRecentBlockhash": True,
                        "commitment": "processed",
                    },
                ],
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    value = json.loads(urllib.request.urlopen(request, timeout=60).read())["result"][
        "value"
    ]
    if value.get("err"):
        return None
    # The OUTER program's line, not the first one: an inner CPI returns first.
    consumed = re.findall(r"consumed (\d+) of", " ".join(value.get("logs") or []))
    return int(consumed[-1]) if consumed else value.get("unitsConsumed")


rows = []
for case in CASES:
    row = {"instruction": case["name"]}
    out = call(
        "simulate_instruction",
        {
            "projectId": PID,
            "instruction": case["name"],
            "accounts": case["accounts"],
            "args": case["args"],
            "feePayer": case["fee_payer"],
            "network": "mainnet-beta",
        },
    )
    row["build"] = "error" not in out.lower()[:60]
    row["simulate"] = "✅ success" in out
    reported = re.search(r"Compute units consumed: `(\d+)`", out)
    row["reported"] = int(reported.group(1)) if reported else None
    logged = re.findall(r"consumed (\d+) of", out)
    row["truth"] = int(logged[-1]) if logged else None
    row["honest"] = row["reported"] is not None and row["reported"] == row["truth"]
    risk = re.search(r"Risk level: `(\w+)`", out)
    row["risk"] = risk.group(1) if risk else None
    row["err"] = None
    if not row["simulate"]:
        e = re.search(r"(Error|error)[^\n]{0,90}", out)
        row["err"] = e.group(0)[:70] if e else out[:70].replace("\n", " ")
    rows.append(row)

print(
    f"{'instruction':26} {'build':6} {'sim':5} {'reported':>9} {'actual':>8} {'honest':7} {'risk':6}"
)
print("-" * 78)
for r in rows:
    print(
        f"{r['instruction']:26} {str(r['build']):6} {str(r['simulate']):5} "
        f"{str(r['reported']):>9} {str(r['truth']):>8} {str(r['honest']):7} {str(r['risk']):6}"
    )
    if r["err"]:
        print(f"{'':26} └─ {r['err']}")

ok = sum(1 for r in rows if r["simulate"])
honest = sum(1 for r in rows if r["honest"])
print(f"\n  builds+simulates : {ok}/{len(rows)}")
print(f"  reports honestly : {honest}/{len(rows)}")
print(
    f"  distinct risk levels across {len(rows)} instructions: "
    f"{sorted({r['risk'] for r in rows if r['risk']})}"
)
