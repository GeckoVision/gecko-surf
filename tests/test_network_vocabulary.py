"""The network vocabulary — ONE closed set, TWO provenances, and a gate that trusts one.

This file exists to answer a question empirically rather than by preference: the corpus
already had a network vocabulary (``corpus.NETWORKS``, DERIVED from a prose label by
``corpus.network_category``) and the signing gate needs one that a caller ASSERTS. Two
vocabularies for one concept is how a receipt ends up categorised ``mainnet`` in one
module and ``unknown`` in the other, so the two are exercised HERE in the same run — a
corpus row being written AND a signing verdict being taken — and the invariants that
must survive the unification are asserted rather than described:

* the two consumers share ONE declaration (``gecko.networks``), never two spellings;
* prose derivation stays on the corpus side and is not reachable from the gate;
* the catch-all member approves NOTHING, even against itself;
* rows already written with the corpus's old catch-all spelling (``other``) stay
  readable — a vocabulary change that orphans persisted rows is a failure, not a rename.

Offline (Pattern B): every transaction is assembled locally, every RPC is injected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gecko import corpus as corpus_module
from gecko import networks as networks_module
from gecko.corpus import (
    CorpusError,
    network_category,
    simulated_outcome_from_record,
)
from gecko.landing import assemble_unsigned_tx
from gecko.networks import (
    APPROVABLE_NETWORKS,
    LEGACY_NETWORK_ALIASES,
    NETWORKS,
    UNKNOWN_NETWORK,
    coerce_network,
    network_from_label,
)
from gecko.providers.landing_record import record_landing_outcome
from gecko.simulate import BuiltTx, Receipt, simulate
from gecko.txbind import evaluate_tx, message_binding

PAYER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
FORK_LABEL = "surfpool fork (mainnet-backed — NOT mainnet)"


def _memo(payload: bytes) -> str:
    """A minimal real unsigned transaction, base64 — one memo instruction."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    meta = AccountMeta(Pubkey.from_string(USDC), False, False)
    return assemble_unsigned_tx([Instruction(program, payload, [meta])], PAYER).tx


def _receipt(tx: str, *, network: Any, label: str = FORK_LABEL) -> Receipt:
    """A PASSING receipt that binds ``tx`` — so every refusal below is about the network
    and nothing else."""
    return Receipt(
        status="pass",
        err=None,
        revert_class=None,
        units_consumed=50_000,
        sol_delta=None,
        tokens_received=None,
        logs_tail=(),
        network_label=label,
        message_binding=message_binding(tx),
        binding_strength="structural",
        network=network,
    )


def _sim_rpc(slot: int | None = 250_000_123):
    def call(_url: str, method: str, _params: Any) -> dict[str, Any]:
        if method == "simulateTransaction":
            result: dict[str, Any] = {
                "value": {"err": None, "logs": ["Program success"], "unitsConsumed": 42}
            }
            if slot is not None:
                result["context"] = {"slot": slot}
            return {"result": result}
        if method == "getSlot":
            return {"result": slot}
        return {"result": {"value": None}}

    return call


# --------------------------------------------------------------------------- #
# ONE declaration.
# --------------------------------------------------------------------------- #
def test_the_corpus_and_the_gate_read_one_declaration() -> None:
    """Not "the same members" — the SAME OBJECT. Two frozensets that happen to agree
    today are two vocabularies, and they drift the first time one is edited."""
    assert corpus_module.NETWORKS is networks_module.NETWORKS
    assert APPROVABLE_NETWORKS < NETWORKS
    assert UNKNOWN_NETWORK in NETWORKS
    assert UNKNOWN_NETWORK not in APPROVABLE_NETWORKS


# --------------------------------------------------------------------------- #
# The catch-all approves nothing — the T1 blocking condition, both spellings.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("catch_all", ["unknown", "other"])
def test_the_catch_all_approves_nothing_even_against_itself(catch_all: str) -> None:
    """``expected == actual`` is not agreement when neither side was told anything.

    Both spellings are tested: ``unknown`` is this vocabulary's fail-closed member and
    ``other`` was the corpus's older name for the same bucket, so a rename must not
    quietly move the guard off one of them. ``corpus.network_category`` returns the
    bucket for ANYTHING unrecognised — including ``None`` — so if the bucket were
    approvable, a receipt whose network was never established would approve."""
    tx = _memo(b"buy water")
    verdict = evaluate_tx(
        tx, _receipt(tx, network=catch_all), expected_network=catch_all
    )

    assert verdict.approved is False
    assert "network" in verdict.reason.lower()


