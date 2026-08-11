"""Derive a program's PDAs locally, from the seed recipe, and check them against the chain.

A hosted `POST /pda/derive` is a convenience: you send seed values, it returns an address.
It is also a dependency. When it is unavailable — rate-limited, behind a bot rule, mid-deploy,
or simply answering something you did not expect — an agent that can only ASK is stuck at
step one, before it has built anything.

Gecko does not ask. The seed RECIPE is part of the comprehended surface, so the address is
computed here, offline, in microseconds. This script shows both paths side by side against
the live `let_me_buy` program and then does the only thing that settles it: reads the
derived account back off mainnet and reports whether it exists and who owns it.

**The check that matters is the last one.** A derivation that agrees with a hosted endpoint
proves the two implementations agree; a derivation whose account is owned by the program
proves the derivation is RIGHT. Those are different claims, and only the second one is
worth anything when the endpoint is the thing that is unavailable.

    uv run python examples/derive_locally.py                 # default store: jonasbar
    uv run python examples/derive_locally.py <store_name>

Read-only. No key, no signature, no broadcast, $0. Needs the `solana` extra for `solders`.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

# The `let_me_buy` program. `receipts` is seeded ["receipts", store_name] — one account per
# store — and every instruction in the lifecycle (initialize, add_product, make_purchase,
# mark_as_delivered) reads the SAME account. That shared PDA is the data dependency that
# makes the lifecycle a chain rather than four unrelated calls.
PROGRAM_ID = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
DERIVE_URL = "https://api.orquestra.dev/api/p7o7nf4pucllzadrmiqhf/pda/derive"
MAINNET = "https://api.mainnet-beta.solana.com"
RECEIPTS_SEED = b"receipts"


def _post(url: str, body: dict, timeout: int = 30) -> dict:
    request = urllib.request.Request(
        url,
        json.dumps(body).encode(),
        {"Content-Type": "application/json", "User-Agent": "gecko-surf/derive-locally"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def derive_hosted(store_name: str) -> tuple[str | None, str]:
    """Ask the provider. Returns (address, note) — never raises: an unavailable endpoint is
    an ANSWER here, not a crash, because being able to continue without it is the point."""
    body = {
        "instruction": "initialize",
        "account": "receipts",
        "seeds": {"store_name": store_name},
    }
    try:
        payload = _post(DERIVE_URL, body)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", "replace")
        return None, f"HTTP {exc.code} — {detail}"
    except Exception as exc:  # noqa: BLE001 - any transport failure is the same answer
        return None, f"unavailable — {type(exc).__name__}"
    address = payload.get("address") or payload.get("pda")
    return (address, "ok") if address else (None, f"no address in response: {payload}")


def derive_locally(store_name: str) -> tuple[str, int]:
    """Compute it from the seed recipe. No network, no provider, no permission."""
    from solders.pubkey import Pubkey

    address, bump = Pubkey.find_program_address(
        [RECEIPTS_SEED, store_name.encode()], Pubkey.from_string(PROGRAM_ID)
    )
    return str(address), bump


def account_on_chain(address: str) -> tuple[bool, str | None, int]:
    """Read it back. This is the anchor: existence + owner are facts about the chain, not
    agreement between two implementations of the same guess."""
    payload = _post(
        MAINNET,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [address, {"encoding": "base64"}],
        },
        timeout=45,
    )
    value = payload.get("result", {}).get("value")
    if not value:
        return False, None, 0
    return True, value["owner"], len(value["data"][0])


def main(argv: list[str]) -> int:
    store_name = argv[1] if len(argv) > 1 else "jonasbar"

    print(f"program : {PROGRAM_ID}")
    print(f"store   : {store_name!r}")
    print("recipe  : receipts = PDA([b'receipts', store_name], program)")
    print()

    local, bump = derive_locally(store_name)
    print(f"  local   : {local}  (bump {bump}) — offline, no provider")

    hosted, note = derive_hosted(store_name)
    print(f"  hosted  : {hosted or '—'}  ({note})")

    if hosted and hosted != local:
        print(
            "\n  ⚠️  MISMATCH between local and hosted derivation. Trust neither until the"
        )
        print(
            "      chain settles it; a wrong address that two systems agree on is still wrong."
        )

    exists, owner, size = account_on_chain(local)
    print()
    if exists:
        owned = owner == PROGRAM_ID
        print(f"  ON CHAIN: EXISTS — {size:,} bytes, owner {owner}")
        print(f"            owned by this program: {owned}")
        if owned:
            print(
                "\n  The derivation is CORRECT. Not because a second implementation agreed"
            )
            print(
                "  with it, but because the account it names is real and the program owns it."
            )
    else:
        print("  ON CHAIN: does not exist yet — correct for a store never initialized.")
        print(
            "            The address is still derivable; nothing had to be running to know it."
        )

    print()
    if hosted is None:
        print(
            "  The hosted endpoint did not answer. It did not need to: the seed recipe is"
        )
        print(
            "  part of the comprehended surface, so the address was already known here."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
