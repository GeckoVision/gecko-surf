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
    SEED_KIND_TOKENS,
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
    # NOTE: fixtures moved to the closed SEED_KIND_TOKENS vocabulary ("account:pubkey",
    # not the old free-form "account:mint") when the structural input guard landed.
    h1 = recipe_hash(
        program_id="P",
        instruction="buy",
        account_names=["user", "pool", "mint"],
        arg_names=["amount", "max_sol"],
        seed_recipes={"pool": ["const:utf8", "account:pubkey"]},
    )
    h2 = recipe_hash(
        program_id="P",
        instruction="buy",
        account_names=["mint", "pool", "user"],  # different order
        arg_names=["max_sol", "amount"],
        seed_recipes={"pool": ["const:utf8", "account:pubkey"]},
    )
    assert h1 == h2


def test_recipe_hash_distinct_across_instruction_and_program() -> None:
    base = dict(
        account_names=["user"],
        arg_names=["amount"],
        seed_recipes={"x": ["const:utf8"]},
    )
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
            seed_recipes={"x": ["resolver"]},
        )
        != h
    )


# --- recipe_hash input guard: values-free by construction (defi-security follow-up) ----
# A hash over low-cardinality secret-adjacent inputs is a dictionary-attack surface, so a
# resolved pubkey/amount/secret must be REJECTED at the boundary — and the raised message
# must never echo the offending value (an exception message is a log line waiting to happen).

# A realistic resolved address (base58 alphabet, pubkey length) — must never be hashable
# as a "name". Distinct from CANARY_PUBKEY, which is Receipt-side.
RESOLVED_PUBKEY = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SECRET_ARG = (
    "sk-AAAAABBBBBCCCCCDDDDDEEEEE"  # OpenAI-shaped, trips looks_like_secret_value
)


def _well_formed_kwargs() -> dict:
    return dict(
        program_id="PUMPProgram1111111111111111111111111111111",
        instruction="buy",
        account_names=["user", "pool", "mint"],
        arg_names=["amount", "max_sol"],
        seed_recipes={"pool": ["const:utf8", "account:pubkey"], "vault": ["resolver"]},
    )


def test_recipe_hash_pinned_for_well_formed_input() -> None:
    # The guard must not perturb the fingerprint of well-formed input: sha256 over the
    # sorted-JSON structural fingerprint, pinned so a refactor can't silently reshape it
    # (a reshaped hash would break every drift series keyed on it).
    assert (
        recipe_hash(**_well_formed_kwargs())
        == "3c3d610a2d51f9bbea1b5128c6caf99bbd5b88c32fff23aca38786ec264e92bf"
    )


def test_recipe_hash_rejects_pubkey_as_account_name() -> None:
    kwargs = _well_formed_kwargs()
    kwargs["account_names"] = ["user", RESOLVED_PUBKEY]
    with pytest.raises(CorpusError) as excinfo:
        recipe_hash(**kwargs)
    assert RESOLVED_PUBKEY not in str(excinfo.value)  # redacted


def test_recipe_hash_rejects_secret_shaped_arg_name() -> None:
    kwargs = _well_formed_kwargs()
    kwargs["arg_names"] = [SECRET_ARG]
    with pytest.raises(CorpusError) as excinfo:
        recipe_hash(**kwargs)
    assert SECRET_ARG not in str(excinfo.value)  # redacted


def test_recipe_hash_rejects_resolved_address_in_seed_recipes() -> None:
    # A resolved base58 address is not in the closed kind vocabulary — structurally
    # impossible to smuggle in as a "kind".
    kwargs = _well_formed_kwargs()
    kwargs["seed_recipes"] = {"pool": [RESOLVED_PUBKEY]}
    with pytest.raises(CorpusError) as excinfo:
        recipe_hash(**kwargs)
    assert RESOLVED_PUBKEY not in str(excinfo.value)  # redacted
    assert RESOLVED_PUBKEY not in SEED_KIND_TOKENS


def test_recipe_hash_rejects_pubkey_as_seed_recipes_key() -> None:
    kwargs = _well_formed_kwargs()
    kwargs["seed_recipes"] = {RESOLVED_PUBKEY: ["resolver"]}
    with pytest.raises(CorpusError) as excinfo:
        recipe_hash(**kwargs)
    assert RESOLVED_PUBKEY not in str(excinfo.value)  # redacted


def test_recipe_hash_rejects_over_long_name() -> None:
    overlong = "a_" * 40  # 80 chars — beyond any identifier, below secret patterns
    kwargs = _well_formed_kwargs()
    kwargs["account_names"] = [overlong]
    with pytest.raises(CorpusError) as excinfo:
        recipe_hash(**kwargs)
    assert overlong not in str(excinfo.value)  # redacted


def test_recipe_hash_rejects_off_vocabulary_kind_and_bare_string_recipe() -> None:
    kwargs = _well_formed_kwargs()
    kwargs["seed_recipes"] = {"pool": ["definitely_not_a_kind"]}
    with pytest.raises(CorpusError):
        recipe_hash(**kwargs)
    # a bare string is Sequence[str] too — must be rejected, not iterated as chars
    kwargs["seed_recipes"] = {"pool": "resolver"}
    with pytest.raises(CorpusError):
        recipe_hash(**kwargs)


def test_seed_kind_tokens_is_closed_and_kind_shaped() -> None:
    # every token is short and kind-shaped; none could hold a resolved address
    assert "resolver" in SEED_KIND_TOKENS
    assert "const:utf8" in SEED_KIND_TOKENS
    assert "account:pubkey" in SEED_KIND_TOKENS
    assert "ordered_pair:max" in SEED_KIND_TOKENS
    assert all(len(token) <= 32 for token in SEED_KIND_TOKENS)


# --- revert_family split (shared vocab, single source of truth) ------------------------
def test_revert_family_splits() -> None:
    assert revert_family("custom_program_error:3012") == ("custom_program_error", 3012)
    assert revert_family("account_error") == ("account_error", None)
    assert revert_family(None) == ("none", None)
    assert revert_family("slippage") == ("slippage", None)
    # fail closed: an unknown class collapses to the "other" family
    assert revert_family("who_knows") == ("other", None)
