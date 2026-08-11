"""The D2 corpus WIRING (task #85): orchestrator opt-in → simulated.jsonl → gecko drift.

Delivery 2 built the pieces (simulated_outcome_from / recipe_hash / record_simulated /
detect_drift) but nothing outside their own tests called them. These tests cover the
new connective tissue: the mechanical seed-KIND projection from the real packaged PDA
graphs, the network-label collapse, the shared ``record_landing_outcome`` bridge
(fail-closed on a control-plane violation, best-effort on everything else), the
read-side rehydrator, and the thin ``gecko drift`` CLI. All offline ($0, Pattern B).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gecko import cli
from gecko.corpus import (
    SEED_KIND_TOKENS,
    CorpusError,
    SimulatedOutcome,
    network_category,
    recipe_hash,
    seed_recipes_of,
    simulated_outcome_from_record,
    to_simulated_record,
)
from gecko.networks import UNKNOWN_NETWORK
from gecko.pda import ConstantPdaSeedNode, PdaNode, VariablePdaSeedNode
from gecko.providers.landing_record import record_landing_outcome
from gecko.providers.meteora import _load_meteora_pdas
from gecko.providers.pumpfun import _load_pumpfun_pdas
from gecko.simulate import Receipt

# A realistic resolved address — must never survive as a "name" in a fingerprint.
RESOLVED_PUBKEY = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def _receipt(*, status: str = "pass", revert_class: str | None = None) -> Receipt:
    return Receipt(
        status=status,  # type: ignore[arg-type]
        err=None,
        revert_class=revert_class,
        units_consumed=86_669,
        sol_delta=-31,
        tokens_received=None,
        logs_tail=("Program success",),
        network_label="surfpool fork (mainnet-backed — NOT mainnet)",
    )


# --- the seed-kind helper must ACCEPT the real packaged configs (task step 2) ----------


@pytest.mark.parametrize("load", [_load_pumpfun_pdas, _load_meteora_pdas])
def test_seed_recipes_of_real_config_passes_the_guard(load) -> None:
    # The projection is mechanical (node model → closed tokens), so hashing the REAL
    # config graphs must succeed — the guard rejecting our own packaged configs would
    # mean the vocabulary and the node model have drifted apart.
    recipes = seed_recipes_of(load())
    assert recipes  # both programs carry PDA recipes
    for kinds in recipes.values():
        assert kinds  # every node has at least one seed
        assert all(kind in SEED_KIND_TOKENS for kind in kinds)
    digest = recipe_hash(
        program_id="P",
        instruction="x",
        account_names=list(recipes),
        arg_names=["amount"],
        seed_recipes=recipes,
    )
    assert len(digest) == 64  # sha256 hex — the guard admitted the whole graph


def test_seed_kind_token_covers_every_node_shape() -> None:
    node = PdaNode(
        name="pool",
        seeds=(
            ConstantPdaSeedNode(b"pool", "utf8"),
            VariablePdaSeedNode("mint", "account", "pubkey"),
            VariablePdaSeedNode("index", "argument", "le", width=8),
        ),
    )
    assert seed_recipes_of({"pool": node}) == {
        "pool": ["const:utf8", "account:pubkey", "argument:le"]
    }


# --- network_category: closed-set collapse, fail closed --------------------------------


def test_network_category_collapses_to_closed_set() -> None:
    # fork wins over the "NOT mainnet" caveat text; anything unrecognised — including a
    # URL and None — fails closed to the CATCH-ALL, now spelled `unknown`. It was `other`
    # while the corpus owned this vocabulary alone; there is one declaration now
    # (`gecko.networks`) and rows written under the old spelling still read back through
    # LEGACY_NETWORK_ALIASES. Same bucket, one name.
    assert network_category("surfpool fork (mainnet-backed — NOT mainnet)") == "fork"
    assert network_category("simulated (fork/RPC snapshot — not mainnet)") == "fork"
    assert network_category("mainnet") == "mainnet"
    assert network_category("devnet snapshot") == "devnet"
    assert network_category("http://evil.example/rpc") == UNKNOWN_NETWORK
    assert network_category(None) == UNKNOWN_NETWORK


# --- record_landing_outcome: fail closed vs best-effort --------------------------------


def test_record_landing_outcome_writes_one_categorical_row(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"

    def rpc(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        assert method == "getSlot"
        return {"result": 250_000_123}

    record_landing_outcome(
        _receipt(),
        program_id=RESOLVED_PUBKEY,
        instruction="buy",
        account_names=["user", "mint"],
        arg_names=["amount"],
        pdas=_load_pumpfun_pdas(),
        network_label="surfpool fork (mainnet-backed — NOT mainnet)",
        rpc_url="http://127.0.0.1:8899",
        rpc_call=rpc,
        record_to=corpus,
    )
    sibling = tmp_path / "simulated.jsonl"
    assert sibling.exists() and not corpus.exists()
    row = json.loads(sibling.read_text().strip())
    assert row["status"] == "pass"
    assert row["network"] == "fork"
    assert row["slot"] == 250_000_123
    assert row["source"] == "simulated"


def test_record_landing_outcome_surfaces_corpus_error(tmp_path: Path) -> None:
    # A resolved pubkey posing as an account NAME is a control-plane violation — it
    # must RAISE (fail closed), never be swallowed into a silent no-write.
    with pytest.raises(CorpusError):
        record_landing_outcome(
            _receipt(),
            program_id="P",
            instruction="buy",
            account_names=["user", RESOLVED_PUBKEY],
            arg_names=["amount"],
            pdas=_load_pumpfun_pdas(),
            network_label=None,
            rpc_url="http://127.0.0.1:8899",
            rpc_call=lambda *_: {"result": 1},
            record_to=tmp_path / "corpus.jsonl",
        )
    assert not (tmp_path / "simulated.jsonl").exists()


def test_record_landing_outcome_is_best_effort_on_io_failure(tmp_path: Path) -> None:
    # An unwritable target (parent is a FILE) must never break the simulate call —
    # mirror record()'s posture: swallowed with a redacted note.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    record_landing_outcome(
        _receipt(),
        program_id="P",
        instruction="buy",
        account_names=["user"],
        arg_names=["amount"],
        pdas=_load_pumpfun_pdas(),
        network_label=None,
        rpc_url="http://127.0.0.1:8899",
        rpc_call=lambda *_: {"result": 1},
        record_to=blocker / "corpus.jsonl",  # mkdir under a file → OSError inside
    )  # no raise = pass


def test_record_landing_outcome_slot_is_best_effort(tmp_path: Path) -> None:
    # An injected transport that doesn't answer getSlot (the offline fakes raise) must
    # not lose the row — slot degrades to None, the row still lands.
    def rpc(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected method {method}")

    record_landing_outcome(
        _receipt(),
        program_id="P",
        instruction="buy",
        account_names=["user"],
        arg_names=["amount"],
        pdas=_load_pumpfun_pdas(),
        network_label=None,
        rpc_url="http://127.0.0.1:8899",
        rpc_call=rpc,
        record_to=tmp_path / "corpus.jsonl",
    )
    row = json.loads((tmp_path / "simulated.jsonl").read_text().strip())
    assert row["slot"] is None
    # None label fails closed to the catch-all (spelled `unknown` since the vocabulary
    # was unified with the signing gate's).
    assert row["network"] == UNKNOWN_NETWORK


# --- the read side: fail-closed rehydration -------------------------------------------


def _row(**kw: Any) -> SimulatedOutcome:
    base = SimulatedOutcome(
        ts=1_700_000_000_000,
        surface_id="orquestra:P",
        program_id="P",
        instruction="buy",
        recipe_hash="H",
        status="pass",
        revert_class="none",
        error_code=None,
        units_consumed=86_669,
        slot=1,
        network="fork",
        source="simulated",
    )
    return replace(base, **kw)


def test_simulated_outcome_from_record_round_trips() -> None:
    outcome = _row()
    assert simulated_outcome_from_record(to_simulated_record(outcome)) == outcome


def test_simulated_outcome_from_record_fails_closed() -> None:
    record = to_simulated_record(_row())
    with pytest.raises(CorpusError):  # a leaked-log-shaped extra key
        simulated_outcome_from_record({**record, "logs": "Program log: leaked"})
    with pytest.raises(CorpusError):  # a truncated record is never defaulted
        simulated_outcome_from_record(
            {k: v for k, v in record.items() if k != "status"}
        )
    with pytest.raises(CorpusError):  # off-vocabulary axis on a hand-edited row
        simulated_outcome_from_record({**record, "network": "mars"})


# --- gecko drift: the thin CLI over detect_drift (task step 4) -------------------------


def _write_series(path: Path, rows: list[SimulatedOutcome]) -> None:
    path.write_text(
        "".join(json.dumps(to_simulated_record(row)) + "\n" for row in rows)
    )


def test_drift_cli_prints_a_confirmed_flip(tmp_path: Path, capsys) -> None:
    # 2 pass slots then 2 fail slots on one recipe_hash: an N-confirmed (n=2) flip.
    _write_series(
        tmp_path / "simulated.jsonl",
        [
            _row(slot=1),
            _row(slot=2),
            _row(status="fail", revert_class="account_error", slot=3),
            _row(status="fail", revert_class="account_error", slot=4),
        ],
    )
    code = cli.main(["drift", str(tmp_path / "simulated.jsonl")])
    out = capsys.readouterr().out
    assert code == 1  # drift detected
    assert "DRIFT" in out
    assert "pass:none -> fail:account_error" in out
    assert "confirmed slot 4" in out


def test_drift_cli_stable_series_exits_zero(tmp_path: Path, capsys) -> None:
    # Also exercises sibling routing: pass the CORPUS path, the CLI reads its
    # simulated.jsonl sibling (the same routing record_simulated writes with).
    _write_series(tmp_path / "simulated.jsonl", [_row(slot=s) for s in (1, 2, 3)])
    code = cli.main(["drift", str(tmp_path / "corpus.jsonl")])
    assert code == 0
    assert "no drift detected (3 row(s), 1 recipe(s))" in capsys.readouterr().out


def test_drift_cli_json_output(tmp_path: Path, capsys) -> None:
    _write_series(
        tmp_path / "simulated.jsonl",
        [
            _row(slot=1),
            _row(status="fail", revert_class="slippage", slot=2),
            _row(status="fail", revert_class="slippage", slot=3),
        ],
    )
    code = cli.main(["drift", "--json", str(tmp_path / "simulated.jsonl")])
    events = json.loads(capsys.readouterr().out)
    assert code == 1
    assert events[0]["from_class"] == "pass:none"
    assert events[0]["to_class"] == "fail:slippage"


def test_drift_cli_missing_and_poisoned_input(tmp_path: Path, capsys) -> None:
    assert cli.main(["drift", str(tmp_path / "nope.jsonl")]) == 2
    poisoned = tmp_path / "simulated.jsonl"
    row = to_simulated_record(_row())
    poisoned.write_text(json.dumps({**row, "logs": "Program log: leaked"}) + "\n")
    assert cli.main(["drift", str(poisoned)]) == 2  # fail closed, never defaulted
    err = capsys.readouterr().err
    assert "unreadable simulated corpus" in err
    assert "Program log: leaked" not in err  # the poisoned VALUE is never echoed
