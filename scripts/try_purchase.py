"""Buy something, for real, on a fork — and print what actually moved.

Every other scorecard in this repo stops at "it would land". This one lands it: it funds
a key that did not exist a second ago, drives the SAME `prepare_purchase` the public MCP
surface serves, signs the bytes that path returned, sends them, waits for the fork to
confirm, and then reads the ledger back out of the chain. The table below is not a
prediction. It is four balances and one receipt, before and after.

    uv run python -m scripts.try_purchase

Nothing here can touch mainnet. The key is created by `gecko.sandbox.ephemeral_signer`,
which takes a `SurfnetProof` and not a URL, and a proof only exists once an endpoint has
answered `surfnet_getSurfnetInfo` — a method mainnet does not implement. A misconfigured
RPC is refused before any secret exists, not after.

The storefront it buys from is YOURS, so no wallet, store or channel appears in this file.
Point it at your own by writing `~/.gecko/catalog-ci.json` — the same file
`scripts/catalog_ci.py` and `scripts/roundtrip_let_me_buy.py` read:

    {
      "buyer":     "<pubkey that would pay on mainnet — used ONLY for the CU comparison>",
      "authority": "<pubkey that owns the store>",
      "store":     "<store name>",
      "telegram":  "@<channel>"
    }

Every key may be overridden by the matching `GECKO_CI_*` environment variable. It fails
closed: an absent config is a refusal, never a default store.

The fork is started here and torn down on exit. Set `GECKO_FORK_RPC_URL` to reuse one you
are already running (it still has to prove itself before a key is generated).
"""

from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from gecko.netguard import UnsafeUrlError
from gecko.pda_testkit import SurfpoolError, SurfpoolFork, surfpool_status
from gecko.prepare_purchase import prepare_purchase_result
from gecko.sandbox import (
    NotASurfnetError,
    SurfnetProof,
    ephemeral_signer,
    prove_surfnet,
)
from gecko.sandbox.rehearse import Rehearsal, RehearsalError, rehearse_purchase

CONFIG_PATH = Path(
    os.environ.get("GECKO_CI_CONFIG", "~/.gecko/catalog-ci.json")
).expanduser()

DEFAULT_MAINNET_RPC = "https://api.mainnet-beta.solana.com"
DEFAULT_FORK_PORT = 8936


class TryPurchaseError(RuntimeError):
    """The rehearsal could not be attempted — config, or no fork to attempt it on."""


class ConfigError(TryPurchaseError):
    """The local storefront config is missing or incomplete. Never guesses a wallet."""


def load_config() -> dict[str, str]:
    """Read the local storefront identities. Fails closed — never guesses a store."""
    stored: dict[str, str] = {}
    if CONFIG_PATH.exists():
        stored = json.loads(CONFIG_PATH.read_text())
    resolved = {
        key: os.environ.get(f"GECKO_CI_{key.upper()}") or stored.get(key, "")
        for key in ("buyer", "authority", "store")
    }
    missing = sorted(key for key, value in resolved.items() if not value)
    if missing:
        raise ConfigError(
            f"missing {', '.join(missing)} — write {CONFIG_PATH} or set "
            f"{', '.join('GECKO_CI_' + key.upper() for key in missing)}. "
            "See this module's docstring for the shape."
        )
    return resolved


@contextmanager
def fork_endpoint(port: int, mainnet_rpc: str) -> Iterator[str]:
    """A running surfnet's URL — reused if one is configured, started here otherwise."""
    existing = os.environ.get("GECKO_FORK_RPC_URL")
    if existing:
        yield existing
        return
    status = surfpool_status()
    if not status.available:
        raise TryPurchaseError(
            f"{status.detail}. Install surfpool, or point GECKO_FORK_RPC_URL at a fork "
            "you are already running. NOTHING was measured."
        )
    with SurfpoolFork(mainnet_rpc, port=port, ready_timeout=180) as fork:
        yield fork.rpc_url


