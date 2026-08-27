"""Can this wallet buy this product — and if not, what is the shortest honest route?

    uv run python scripts/pay_with_any_token.py --signer <PUBKEY> \
        --store geckocoffee --product Espresso

WHAT THIS ANSWERS. A storefront prices a product in ONE mint under ONE token program.
A buyer holds whatever they hold. Those two facts are read from chain and compared; there
is no guessing anywhere in it, and nothing here signs.

WHY IT IS A REFUSAL AND NOT A ROUTER. Asked "I only have USDG but I want a coffee priced
in USDC", our own router returns `let_me_buy.make_purchase` and pumpfun's `buy` — it finds
the DESTINATION and misses the conversion entirely, because the cards are scored lexically
and there is no cross-program edge to express "convert first". Measured, not assumed. So
this does not pretend to have discovered a route. It states a fact the chain makes certain
— your mint cannot pay this price — and then derives the one venue that changes that.

WHAT IS DERIVED RATHER THAN CHOSEN. The venue is NOT hardcoded. Given (held mint, priced
mint) it queries the AMM by the Whirlpool discriminator plus `token_mint_a`/`token_mint_b`
at offsets `field_offset` computes from the IDL, then RE-DERIVES each candidate's address
from its own (config, mints, tick_spacing). A pool that does not reproduce its own address
is dropped: the memcmp proposes, the seed recipe disposes. That check is what makes a wrong
field offset refute itself instead of returning a plausible wrong pool.

WHAT THE TOKEN PROGRAM COSTS YOU IF YOU GUESS IT. `let_me_buy` PINS classic SPL Token in
its IDL, so a Token-2022 mint has no path through `make_purchase` at all — and the two
programs derive DIFFERENT ATAs for the same owner and mint. Every token program here is
read from its mint's own `owner`, never inferred from a label.
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gecko.idl_layout import field_offset  # noqa: E402
from gecko.pda import b58_encode, derive_pda  # noqa: E402
from gecko.peg_guard import PegVerdict, verdict_from  # noqa: E402
from gecko.provider_config import load_packaged_provider  # noqa: E402
from gecko.providers.catalog_surface import orquestra_seams  # noqa: E402
from gecko.rpc import default_rpc_call  # noqa: E402
from gecko.store_accounts import TOKEN_PROGRAM_ID, derive_ata, resolve_store  # noqa: E402

WHIRLPOOL_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
_SIZES = {"pubkey": 32, "u128": 16, "u64": 8, "i32": 4, "u16": 2, "u8": 1}


def _rpc(url: str, method: str, params: list, *, tries: int = 4) -> dict:
    """Retry: a public endpoint 429s under any real use, and a half-finished read here
    would report a wallet as empty when it is not."""
    for attempt in range(tries):
        try:
            return default_rpc_call(url, method, params)
        except Exception as exc:  # noqa: BLE001
            if attempt == tries - 1:
                raise SystemExit(
                    f"\nSTOP: {method} failed after {tries} attempts against {url}"
                    f"\n  {type(exc).__name__}: {exc}"
                ) from exc
            time.sleep(1.5 * (attempt + 1))
    return {}


def _mint_owner(url: str, mint: str) -> str:
    """The token program IS the mint account's owner. Read; never inferred from a label —
    two mints can wear the same label and derive different ATAs."""
    value = (_rpc(url, "getAccountInfo", [mint, {"encoding": "base64"}]).get("result") or {}).get("value")
    if not value:
        raise SystemExit(f"STOP: mint {mint} does not exist on this node.")
    return value["owner"]


def _holdings(url: str, owner: str) -> dict[str, tuple[int, str]]:
    """Every token this wallet actually holds: mint -> (raw amount, token program).

    Asked of BOTH token programs, because `getTokenAccountsByOwner` filters by one and a
    Token-2022 balance is invisible to a classic-SPL query. That asymmetry is the whole
    reason this script exists.
    """
    out: dict[str, tuple[int, str]] = {}
    for program in (TOKEN_PROGRAM_ID, "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        rows = (
            _rpc(url, "getTokenAccountsByOwner", [owner, {"programId": program}, {"encoding": "jsonParsed"}])
            .get("result", {})
            .get("value", [])
        )
        for row in rows:
            info = (((row.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            amount = int((info.get("tokenAmount") or {}).get("amount") or 0)
            if amount > 0 and info.get("mint"):
                out[info["mint"]] = (amount, program)
    return out


#: Pegana — the peg-risk oracle, addressed BY MINT, the same value domain the store and the
#: pool speak. Public read, no auth; it is a third party, so a failure to reach it must not
#: read as "the peg is fine".
PEGANA = "https://api.pegana.xyz"


def _peg_verdict(_url: str, mint: str) -> PegVerdict:
    """Ask Pegana about one mint. Unreachable is `unknown`, never `ok`."""
    try:
        card = _http_json(f"{PEGANA}/v1/assets/by-mint/{mint}")
    except Exception:  # noqa: BLE001 - a third party being down is not a peg opinion
        return verdict_from(mint, None)
    if not card or not card.get("symbol"):
        return verdict_from(mint, None)
    try:
        state = _http_json(f"{PEGANA}/v1/assets/{card['symbol']}/state")
    except Exception:  # noqa: BLE001
        state = None
    return verdict_from(mint, state, symbol=card.get("symbol"))


def _http_json(url: str) -> dict | None:
    import json as _json
    import urllib.request

    req = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https host
        return _json.loads(resp.read().decode())


def _find_venue(url: str, held: str, needed: str) -> list[dict]:
    """Pools that trade this pair, each PROVEN by re-deriving its own address.

    The memcmp proposes a candidate; the packaged seed recipe disposes. A pool whose
    address does not reproduce from its own (config, mints, tick_spacing) is dropped — so
    a wrong offset refutes itself rather than returning a plausible wrong venue.
    """
    idl_fetch, _build = orquestra_seams()
    idl = idl_fetch(WHIRLPOOL_PROGRAM)
    disc = next(
        (bytes(a.get("discriminator") or []) for a in idl.get("accounts") or [] if a.get("name") == "Whirlpool"),
        b"",
    )
    if not disc:
        raise SystemExit("STOP: the Whirlpool IDL declares no discriminator for its pool account.")
    off = {f: field_offset(idl, "Whirlpool", f)["offset"] for f in
           ("whirlpools_config", "tick_spacing", "token_mint_a", "token_mint_b", "liquidity")}
    _, apis = load_packaged_provider("orquestra")
    recipe = dict(apis["whirlpool"].program.pdas)["whirlpool"]

    found: list[dict] = []
    for mint_a, mint_b, a_to_b in ((held, needed, True), (needed, held, False)):
        rows = _rpc(url, "getProgramAccounts", [WHIRLPOOL_PROGRAM, {
            "encoding": "base64",
            "filters": [
                {"memcmp": {"offset": 0, "bytes": b58_encode(disc)}},
                {"memcmp": {"offset": off["token_mint_a"], "bytes": mint_a}},
                {"memcmp": {"offset": off["token_mint_b"], "bytes": mint_b}},
            ],
        }]).get("result") or []
        for row in rows:
            data = base64.b64decode(row["account"]["data"][0])
            def read(field: str, kind: str = "pubkey"):
                chunk = data[off[field]: off[field] + _SIZES[kind]]
                return b58_encode(chunk) if kind == "pubkey" else int.from_bytes(chunk, "little")
            tick_spacing = read("tick_spacing", "u16")
            config = read("whirlpools_config")
            derived = derive_pda(recipe, {
                "whirlpools_config": config, "token_mint_a": mint_a,
                "token_mint_b": mint_b, "tick_spacing": tick_spacing,
            }).address
            if derived != row["pubkey"]:
                continue  # proposes but does not dispose — not our pool
            found.append({
                "pool": row["pubkey"], "tick_spacing": tick_spacing,
                "liquidity": read("liquidity", "u128"), "a_to_b": a_to_b,
            })
    return sorted(found, key=lambda p: -p["liquidity"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pay_with_any_token",
        description="Can this wallet buy this product, and if not, what is the route? Signs nothing.",
    )
    parser.add_argument("--signer", required=True, help="the buyer's base58 pubkey")
    parser.add_argument("--store", default="geckocoffee")
    parser.add_argument("--product", default="Espresso")
    parser.add_argument("--rpc-url", default="https://api.mainnet-beta.solana.com")
    parser.add_argument("--slippage-bps", type=int, default=100)
    args = parser.parse_args(argv)
    url = args.rpc_url

    store = resolve_store(args.store, rpc_url=url, rpc_call=default_rpc_call).accounts_for(args.product)
    priced_mint = store.product.mint
    priced_program = _mint_owner(url, priced_mint)
    price = store.product.price_raw

    print(f"WANT   {args.product} at {args.store}")
    print(f"  price        {price:,} raw ({store.product.price_ui})")
    print(f"  priced mint  {priced_mint}")
    print(f"  under        {priced_program}")
    # let_me_buy PINS this in its IDL, so it is a structural fact about the program, not a
    # preference: a mint under any other token program cannot be spent here at all.
    print(f"  make_purchase pins {TOKEN_PROGRAM_ID}"
          f"  -> {'match' if priced_program == TOKEN_PROGRAM_ID else 'MISMATCH'}")

    held = _holdings(url, args.signer)
    print(f"\nHAVE   {args.signer}")
    for mint, (amount, program) in sorted(held.items(), key=lambda kv: -kv[1][0]):
        tag = "  <- the priced mint" if mint == priced_mint else ""
        print(f"  {amount:>14,}  {mint}  under {program}{tag}")
    if not held:
        print("  (no token balances)")

    # SELF-PURCHASE, checked BEFORE anything about balances. If the buyer IS the store's
    # authority, ATA(signer, mint) and ATA(authority, mint) are one address: the payment
    # would move value from an account to itself. `check_plan_accounts` refuses this at
    # plan time — but by then a route may already have been quoted and swaps already paid
    # for, which is exactly what happened here: three mainnet swaps funded a wallet that
    # structurally could not make the purchase they were funding.
    buyer_ata = derive_ata(args.signer, priced_mint, token_program=priced_program)
    if buyer_ata == store.token_account:
        print(f"\nNOT PAYABLE — you ARE this store's authority ({store.authority}).")
        print(f"  your account and the store's are the same address: {buyer_ata}")
        print("  The payment would credit the account it debits, so make_purchase refuses")
        print("  it (PlanRefused). No amount of funding changes this — pick a store whose")
        print("  authority is someone else.")
        return 1

    have_raw = held.get(priced_mint, (0, ""))[0]
    if have_raw >= price:
        print(f"\nPAYABLE NOW — you hold {have_raw:,} of the priced mint, price is {price:,}.")
        print(f"  uv run python scripts/prepare_purchase.py --signer {args.signer} \\")
        print(f"    --store {args.store} --product '{args.product}'")
        return 0

    # The refusal, stated as a fact rather than a failure.
    print(f"\nNOT PAYABLE — you hold {have_raw:,} of the priced mint; the price is {price:,}.")
    if priced_program == TOKEN_PROGRAM_ID and any(
        p != TOKEN_PROGRAM_ID for _a, p in held.values()
    ):
        print("  Your balance is under a DIFFERENT token program than this store settles in.")
        print("  make_purchase pins classic SPL Token, so that mint has no path here — the")
        print("  two programs also derive different ATAs for the same owner and mint.")

    candidates = [(m, a, p) for m, (a, p) in held.items() if m != priced_mint and a > 0]
    if not candidates:
        print("\n  Nothing held can reach it. This wallet cannot buy this product.")
        return 1

    print("\nROUTE — derived from the mint pair, not chosen:")
    for held_mint, amount, _program in sorted(candidates, key=lambda c: -c[1]):
        # THE PEG GUARD, checked before the venue is even quoted. A conversion is where a
        # discount is REALISED — if the thing you hold is not holding its peg, the honest
        # answer is "not right now", not a cheaper route. Pegana is keyed by mint, which is
        # the same value domain the store and the pool speak, so no vocabulary is invented.
        peg = _peg_verdict(url, held_mint)
        mark = {"ok": "holding", "refuse": "NOT HOLDING", "unknown": "not tracked"}[peg.outcome]
        print(f"  peg check    {peg.symbol or held_mint[:8]}: {mark} — {peg.reason}")
        if peg.blocks:
            print("\n  STOPPING HERE. Converting now would realise that discount. This is a")
            print("  refusal to act on a price we cannot vouch for, not a routing failure —")
            print("  re-run when Pegana can speak for the peg.")
            return 1
        venues = _find_venue(url, held_mint, priced_mint)
        if not venues:
            print(f"  {held_mint}  no pool trades this pair")
            continue
        best = venues[0]
        need = int(price * (10_000 + args.slippage_bps) / 10_000) + 1
        print(f"  swap {held_mint}")
        print(f"    venue        {best['pool']}  (tick_spacing {best['tick_spacing']}, "
              f"liquidity {best['liquidity']:,})")
        print(f"    proven by    re-deriving its own address from its config + mints + tick_spacing")
        print(f"    direction    {'a-to-b' if best['a_to_b'] else 'b-to-a'}")
        print(f"    spend        ~{need:,} raw to clear {price:,} plus {args.slippage_bps} bps")
        if amount < need:
            print(f"    SHORT        you hold {amount:,}")
            continue
        print("\n  Then, in order:")
        print(f"    uv run python scripts/prepare_whirlpool_swap.py --signer {args.signer} \\")
        print(f"      --direction {'a-to-b' if best['a_to_b'] else 'b-to-a'} "
              f"--amount {need} --keypair <KEY> --send")
        print(f"    uv run python scripts/prepare_purchase.py --signer {args.signer} \\")
        print(f"      --store {args.store} --product '{args.product}'")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
