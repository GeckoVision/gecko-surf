"""Tenancy is DERIVED, never accepted — on BOTH the write and the read path.

Two holes this closes, both on the ``tenancy`` axis ("may this record egress into the
cross-customer corpus?"):

(a) **Write path.** ``outcome_from`` / ``simulated_outcome_from`` took ``tenancy`` as a
    caller-supplied parameter. Set membership was checked, but membership is not
    provenance: any caller could hand the boundary ``"contributed"`` and the row was
    labelled egress-eligible on nothing but the caller's say-so. ``source`` already gets
    the right treatment ("DERIVED from mode so a caller cannot mislabel"); ``tenancy``
    now gets the same one, derived from an explicit LOCAL OPERATOR consent predicate.

(b) **Read path.** ``outcome_from_record`` re-validated NOTHING — a proven asymmetry:
    ``outcome_from`` rejects an off-set ``tenancy`` and ``to_simulated_record`` rejects it
    for the simulated sibling, but the rehydration seam constructed a ``CallOutcome``
    straight out of a hand-written row. A fixture could therefore carry
    ``tenancy="contributed"``, or the off-set ``"EVERYONE-PLEASE"``, into the replay path.

The consent predicate is **default-deny and trusted-source-only**: an explicit env var or
a file under the operator's own gecko config dir, nothing else. Spec text, a response, an
agent arg, or a CWD-relative file can never influence it, and unreadable / unparsable /
missing / unrecognized state yields ``local`` — never anything wider.

Scope note (deliberate): NOTHING in the repo consumes ``tenancy == "contributed"``, and
this change adds no egress. Deriving from ambient operator state is a widening, and it is
only safe while nothing acts on the flag.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from gecko.corpus import (
    TENANCIES,
    CallOutcome,
    CorpusError,
    consented_tenancy,
    outcome_from,
    outcome_from_record,
    simulated_outcome_from,
    to_record,
)
from gecko.simulate import Receipt

TOOL_INVOKE = {
    "method": "GET",
    "path": "/v1/assets/by-mint/{mint}/state",
    "param_locations": {"mint": "path"},
}
ARGS = {"mint": "SoLSeCrEtMintAddr1111111111111111111111111", "limit": 50}


@pytest.fixture(autouse=True)
def _hermetic_consent(monkeypatch, tmp_path):
    """Every test starts from an empty, absolute config home and no consent env var —
    so a real ``~/.gecko`` on the dev machine can never colour these assertions."""
    monkeypatch.delenv("GECKO_CORPUS_CONSENT", raising=False)
    monkeypatch.setenv("GECKO_CONFIG_HOME", str(tmp_path / "confighome"))


def _outcome(**overrides) -> CallOutcome:
    base = dict(
        operation_id="get_asset_state",
        tool_invoke=TOOL_INVOKE,
        args=ARGS,
        status=200,
        error_class="none",
        latency_ms=42,
        mode="live",
        auth_injected=True,
        ts=1_700_000_000_000,
        surface_id="pegana",
        surface_rev="rev-1",
    )
    base.update(overrides)
    return outcome_from(**base)


def _sim_outcome(**overrides) -> object:
    base = dict(
        program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        instruction="buy",
        recipe_hash="0" * 64,
        slot=123,
        network="fork",
        ts=1_700_000_000_000,
        surface_id="orquestra:pump",
    )
    base.update(overrides)
    receipt = Receipt(
        status="pass",
        err=None,
        revert_class=None,
        units_consumed=86_669,
        sol_delta=None,
        tokens_received=None,
        logs_tail=(),
        network_label="surfpool fork (mainnet-backed — NOT mainnet)",
    )
    return simulated_outcome_from(receipt, **base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# (1) a caller cannot hand either boundary a tenancy of its choosing
# --------------------------------------------------------------------------- #
def test_outcome_from_has_no_tenancy_parameter():
    assert "tenancy" not in set(inspect.signature(outcome_from).parameters)
    with pytest.raises(TypeError):
        _outcome(tenancy="contributed")  # type: ignore[call-arg]


def test_simulated_outcome_from_has_no_tenancy_parameter():
    assert "tenancy" not in set(inspect.signature(simulated_outcome_from).parameters)
    with pytest.raises(TypeError):
        _sim_outcome(tenancy="contributed")  # type: ignore[call-arg]


def test_both_boundaries_derive_the_same_consented_tenancy(monkeypatch):
    monkeypatch.setenv("GECKO_CORPUS_CONSENT", "contributed")
    assert consented_tenancy() == "contributed"
    assert _outcome().tenancy == "contributed"
    assert _sim_outcome().tenancy == "contributed"


def test_default_deny_when_the_operator_said_nothing():
    assert consented_tenancy() == "local"
    assert _outcome().tenancy == "local"
    assert _sim_outcome().tenancy == "local"


# --------------------------------------------------------------------------- #
# (2) unrecognized / unreadable consent state yields ``local``, never wider
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw",
    ["", "  ", "yes", "true", "1", "EVERYONE-PLEASE", "contributed,local", "local"],
)
def test_unrecognized_env_consent_is_local(monkeypatch, raw):
    monkeypatch.setenv("GECKO_CORPUS_CONSENT", raw)
    assert consented_tenancy() == "local"
    assert _outcome().tenancy == "local"


def test_consent_file_under_the_config_home_is_honoured(monkeypatch, tmp_path):
    home = tmp_path / "confighome"
    home.mkdir(parents=True, exist_ok=True)
    (home / "corpus-consent").write_text("contributed\n", encoding="utf-8")
    assert consented_tenancy() == "contributed"


def test_unreadable_consent_file_is_local(monkeypatch, tmp_path):
    home = tmp_path / "confighome"
    home.mkdir(parents=True, exist_ok=True)
    # A DIRECTORY where the file should be: read raises OSError -> fail closed.
    (home / "corpus-consent").mkdir()
    assert consented_tenancy() == "local"


def test_undecodable_consent_file_is_local(tmp_path):
    home = tmp_path / "confighome"
    home.mkdir(parents=True, exist_ok=True)
    (home / "corpus-consent").write_bytes(b"\xff\xfe\x00contributed")
    assert consented_tenancy() == "local"


def test_oversized_consent_file_cannot_smuggle_the_token(tmp_path):
    home = tmp_path / "confighome"
    home.mkdir(parents=True, exist_ok=True)
    (home / "corpus-consent").write_text("x" * 4096 + "contributed", encoding="utf-8")
    assert consented_tenancy() == "local"


def test_derived_tenancy_is_always_a_closed_set_member(monkeypatch):
    for raw in ("contributed", "local", "nonsense", ""):
        monkeypatch.setenv("GECKO_CORPUS_CONSENT", raw)
        assert consented_tenancy() in TENANCIES


# --- trusted-source-only: nothing ambient may flip it ---------------------------------
def test_hostile_cwd_file_cannot_flip_tenancy(monkeypatch, tmp_path):
    """The predicate reads the operator's config home, never CWD. A repo/checkout an
    agent can write into (the poisoned-workspace case) must not be able to opt the
    machine into contribution."""
    hostile = tmp_path / "hostile-cwd"
    (hostile / ".gecko").mkdir(parents=True)
    (hostile / "corpus-consent").write_text("contributed", encoding="utf-8")
    (hostile / ".gecko" / "corpus-consent").write_text("contributed", encoding="utf-8")
    monkeypatch.chdir(hostile)
    assert consented_tenancy() == "local"
    assert _outcome().tenancy == "local"


def test_relative_config_home_override_is_refused(monkeypatch, tmp_path):
    """A relative ``GECKO_CONFIG_HOME`` is CWD-relative state; it must not be trusted.
    Falls back to the real home dir (which has no consent file under test)."""
    hostile = tmp_path / "relative-cwd"
    (hostile / "cfg").mkdir(parents=True)
    (hostile / "cfg" / "corpus-consent").write_text("contributed", encoding="utf-8")
    monkeypatch.chdir(hostile)
    monkeypatch.setenv("GECKO_CONFIG_HOME", "cfg")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nowhere"))
    assert consented_tenancy() == "local"


def test_agent_args_and_spec_text_cannot_reach_the_predicate():
    """The predicate takes NO arguments — structurally it cannot read a spec string, an
    agent arg, or a response (the same shape of proof as 'outcome_from takes no body')."""
    assert not inspect.signature(consented_tenancy).parameters


# --------------------------------------------------------------------------- #
# (3)+(4) the read path re-validates: the proven asymmetry
# --------------------------------------------------------------------------- #
def test_outcome_from_record_rejects_a_handwritten_contributed_row():
    row = to_record(_outcome())
    assert row["tenancy"] == "local"
    row["tenancy"] = "contributed"  # hand-edited egress upgrade
    with pytest.raises(CorpusError):
        outcome_from_record(row)


def test_outcome_from_record_rejects_an_off_set_tenancy():
    row = to_record(_outcome())
    row["tenancy"] = "EVERYONE-PLEASE"
    with pytest.raises(CorpusError):
        outcome_from_record(row)


def test_outcome_from_record_rejects_an_off_set_source():
    row = to_record(_outcome())
    row["source"] = "totally-observed"
    with pytest.raises(CorpusError):
        outcome_from_record(row)


def test_read_path_admits_no_tenancy_wider_than_local_consent(monkeypatch):
    """The read gate re-derives from the SAME predicate as the write gate and admits
    nothing WIDER than it: ``contributed`` is legible exactly on a machine whose operator
    opted in — never on the strength of the row's own label. ``local`` (the narrowest
    label) always reads, so opting in never orphans a machine's older rows."""
    monkeypatch.setenv("GECKO_CORPUS_CONSENT", "contributed")
    contributed_row = to_record(_outcome())
    assert contributed_row["tenancy"] == "contributed"
    assert outcome_from_record(contributed_row).tenancy == "contributed"
    monkeypatch.delenv("GECKO_CORPUS_CONSENT")
    local_row = to_record(_outcome())
    assert outcome_from_record(local_row).tenancy == "local"  # narrower: always fine
    with pytest.raises(CorpusError):
        outcome_from_record(contributed_row)  # wider than local consent -> refused