def test_a_named_network_matching_itself_approves() -> None:
    """The positive control for the two refusals above: the gate is refusing the
    CATCH-ALL, not everything."""
    tx = _memo(b"buy water")
    verdict = evaluate_tx(tx, _receipt(tx, network="fork"), expected_network="fork")

    assert verdict.approved is True


# --------------------------------------------------------------------------- #
# Prose and structure are independent — the required probe.
# --------------------------------------------------------------------------- #
def test_simulate_keeps_the_prose_label_and_the_structured_network_apart() -> None:
    """REQUIRED: ``network_label='live mainnet'`` + ``network='fork'`` yields ``fork``.

    The label is prose for a human and the field is the gate's input; the field wins and
    the label is carried through untouched. No contradiction check collapses them,
    because a contradiction check makes the prose load-bearing again — and the DEFAULT
    label on every Receipt is literally "simulated (fork/RPC snapshot — not mainnet)",
    which any substring reading of "mainnet" matches."""
    receipt = simulate(
        {},
        rpc_url="https://rpc.example.com",
        rpc_call=_sim_rpc(),
        build_call=lambda _plan: BuiltTx(tx=_memo(b"buy water"), encoding="base64"),
        network_label="live mainnet",
        network="fork",
    )

    assert receipt.network == "fork"
    assert receipt.network_label == "live mainnet"


def test_the_gate_reads_the_field_not_the_label() -> None:
    """A receipt whose prose says mainnet and whose field says fork is a FORK receipt."""
    tx = _memo(b"buy water")
    receipt = _receipt(tx, network="fork", label="simulated against LIVE mainnet")

    assert evaluate_tx(tx, receipt, expected_network="mainnet").approved is False
    assert evaluate_tx(tx, receipt, expected_network="fork").approved is True


def test_the_prose_collapse_is_not_reachable_from_the_signing_module() -> None:
    """Structural, not a promise: ``txbind`` must not mention the prose label or the
    function that collapses it. The whole D2 bug is that a fork label reads as mainnet
    to any substring test, so the collapse belongs to the corpus and to display."""
    source = Path(networks_module.__file__).with_name("txbind.py").read_text()

    assert "network_label" not in source
    assert "network_from_label" not in source
    assert "network_category" not in source


def test_network_from_label_decides_fork_before_mainnet() -> None:
    """The prose collapse itself, kept where it belongs (corpus/display). Specificity
    order, not convenience order: a fork label routinely names mainnet."""
    assert network_from_label(FORK_LABEL) == "fork"
    assert network_from_label("simulated (fork/RPC snapshot — not mainnet)") == "fork"
    assert network_from_label("mainnet") == "mainnet"
    assert network_from_label(None) == UNKNOWN_NETWORK
    assert network_category(FORK_LABEL) == "fork"


def test_a_network_is_asserted_never_guessed_from_a_url() -> None:
    """A fork proxy answers at any hostname, so a URL is evidence of nothing."""
    assert coerce_network("https://api.mainnet-beta.solana.com") == UNKNOWN_NETWORK
    assert coerce_network(None) == UNKNOWN_NETWORK
    assert coerce_network(42) == UNKNOWN_NETWORK
    assert coerce_network("mainnet") == "mainnet"
    # the corpus's older spelling of the same fail-closed bucket
    assert coerce_network("other") == UNKNOWN_NETWORK
    assert LEGACY_NETWORK_ALIASES["other"] == UNKNOWN_NETWORK


