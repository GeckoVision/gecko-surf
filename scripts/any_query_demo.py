"""Any-query, end to end, $0: onboard, plan, run, pay in SOL, rebate, run again, diff.

    uv run python scripts/any_query_demo.py --mode recorded

Every lane's real code runs (planner, runner, Solana billing, the tools); only the
edge is recorded (gecko/anyquery/recorded.py). Nothing here reaches a network, a model,
a chain, Privy, Helius or MongoDB. The story lives in gecko/anyquery/demo.py; this file
loads the fixtures, supplies the signer and prints.

A SOL payment is TWO transactions (option B, founder ruling 9 Oct): the user's wallet
sends one plain transfer to Gecko's settlement wallet (final when it lands), then the
settlement wallet swaps that SOL to USDC on one Whirlpool and forwards it to the cold
treasury, through Helius with the relay as rebate address. Both are printed.

THE SIGNERS. Privy signs inside its enclave and Gecko never holds the key. The stand-ins
below sign with throwaway keys from fixed seeds, which is why they live here and not in
gecko/ (tests/test_no_key_material_in_gecko.py). They control no funds anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gecko.anyquery.demo import (  # noqa: E402
    DemoReport,
    build_rig,
    load_fixtures,
    run_demo,
)

FIXTURES = ROOT / "tests" / "fixtures" / "anyquery"
ACCOUNT = "did:gecko:demo-user"
_SEED = b"gecko-any-query-recorded-demo-01"  # 32 bytes, public on purpose
_SETTLEMENT_SEED = b"gecko-any-query-settlement-dm-01"  # 32 bytes, public on purpose


class RecordedPrivySigner:
    """Stands in for Privy's ``signTransaction``: signs the exact bytes it is handed."""

    def __init__(self, seed: bytes = _SEED) -> None:
        from solders.keypair import Keypair

        self._key = Keypair.from_seed(seed)
        self.asked = 0

    @property
    def pubkey(self) -> str:
        return str(self._key.pubkey())

    def sign_transaction(self, unsigned_transaction: bytes, attestation: Any) -> bytes:
        from solders.transaction import Transaction

        self.asked += 1
        tx = Transaction.from_bytes(unsigned_transaction)
        signature = self._key.sign_message(bytes(tx.message))
        return bytes(Transaction.populate(tx.message, [signature]))


def _usdc(micro: int | None) -> str:
    return "-" if micro is None else f"{micro / 1_000_000:.6f} USDC"


