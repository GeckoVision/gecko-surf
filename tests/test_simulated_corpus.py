"""Control-plane gate for the simulated (self-run Receipt) corpus tier — D2.

Mirrors ``tests/test_redteam_corpus.py``/``test_corpus_controlplane.py``: a
``SimulatedOutcome`` may persist ONLY categorical/structural metadata — never a pubkey,
balance, amount, instruction data, the revert LOG string, or an RPC URL. The killer test
plants canary VALUES in the Receipt's value fields (sol_delta/tokens_received/logs_tail/
err) and asserts NONE reach the serialized row (the defi-security grep).
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from gecko.corpus import (
    NETWORKS,
    REVERT_CLASSES,
    SIM_STATUSES,
    SIMULATED_ALLOWED_KEYS,
    CorpusError,
    SimulatedOutcome,
    assert_simulated_allowlisted,
    recipe_hash,
    record_simulated,
    simulated_outcome_from,
    to_simulated_record,
)
from gecko.simulate import REVERT_FAMILIES, Receipt, revert_family

# Secret-shaped VALUES the Receipt carries for the human caller — none may reach the corpus.
CANARY_PUBKEY = "CANARYPUBKEY1111111111111111111111111111111"
CANARY_AMOUNT = 987654321
CANARY_LOG = f"Program log: transferred {CANARY_AMOUNT} to {CANARY_PUBKEY}"


def _receipt(
    *, status: str = "fail", revert_class: str | None = "account_error"
) -> Receipt:
    # Every VALUE field is a canary; only status/revert_class/units are categorical/public.
    return Receipt(
        status=status,  # type: ignore[arg-type]
        err={"InstructionError": [0, {"Custom": 3012}], "leaked": CANARY_PUBKEY},
        revert_class=revert_class,
        units_consumed=31000,
        sol_delta=-CANARY_AMOUNT,
        tokens_received=CANARY_AMOUNT,
        logs_tail=(CANARY_LOG, "Program failed"),
        network_label="simulated (fork/RPC snapshot — not mainnet)",
    )


def _outcome(**kw) -> SimulatedOutcome:
    base = dict(
        program_id="PUMPProgram1111111111111111111111111111111",
        instruction="buy",
        recipe_hash="deadbeef",
        slot=250,
        network="fork",
        ts=1_700_000_000_000,
        surface_id="orquestra:PUMPProgram",
    )
    base.update(kw)
    return simulated_outcome_from(_receipt(), **base)  # type: ignore[arg-type]


# --- the defi-security grep: no VALUE reaches the serialized row -----------------------
def test_simulated_outcome_from_is_values_free() -> None:
    outcome = _outcome()
    blob = json.dumps(to_simulated_record(outcome))
    for canary in (
        CANARY_PUBKEY,
        str(CANARY_AMOUNT),
        "CANARY",
        "transferred",
        "leaked",
    ):
        assert canary not in blob
    # positive: only the categorical/public fields survived
    assert outcome.status == "fail"
    assert outcome.revert_class == "account_error"
    assert outcome.error_code is None  # account_error carries no code
    assert outcome.units_consumed == 31000  # public metric, like latency_ms
    assert outcome.source == "simulated"
    assert outcome.tenancy == "local"


def test_builder_never_reads_value_fields_into_the_record() -> None:
    outcome = _outcome()
    record_dict = to_simulated_record(outcome)
    assert set(record_dict) == SIMULATED_ALLOWED_KEYS
    for forbidden in (
        "sol_delta",
        "tokens_received",
        "logs_tail",
        "err",
        "network_label",
    ):
        assert forbidden not in record_dict


def test_custom_program_error_code_is_split_out() -> None:
    outcome = _outcome(**{})
    # a custom_program_error receipt yields a public NUMBER in error_code, family in class
    receipt = _receipt(revert_class="custom_program_error:6002")
    out = simulated_outcome_from(
        receipt,
        program_id="P",
        instruction="buy",
        recipe_hash="h",
        slot=1,
        network="fork",
        ts=1,
        surface_id="orquestra:P",
    )
    assert out.revert_class == "custom_program_error"
    assert out.error_code == 6002
    assert outcome.revert_class in REVERT_CLASSES


# --- allowlist + closed-set rejection (fail closed) -----------------------------------
def test_allowlist_matches_dataclass_fields() -> None:
    assert SIMULATED_ALLOWED_KEYS == set(SimulatedOutcome.__dataclass_fields__)


def test_to_simulated_record_rejects_non_allowlisted_key() -> None:
    tampered = to_simulated_record(_outcome())
    tampered["logs"] = CANARY_LOG  # a log string sneaking in as a new key
    with pytest.raises(CorpusError):
        assert_simulated_allowlisted(tampered)


def test_to_simulated_record_rejects_off_set_status() -> None:
    with pytest.raises(CorpusError):
        to_simulated_record(replace(_outcome(), status="exploded"))


def test_to_simulated_record_rejects_off_set_revert_class() -> None:
    with pytest.raises(CorpusError):
        to_simulated_record(replace(_outcome(), revert_class="not_a_family"))


def test_to_simulated_record_rejects_off_set_network() -> None:
    with pytest.raises(CorpusError):
        to_simulated_record(replace(_outcome(), network="mars"))
    # sanity: the closed sets are the source of truth
    assert SIM_STATUSES == {"pass", "fail", "unknown"}
    assert NETWORKS == {"fork", "mainnet", "devnet", "other"}
    assert REVERT_CLASSES is REVERT_FAMILIES


# --- segregated write ------------------------------------------------------------------
def test_record_simulated_segregates_and_leaks_nothing(tmp_path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    record_simulated(_outcome(), corpus)
    sibling = tmp_path / "simulated.jsonl"
    assert sibling.exists()
    assert not corpus.exists()  # never pollutes the main (wire-only) corpus
    raw = sibling.read_text()
    for canary in (CANARY_PUBKEY, str(CANARY_AMOUNT), "CANARY"):
        assert canary not in raw
    row = json.loads(raw.strip())
    assert set(row) == SIMULATED_ALLOWED_KEYS
    assert row["source"] == "simulated"


# --- recipe_hash: values-free structural fingerprint ----------------------------------
def test_recipe_hash_stable_across_ordering_and_resolved_values() -> None:
    # The hash takes NAMES only, so two runs of the same intent (accounts in any order,
    # resolved to different pubkeys per user) fingerprint identically.
    h1 = recipe_hash(
        program_id="P",
        instruction="buy",
        account_names=["user", "pool", "mint"],
        arg_names=["amount", "max_sol"],
        seed_recipes={"pool": ["account:mint"]},
    )
    h2 = recipe_hash(
        program_id="P",
        instruction="buy",
        account_names=["mint", "pool", "user"],  # different order
        arg_names=["max_sol", "amount"],
        seed_recipes={"pool": ["account:mint"]},
    )
    assert h1 == h2


def test_recipe_hash_distinct_across_instruction_and_program() -> None:
    base = dict(account_names=["user"], arg_names=["amount"], seed_recipes={"x": ["k"]})
    h = recipe_hash(program_id="P", instruction="buy", **base)  # type: ignore[arg-type]
    assert recipe_hash(program_id="P", instruction="sell", **base) != h  # type: ignore[arg-type]
    assert recipe_hash(program_id="Q", instruction="buy", **base) != h  # type: ignore[arg-type]
    # a change in the RECOVERED seed recipe (our own comprehension) is also a new fingerprint
    assert (
        recipe_hash(
            program_id="P",
            instruction="buy",
            account_names=["user"],
            arg_names=["amount"],
            seed_recipes={"x": ["k2"]},
        )
        != h
    )


# --- revert_family split (shared vocab, single source of truth) ------------------------
def test_revert_family_splits() -> None:
    assert revert_family("custom_program_error:3012") == ("custom_program_error", 3012)
    assert revert_family("account_error") == ("account_error", None)
    assert revert_family(None) == ("none", None)
    assert revert_family("slippage") == ("slippage", None)
    # fail closed: an unknown class collapses to the "other" family
    assert revert_family("who_knows") == ("other", None)