# --------------------------------------------------------------------------- #
# BOTH PATHS, ONE RUN: a corpus row is written and a signing verdict is taken.
# --------------------------------------------------------------------------- #
def test_a_corpus_row_and_a_signing_verdict_agree_in_one_run(tmp_path: Path) -> None:
    """The coexistence probe. One simulation, two consumers:

    * the corpus writes a categorical row, deriving its network from the prose label;
    * the signing gate takes a verdict, comparing the ASSERTED structured field.

    They must land on the same member of the same closed set without the gate ever
    touching the prose, and the row must stay categorical (no label, no URL, no slot
    beyond the public integer)."""
    tx = _memo(b"buy water")
    receipt = simulate(
        {},
        rpc_url="https://rpc.example.com",
        rpc_call=_sim_rpc(),
        build_call=lambda _plan: BuiltTx(tx=tx, encoding="base64"),
        network_label=FORK_LABEL,
        network="fork",
    )

    record_landing_outcome(
        receipt,
        program_id="P",
        instruction="buy",
        account_names=["user", "mint"],
        arg_names=["amount"],
        pdas={},
        network_label=FORK_LABEL,
        rpc_url="https://rpc.example.com",
        rpc_call=_sim_rpc(),
        record_to=tmp_path / "corpus.jsonl",
    )
    row = json.loads((tmp_path / "simulated.jsonl").read_text().strip())
    verdict = evaluate_tx(tx, receipt, expected_network="fork")

    assert row["network"] == "fork"
    assert row["network"] in NETWORKS
    assert verdict.approved is True
    assert evaluate_tx(tx, receipt, expected_network="mainnet").approved is False
    # the row is categorical: the prose label never enters it
    assert FORK_LABEL not in json.dumps(row)


def test_the_row_takes_the_structured_network_over_the_prose(tmp_path: Path) -> None:
    """When the Receipt carries an ASSERTED network, that is the fact the corpus
    records — the label is one operator's sentence about it, and an operator who
    mislabels a devnet run must not poison the honesty ledger with ``fork``."""
    tx = _memo(b"buy water")
    record_landing_outcome(
        _receipt(tx, network="devnet", label=FORK_LABEL),
        program_id="P",
        instruction="buy",
        account_names=["user"],
        arg_names=["amount"],
        pdas={},
        network_label=FORK_LABEL,
        rpc_url="https://rpc.example.com",
        rpc_call=_sim_rpc(),
        record_to=tmp_path / "corpus.jsonl",
    )

    row = json.loads((tmp_path / "simulated.jsonl").read_text().strip())
    assert row["network"] == "devnet"


def test_the_row_falls_back_to_the_prose_for_a_receipt_that_asserted_nothing(
    tmp_path: Path,
) -> None:
    """The legacy path, kept alive on purpose: a Receipt that never asserted a network
    (older code, a provider orchestrator that is told nothing) still categorises from
    its label. That is a DERIVED value in a ledger, not a gate input — the corpus may
    read prose; the signing gate may not."""
    tx = _memo(b"buy water")
    record_landing_outcome(
        _receipt(tx, network=UNKNOWN_NETWORK, label=FORK_LABEL),
        program_id="P",
        instruction="buy",
        account_names=["user"],
        arg_names=["amount"],
        pdas={},
        network_label=FORK_LABEL,
        rpc_url="https://rpc.example.com",
        rpc_call=_sim_rpc(),
        record_to=tmp_path / "corpus.jsonl",
    )

    row = json.loads((tmp_path / "simulated.jsonl").read_text().strip())
    assert row["network"] == "fork"


# --------------------------------------------------------------------------- #
# Persisted history survives the rename.
# --------------------------------------------------------------------------- #
def _legacy_row(network: str) -> dict[str, Any]:
    return {
        "ts": 1_754_000_000_000,
        "surface_id": "orquestra:P",
        "program_id": "P",
        "instruction": "buy",
        "recipe_hash": "0" * 64,
        "status": "pass",
        "revert_class": "none",
        "error_code": None,
        "units_consumed": 86_669,
        "slot": 250_000_123,
        "network": network,
        "source": "simulated",
        "tenancy": "local",
    }


def test_a_historical_other_row_stays_readable() -> None:
    """``other`` was the corpus's spelling of the fail-closed bucket before the gate
    needed one. Rows written under it are already on disk; a vocabulary change that
    makes them unreadable is a data loss, not a rename."""
    outcome = simulated_outcome_from_record(_legacy_row("other"))

    assert outcome.network == UNKNOWN_NETWORK
    assert outcome.status == "pass"
    assert outcome.slot == 250_000_123


def test_an_off_vocabulary_row_still_fails_closed() -> None:
    """GUARD on the line above: reading a legacy ALIAS must not have turned into
    coercing anything unrecognised into a member. A hand-edited row still raises."""
    with pytest.raises(CorpusError):
        simulated_outcome_from_record(_legacy_row("mars"))