def _print(
    report: DemoReport, signer: RecordedPrivySigner, settler: RecordedPrivySigner
) -> None:
    onboard = report.get("onboard")
    print("== 1. onboard")
    print(f"   account {onboard['account']}")
    print(
        f"   Privy wallet {onboard['wallet_id']} at {onboard['address']} (bound on create)"
    )

    plan = report.get("plan")
    print("\n== 2. plan the pasted query (model planner on a recorded reply)")
    print(f"   query: {plan['plan']['query']!r}")
    print(f"   subjects: {', '.join(plan['plan']['subjects'])}")
    for fld in plan["plan"]["fields"]:
        sources = " > ".join(
            f"{s['api']}{s['path']} ({s['price_cents']}c)" for s in fld["sources"]
        )
        print(f"   {fld['name']}: {sources}")
    for row in plan["plan"].get("unplanned", []):
        print(f"   unplanned {row['field']}: {row['why']}")
    print(f"   quote: at most {plan['quote']['total_usdc']} USDC")

    for step in ("run_1", "run_2"):
        ran = report.get(step)["run"]
        pay = report.get(step)["payment"] or {}
        print(
            f"\n== {step}: {ran['run_id']} {ran['status']}, payment {ran['payment_status']}"
        )
        print(f"   counts {json.dumps(ran.get('counts', {}))}")
        print(
            f"   charged {pay.get('amount_usdc')} USDC, paid in {pay.get('pay_with')}, "
            f"signature {pay.get('signature')}"
        )
        if pay.get("note"):
            print(f"   {pay['note']}")
        settlements = [
            e
            for e in report.get("payments")["entries"]
            if e["kind"] == "settlement" and e.get("run_id") == ran["run_id"]
        ]
        for settled in settlements:
            print(
                f"   then Gecko settles it: {settled['status']}, "
                f"{settled['amount_usdc']} USDC to the treasury, signature "
                f"{settled['signature']}"
            )
        if step == "run_1":
            after = report.get("payments_after_run_1")
            print(
                f"   rebates credited now: {after['rebates_credited_now']}; discount on "
                f"next run: {_usdc(after['discount_on_next_run_usdc_micro'])}"
            )
            requote = report.get("requote")["quote"]
            print(
                f"\n== requote a week later: {requote['total_usdc']} USDC "
                f"(discount {requote.get('discount_usdc')} USDC)"
            )

    print("\n== run_rows, run 2")
    for row in report.get("run_rows_run_2")["rows"]:
        print(
            f"   {row['subject']:<11} {row['field']:<8} {row['status']:<9} "
            f"{row.get('value_untrusted')!s:<52} {row.get('source', '')}"
        )

    changes = report.get("run_changes")
    print(f"\n== run_changes {changes['since_run']} -> {changes['against_run']}")
    for change in changes["changes"]:
        print(
            f"   {change['subject']:<11} {change['field']:<8} {change['change']:<10} "
            f"{change['value_before_untrusted']!s} -> {change['value_after_untrusted']!s}"
            + ("  (source changed)" if change["source_changed"] else "")
        )

    print("\n== payments ledger (newest first)")
    payments = report.get("payments")
    for entry in payments["entries"]:
        print(
            f"   {entry['kind']:<14} {entry['status']:<8} {entry['amount_usdc']:>8} USDC "
            f"run {entry.get('run_id')}  {entry.get('note', '')}"
        )
    run_2_discount = [
        e
        for e in payments["entries"]
        if e["kind"] == "discount"
        and e.get("run_id") == report.get("run_2")["run"]["run_id"]
    ]
    applied = run_2_discount[0]["amount_usdc"] if run_2_discount else "0"
    print(f"\n== discount applied to run 2: {applied} USDC")

    edge = report.get("edge")
    print("\n== the recorded edge")
    print(f"   Orthogonal calls replayed: {edge['orthogonal_calls']}")
    print(
        f"   Privy creates: {edge['privy_create_calls']}; Privy signatures: "
        f"{signer.asked} by the user's wallet, {settler.asked} by the settlement wallet"
    )
    print(
        f"   Helius rebate-address asked (settlements only): {edge['helius_rebate_address']}"
    )
    print(f"   treasury: {edge['fee_wallet']}")
    print(f"   settlement wallet: {edge['settlement_wallet']}")
    print("   transaction 1 of each run, signed by the user's wallet:")
    for paid in edge["chain_payments"]:
        print(
            f"     {paid['lamports']} lamports to the settlement wallet in slot "
            f"{paid['slot']} (final)"
        )
    print("   transaction 2 of each run, signed by Gecko's settlement wallet:")
    for settled in edge["chain_settlements"]:
        print(
            f"     swapped {settled['lamports_swapped']} lamports for "
            f"{settled['usdc_out_micro']} micro-USDC, "
            f"{settled['usdc_to_treasury_micro']} to the treasury, in slot "
            f"{settled['slot']}"
        )
    print(
        "\n   every source's values stored for this customer (founder ruling, 8 Oct); "
        "storage labels are metadata only."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=["recorded"], default="recorded")
    parser.add_argument(
        "--json", action="store_true", help="print the raw tool answers"
    )
    args = parser.parse_args(argv)

    fixtures = load_fixtures(FIXTURES)
    signer = RecordedPrivySigner()
    settler = RecordedPrivySigner(_SETTLEMENT_SEED)
    with tempfile.TemporaryDirectory() as folder:
        rig = build_rig(
            fixtures,
            payer=signer.pubkey,
            signer_for=lambda _binding: signer,
            settlement_signer=settler,
            folder=Path(folder),
        )
        report = run_demo(
            rig, ACCOUNT, subjects=fixtures.subjects, query=fixtures.query
        )
    if args.json:
        print(json.dumps(report.steps, indent=1, default=str))
    else:
        _print(report, signer, settler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
