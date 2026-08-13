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
import re
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
        tx,
        _receipt(tx, network=catch_all),
        require="structural",
        expected_network=catch_all,
    )

    assert verdict.approved is False
    assert "network" in verdict.reason.lower()


def test_a_named_network_matching_itself_approves() -> None:
    """The positive control for the two refusals above: the gate is refusing the
    CATCH-ALL, not everything."""
    tx = _memo(b"buy water")
    verdict = evaluate_tx(
        tx, _receipt(tx, network="fork"), require="structural", expected_network="fork"
    )

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

    assert (
        evaluate_tx(
            tx, receipt, require="structural", expected_network="mainnet"
        ).approved
        is False
    )
    assert (
        evaluate_tx(tx, receipt, require="structural", expected_network="fork").approved
        is True
    )


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
    verdict = evaluate_tx(tx, receipt, require="structural", expected_network="fork")

    assert row["network"] == "fork"
    assert row["network"] in NETWORKS
    assert verdict.approved is True
    assert (
        evaluate_tx(
            tx, receipt, require="structural", expected_network="mainnet"
        ).approved
        is False
    )
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


# --------------------------------------------------------------------------- #
# NO FABRICATED NETWORK ASSERTION.
#
# The vocabulary above is only worth what the CALL SITES do with it. A gate that
# compares two closed-set members faithfully is still a fail-open if some module
# upstream invents one. So: a call site may assert a network only if it was TOLD one —
# an explicit operator flag, or an argument from its own caller. Everything else passes
# ``unknown``, which approves nothing.
#
# These two are SOURCE GREPS. A grep is defeated by any string construction
# (``"main" + "net"``, a lookup in a dict, an f-string), so it is a LINT against
# accidental reintroduction and NOT a guarantee. It is here because the thing it
# catches — someone typing the convenient literal while wiring a new provider — is
# exactly how this class of bug arrives, and a cheap tripwire on the common case beats
# no tripwire at all.
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(networks_module.__file__).resolve().parents[1]

#: Every script that reaches the simulate → gate seam and therefore needs an operator to
#: name the network. Named explicitly rather than globbed: a new script must be added
#: here deliberately, and a renamed one must fail this test rather than silently drop out.
_NETWORK_ASSERTING_SCRIPTS = (
    "prepare_purchase.py",
    "sign_and_send.py",
    "compose_e2e.py",
)


def _network_call_sites() -> list[Path]:
    """The modules that hand a network to ``simulate``/``evaluate_tx`` without being an
    operator themselves — the orchestrators — plus the scripts that ARE the operator."""
    sites = [_REPO_ROOT / "gecko" / "landing.py"]
    sites += sorted((_REPO_ROOT / "gecko" / "providers").glob("*.py"))
    sites += [_REPO_ROOT / "scripts" / name for name in _NETWORK_ASSERTING_SCRIPTS]
    return sites