# --- derivable consistency: membership alone still admits a forged row ----------------
def test_outcome_from_record_rejects_source_inconsistent_with_mode():
    row = to_record(_outcome(mode="recorded"))  # synthetic
    row["source"] = "observed"  # forge a wire observation out of a faked 200
    with pytest.raises(CorpusError):
        outcome_from_record(row)


def test_outcome_from_record_rejects_forged_ok():
    row = to_record(_outcome(status=500, error_class="server_5xx"))
    assert row["ok"] is False
    row["ok"] = True
    with pytest.raises(CorpusError):
        outcome_from_record(row)


def test_outcome_from_record_rejects_forged_first_call_correct():
    row = to_record(_outcome(attempt=2))
    assert row["first_call_correct"] is False
    row["first_call_correct"] = True  # inflate the published FCC numerator
    with pytest.raises(CorpusError):
        outcome_from_record(row)


def test_outcome_from_record_rejects_off_set_error_class():
    row = to_record(_outcome())
    row["error_class"] = "everything_is_fine"
    with pytest.raises(CorpusError):
        outcome_from_record(row)


def test_outcome_from_record_rejects_a_non_int_status_shape():
    row = to_record(_outcome())
    row["status"] = "200"  # a string status must not crash the reader NOR pass as ok
    with pytest.raises(CorpusError):
        outcome_from_record(row)


def test_outcome_from_record_still_round_trips_an_honest_row():
    outcome = _outcome()
    assert outcome_from_record(to_record(outcome)) == outcome


def test_shipped_observed_fixture_still_loads():
    """The one in-repo corpus fixture read through this seam must survive the new
    consistency gate — if it doesn't, the fixture was forged."""
    from gecko import verify

    fixture = Path(__file__).parent / "fixtures" / "privy_observed_corpus.jsonl"
    loaded = verify.load_observed_corpus(fixture)
    assert loaded and all(o.source == "observed" for o in loaded.values())
    raw = [
        json.loads(line) for line in fixture.read_text().splitlines() if line.strip()
    ]
    assert {row["tenancy"] for row in raw} == {"local"}
