"""The recorded any-query story: every lane's real code on the recorded edge, $0.

If a lane's contract drifts, this is the test that sees the seam break, which is what
the lane suites, each on its own fakes, could not.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("solders")

from solders.keypair import Keypair  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

from gecko.anyquery.billing import rebate_credit_micro  # noqa: E402
from gecko.anyquery.demo import build_rig, load_fixtures, run_demo  # noqa: E402
from gecko.anyquery.recorded import RECORDED_KEY  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "anyquery"
ACCOUNT = "did:gecko:demo-test"


class Signer:
    def __init__(self, seed: int = 7) -> None:
        self.key = Keypair.from_seed(bytes([seed]) * 32)
        self.asked = 0

    @property
    def pubkey(self) -> str:
        return str(self.key.pubkey())

    def sign_transaction(self, unsigned: bytes, attestation: Any) -> bytes:
        self.asked += 1
        tx = Transaction.from_bytes(unsigned)
        return bytes(
            Transaction.populate(tx.message, [self.key.sign_message(bytes(tx.message))])
        )


SETTLER = Signer(9)


@pytest.fixture(scope="module")
def story(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, Any, Signer]:
    fixtures = load_fixtures(FIXTURES)
    signer = Signer()
    rig = build_rig(
        fixtures,
        payer=signer.pubkey,
        signer_for=lambda _binding: signer,
        settlement_signer=SETTLER,
        folder=tmp_path_factory.mktemp("demo"),
    )
    report = run_demo(rig, ACCOUNT, subjects=fixtures.subjects, query=fixtures.query)
    return rig, report, signer


def test_onboarding_binds_privys_answer_once(story: Any) -> None:
    rig, report, signer = story
    onboard = report.get("onboard")
    assert onboard["address"] == signer.pubkey
    assert onboard["wallet_id"] == "privy-recorded-wallet-1"
    assert len(rig.creator.asked) == 1 and ACCOUNT not in rig.creator.asked[0]


def test_the_plan_comes_from_the_catalog_and_the_quote_prices_every_source(
    story: Any,
) -> None:
    _rig, report, _signer = story
    plan = report.get("plan")
    assert [f["name"] for f in plan["plan"]["fields"]] == ["funding", "hiring", "news"]
    # 3 subjects x (4 + 6 + 4 + 4 + 1) cents = 57 cents, the worst case, + $0.10 fee.
    assert plan["quote"]["total_usdc"] == "0.670000"


def test_each_sol_run_is_two_transactions_the_users_transfer_then_gecko_settles(
    story: Any,
) -> None:
    rig, report, signer = story
    for step in ("run_1", "run_2"):
        answer = report.get(step)
        assert answer["run"]["status"] == "complete"
        assert answer["run"]["payment_status"] == "landed"
        assert answer["payment"]["pay_with"] == "SOL"
    # Transaction 1: the user's wallet, a plain transfer to the settlement wallet.
    assert signer.asked == 2
    assert [p.tx.kind for p in rig.chain.paid] == ["sol", "sol"]
    assert [p.tx.signer for p in rig.chain.paid] == [signer.pubkey] * 2
    # 0.67 and 0.640001 USDC at 150 USDC/SOL plus the 2% margin, rounded up.
    assert [p.tx.lamports for p in rig.chain.paid] == [4_557_824, 4_353_749]
    # Transaction 2: Gecko's settlement wallet swaps and forwards to the treasury,
    # and only it goes through Helius with the relay as rebate address.
    assert SETTLER.asked == 2
    assert [x.tx.signer for x in rig.chain.settled] == [rig.settlement_wallet] * 2
    assert rig.helius.rebate_addresses == [rig.relay, rig.relay]
    assert rig.relay != rig.fee_wallet
    for paid, settled in zip(rig.chain.paid, rig.chain.settled, strict=True):
        assert settled.slot > paid.slot
        assert settled.tx.lamports == paid.tx.lamports - 5_000
    ledger = report.get("payments")["entries"]
    assert sorted(e["status"] for e in ledger if e["kind"] == "settlement") == [
        "landed",
        "landed",
    ]


def test_the_observed_rebate_becomes_the_discount_on_the_second_run(
    story: Any,
) -> None:
    rig, report, _signer = story
    ledger = report.get("payments")["entries"]
    [credit] = [e for e in ledger if e["kind"] == "rebate_credit"]
    # Matched to the first SETTLEMENT's slot, priced at that swap's realised rate.
    first = rig.chain.settled[0]
    assert credit["slot"] == first.slot
    rate = first.usdc_out * 1_000_000_000 // first.tx.lamports
    assert credit["amount_usdc_micro"] == rebate_credit_micro(400_000, 50, rate)
    requote = report.get("requote")["quote"]
    assert requote["discount_usdc"] == credit["amount_usdc"]
    run_2 = report.get("run_2")["run"]["run_id"]
    [discount] = [e for e in ledger if e["kind"] == "discount"]
    assert discount["run_id"] == run_2
    assert discount["amount_usdc_micro"] == credit["amount_usdc_micro"]
    assert report.get("payments")["discount_on_next_run_usdc_micro"] == 0


def test_the_second_run_repairs_a_broken_source_and_run_changes_shows_what_moved(
    story: Any,
) -> None:
    _rig, report, _signer = story
    rows = {(r["subject"], r["field"]): r for r in report.get("run_rows_run_2")["rows"]}
    assert rows[("brex.com", "funding")]["status"] == "repaired"
    changed = {
        (c["subject"], c["field"]): c for c in report.get("run_changes")["changes"]
    }
    assert changed[("stripe.com", "hiring")]["value_after_untrusted"] == "398"
    assert changed[("ramp.com", "news")]["value_after_untrusted"].startswith(
        "Ramp raises"
    )
    assert changed[("brex.com", "funding")]["source_changed"] is True


def test_no_key_reaches_the_report(story: Any) -> None:
    _rig, report, _signer = story
    assert RECORDED_KEY not in json.dumps(report.steps, default=str)


def test_the_script_runs_recorded(capsys: pytest.CaptureFixture[str]) -> None:
    spec = importlib.util.spec_from_file_location(
        "any_query_demo", ROOT / "scripts" / "any_query_demo.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--mode", "recorded"]) == 0
    out = capsys.readouterr().out
    assert "discount applied to run 2: 0.0" in out
    assert RECORDED_KEY not in out