def mainnet_units(
    config: Mapping[str, str], product: str, table: int
) -> tuple[Any, str]:
    """(compute units on MAINNET, note) for the same purchase — unsigned, read-only.

    This is the comparison that makes "a fork is not a compute oracle" a measurement
    instead of a slogan: same store, same product, same minute, one simulation on each
    side. It uses the config's own mainnet buyer because the ephemeral one holds nothing
    on the real chain — a purchase it cannot pay for reverts, and a reverting simulation's
    compute number is not the same quantity.

    Never signs and never sends: `prepare_purchase` has no path to a key.
    """
    result = prepare_purchase_result(
        {
            "store": config["store"],
            "product": product,
            "buyer": config["buyer"],
            "network": "mainnet",
            "table": table,
        }
    )
    if result.get("refused"):
        return None, f"mainnet refused: [{result.get('code')}] {result.get('reason')}"
    if result.get("error"):
        return None, f"mainnet unreachable: {result['error']}"
    return result.get("units_consumed"), ""


def _short(address: str | None) -> str:
    return "—" if not address else f"{address[:4]}…{address[-4:]}"


def _signed(value: int | None) -> str:
    return "—" if value is None else f"{value:+,}"


def render(
    rehearsal: Rehearsal,
    *,
    proof: SurfnetProof,
    mainnet_cu: int | None,
    mainnet_note: str,
) -> str:
    """The scorecard. Every cell is a number this run read off the chain."""
    price = rehearsal.price_raw
    lines = [
        "",
        f"  REHEARSAL  {rehearsal.store} / {rehearsal.product} — on a local surfpool fork",
        "  " + "─" * 92,
        f"  proved     surfnet_getSurfnetInfo answered at slot {proof.slot:,} "
        f"({proof.rpc_url})",
        f"  buyer      {rehearsal.buyer}",
        "             an ephemeral key, generated only AFTER that proof, never written "
        "anywhere",
        f"  price      {price:,} base units of {_short(rehearsal.mint)}, read from the "
        "store's own account",
        "",
        f"  {'what moved':16} {'account':12} {'before':>14} {'after':>14} "
        f"{'moved':>12} {'expected':>12}  verdict",
        "  " + "─" * 92,
    ]

    def money_row(
        label: str,
        account: str | None,
        before: Any,
        after: Any,
        moved: Any,
        expected: Any,
    ) -> None:
        verdict = "—" if expected is None else ("ok" if moved == expected else "WRONG")
        lines.append(
            f"  {label:16} {_short(account):12} "
            f"{(f'{before:,}' if before is not None else '—'):>14} "
            f"{(f'{after:,}' if after is not None else '—'):>14} "
            f"{_signed(moved):>12} {(_signed(expected) if expected is not None else 'fee only'):>12}"
            f"  {verdict}"
        )

    buyer_token = rehearsal.buyer_token
    store_token = rehearsal.store_token
    buyer_sol = rehearsal.buyer_sol
    money_row(
        "buyer token",
        buyer_token.account if buyer_token else None,
        buyer_token.before if buyer_token else None,
        buyer_token.after if buyer_token else None,
        buyer_token.moved if buyer_token else None,
        -price if rehearsal.landed else None,
    )
    money_row(
        "store token",
        store_token.account if store_token else None,
        store_token.before if store_token else None,
        store_token.after if store_token else None,
        store_token.moved if store_token else None,
        price if rehearsal.landed else None,
    )
    money_row(
        "buyer SOL",
        buyer_sol.address if buyer_sol else None,
        buyer_sol.before if buyer_sol else None,
        buyer_sol.after if buyer_sol else None,
        buyer_sol.moved if buyer_sol else None,
        None,
    )

    receipt = rehearsal.receipt
    lines.append("")
    if receipt is None:
        lines.append("  receipt          the program wrote none for this buyer")
    else:
        lines.append(
            f"  receipt          #{receipt.receipt_id} · {receipt.product_name!r} · "
            f"{receipt.price_raw:,} · table {receipt.table_number} · "
            f"delivered={receipt.delivered}"
        )
        lines.append(
            f"                   store total_purchases "
            f"{receipt.total_purchases_before} -> {receipt.total_purchases_after}"
        )

    lines += [
        "",
        f"  landed           {'YES' if rehearsal.landed else 'no'}"
        + (f"  {rehearsal.signature}" if rehearsal.signature else ""),
        f"  compute          simulated {rehearsal.simulated_units:,} · charged "
        f"{rehearsal.units_consumed:,}"
        if rehearsal.simulated_units and rehearsal.units_consumed
        else "  compute          —",
    ]
    if rehearsal.simulated_units and rehearsal.units_consumed:
        agree = rehearsal.simulated_units == rehearsal.units_consumed
        lines[-1] += "  (agree)" if agree else "  (DISAGREE)"
    if mainnet_cu is not None and rehearsal.units_consumed:
        drift = rehearsal.units_consumed - mainnet_cu
        lines.append(
            f"  fork vs mainnet  the SAME purchase simulates at {mainnet_cu:,} CU on "
            f"mainnet — the fork charged {drift:+,} ({drift / mainnet_cu:+.1%}).\n"
            "                   A fork is not a compute oracle. Do not size a CU budget "
            "from this run."
        )
    elif mainnet_note:
        lines.append(f"  fork vs mainnet  no comparison: {mainnet_note}")
    if rehearsal.fee_lamports is not None:
        lines.append(
            f"  fee              {rehearsal.fee_lamports:,} lamports. The buyer's ATA was "
            "created by a cheatcode,\n                   so the ~2,039,280 lamports of "
            "rent a real first-time buyer pays is ABSENT here."
        )
    if rehearsal.window_blocks is not None:
        lines.append(
            f"  expiry window    {rehearsal.window_blocks} blocks remaining as prepared — "
            "on a fork this is\n                   NOT the window a real buyer races: "
            "block height tracks the fork's own blocks."
        )

    lines.append("")
    if rehearsal.discrepancies:
        for problem in rehearsal.discrepancies:
            lines.append(f"  !! LEDGER        {problem}")
    elif rehearsal.landed:
        lines.append(
            f"  ledger           the buyer fell by exactly {price:,} and the store rose "
            f"by exactly {price:,}."
        )
    for refusal in rehearsal.refusals:
        lines.append(f"  refused [{refusal.step}] {refusal.reason}")
    if not rehearsal.refusals:
        lines.append("  refusals         none")
    for line in rehearsal.reset:
        lines.append(f"  reset            {line}")
    lines.append(
        "\n  Mainnet was never signed for and never sent to. Every write above happened "
        "on the\n  local fork and was dropped again by surfnet_resetAccount."
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--product",
        default=os.environ.get("GECKO_CI_PRODUCT", "Water"),
        help="the product to buy, exactly as the store lists it",
    )
    parser.add_argument("--table", type=int, default=7, help="table number (u8)")
    parser.add_argument("--port", type=int, default=DEFAULT_FORK_PORT)
    parser.add_argument(
        "--mainnet-rpc",
        default=os.environ.get("GECKO_MAINNET_RPC", DEFAULT_MAINNET_RPC),
        help="the chain the fork mirrors, and the one the CU comparison simulates on",
    )
    parser.add_argument(
        "--no-mainnet-compare",
        action="store_true",
        help="skip the read-only mainnet simulation the CU comparison needs",
    )
    args = parser.parse_args(argv)
    config = load_config()

    with fork_endpoint(args.port, args.mainnet_rpc) as rpc_url:
        # THE GATE. No key exists on either side of this call; only its success creates
        # the possibility of one.
        proof = prove_surfnet(rpc_url)
        signer = ephemeral_signer(proof)
        rehearsal = rehearse_purchase(
            proof,
            buyer=signer,
            store=config["store"],
            product=args.product,
            table_number=args.table,
        )
        mainnet_cu, note = (
            (None, "skipped (--no-mainnet-compare)")
            if args.no_mainnet_compare
            else mainnet_units(config, args.product, args.table)
        )
        print(render(rehearsal, proof=proof, mainnet_cu=mainnet_cu, mainnet_note=note))

    if not rehearsal.landed or rehearsal.discrepancies or rehearsal.refusals:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        TryPurchaseError,
        RehearsalError,
        NotASurfnetError,
        SurfpoolError,
        UnsafeUrlError,
    ) as exc:
        raise SystemExit(f"try_purchase refused: {type(exc).__name__}: {exc}") from exc