def test_no_call_site_asserts_a_network_it_was_not_told() -> None:
    """An approvable literal bound to a ``network``-shaped keyword is a FABRICATED
    assertion: the module states as fact something nobody told it.

    The pattern deliberately does not match ``network_label="live mainnet..."`` — prose
    naming a network is fine and is exactly what the whole D2 split exists to tolerate.
    What is forbidden is the structured field, the gate's only input, being filled in
    from a literal that no operator supplied."""
    fabricated = re.compile(r"\bnetwork\s*=\s*[\"'](mainnet|devnet|testnet|fork)[\"']")

    offenders = [
        f"{path.relative_to(_REPO_ROOT)}:{number}: {line.strip()}"
        for path in _network_call_sites()
        if path.exists()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if fabricated.search(line)
    ]

    assert not offenders, (
        "a call site asserts a network it was not told:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("script", _NETWORK_ASSERTING_SCRIPTS)
def test_every_signing_script_makes_the_operator_name_the_network(script: str) -> None:
    """``--network`` is REQUIRED with NO default on every script that reaches the gate.

    A default here is the whole D2 fail-open wearing a CLI flag: the permissive one
    approves a network nobody chose, and even the fail-closed one lets a run that stated
    nothing look like a run that was checked. The operator says, or the script does not
    start."""
    source = (_REPO_ROOT / "scripts" / script).read_text()

    declaration = re.search(
        r"add_argument\(\s*[\"']--network[\"'](.*?)\n\s*\)", source, re.S
    )
    assert declaration, f"{script} does not declare a --network flag"
    body = declaration.group(1)
    assert "required=True" in body, f"{script}'s --network is not required"
    assert "default=" not in body, f"{script}'s --network carries a default"


# --------------------------------------------------------------------------- #
# The script path, exercised. ``mypy gecko`` does not reach ``scripts/``, so nothing
# else in this repo would notice that a required keyword arrived at ``simulate`` and
# ``compose_e2e`` still called it the old way — it would TypeError on the live path,
# in front of a founder, holding a real RPC URL.
# --------------------------------------------------------------------------- #
def _b58(raw: bytes) -> str:
    from gecko.txbind import _B58

    number = int.from_bytes(raw, "big")
    digits = ""
    while number:
        number, rem = divmod(number, 58)
        digits = _B58[rem] + digits
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + digits


def _compose(monkeypatch: pytest.MonkeyPatch, *, tx_b64: str) -> dict[str, Any]:
    """Wire ``scripts/compose_e2e`` to injected fakes and report what it did.

    Every seam is replaced by module attribute, which the script resolves at call time:
    the builder, the blockhash read, the RPC transport, and ``simulate`` itself (wrapped,
    not replaced, so the REAL engine runs and the recorded ``network`` is the one the
    script actually asserted)."""
    import scripts.compose_e2e as compose

    seen: dict[str, Any] = {}
    real_simulate = compose.simulate

    def recording_simulate(plan: Any, **kwargs: Any) -> Receipt:
        seen["network_arg"] = kwargs.get("network")
        receipt = real_simulate(plan, **kwargs)
        seen["receipt"] = receipt
        return receipt

    monkeypatch.setattr(
        compose,
        "build_instruction",
        lambda *_a, **_k: {
            "serializedTransaction": _b58(__import__("base64").b64decode(tx_b64)),
            "encoding": "base58",
            "simulationError": None,
        },
    )
    monkeypatch.setattr(
        compose, "latest_blockhash", lambda *_a, **_k: ("So11111111111111", 1_000)
    )
    # The script resolves ``--store`` from the chain now, so the injected transport has to
    # answer for that account too. Without it the run would reach a real node — which is
    # precisely what an injected seam exists to make impossible.
    from test_store_accounts import node_with

    store_node = _sim_rpc()
    jonasbar = node_with("jonasbar")

    def rpc(url: str, method: str, params: Any) -> dict[str, Any]:
        if method == "getAccountInfo":
            return jonasbar(url, method, params)
        return store_node(url, method, params)

    monkeypatch.setattr(compose, "_rpc", rpc)
    monkeypatch.setattr(compose, "simulate", recording_simulate)
    return seen


def test_the_script_records_the_network_it_was_told_never_the_one_the_url_claims(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mainnet-SHAPED URL in front of a fork transport. The operator said ``fork``, so
    the receipt says ``fork`` — the hostname is evidence of nothing, and a fork proxy
    answers at any of them.

    The refusal on the last line is the point: these exact bytes, this exact binding, a
    passing simulation — and no approval for a mainnet signature."""
    import scripts.compose_e2e as compose

    tx = _memo(b"buy water")
    seen = _compose(monkeypatch, tx_b64=tx)

    code = compose.main(
        [
            "--signer",
            PAYER,
            "--rpc-url",
            "https://api.mainnet-beta.solana.com",
            "--network",
            "fork",
        ]
    )
    capsys.readouterr()

    assert code == 0
    assert seen["network_arg"] == "fork"
    receipt = seen["receipt"]
    assert receipt.network == "fork"
    assert evaluate_tx(tx, receipt, require="exact", expected_network="fork").approved
    assert (
        evaluate_tx(tx, receipt, require="exact", expected_network="mainnet").approved
        is False
    )


def test_the_script_refuses_when_the_operator_could_not_name_a_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``unknown`` is a legal answer and a REFUSAL, not an opt-out. An operator who
    cannot say which network the RPC is must not get bytes to sign."""
    import scripts.compose_e2e as compose

    _compose(monkeypatch, tx_b64=_memo(b"buy water"))

    code = compose.main(
        [
            "--signer",
            PAYER,
            "--rpc-url",
            "https://rpc.example.com",
            "--network",
            UNKNOWN_NETWORK,
        ]
    )
    output = capsys.readouterr().out

    assert code == 1
    assert "DO NOT SIGN" in output


def test_the_script_will_not_start_without_a_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No flag, no run — argparse refuses before a single RPC is touched."""
    import scripts.compose_e2e as compose

    _compose(monkeypatch, tx_b64=_memo(b"buy water"))

    with pytest.raises(SystemExit) as caught:
        compose.main(["--signer", PAYER, "--rpc-url", "https://rpc.example.com"])

    assert caught.value.code == 2
