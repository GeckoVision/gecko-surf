"""Per-surface first-call-correct: the same observed denominator, split by API.

This is the drill-down under the ONE published rate. It exists because "65% first-call
correct" over a mixed corpus is not a number anyone can act on — it hides which API is
carrying it. Splitting it is only honest if the split obeys the same rules the global
number obeys, so these tests are written as the rules:

1. **Same denominator, sliced — never a wider one.** Only ``source == "observed"`` rows
   count, and a row Gecko's own self-check produced is not in the main corpus at all
   (it is segregated by PATH into ``selfcheck.jsonl``). A per-surface reader that walked
   to that sibling would re-open the hole the segregation just closed.
2. **A thing that could not answer must not answer.** A surface with zero qualifying rows
   gets NO entry — and "not evaluated" is a distinct value from ``0.0``, because a real
   0.0 (every observed call failed) is a finding and an absent one is silence.
3. **Local only, global-only egress.** ``aggregate``/``TelemetryPayload`` stay GLOBAL —
   there is no per-surface key on the wire, and the allowlist fails closed if one is added.
4. **No request/session scoping.** A corpus is a permanent, reused artifact; retrieval must
   read a file written by an unrelated earlier run and must be a pure function of it.
"""

from __future__ import annotations

import json
from pathlib import Path

from gecko import corpus
from gecko.telemetry import (
    PAYLOAD_ALLOWED_KEYS,
    TelemetryError,
    UsageAggregate,
    aggregate,
    assert_payload_allowlisted,
    per_surface_fcc,
    surface_rows,
)

import pytest

# The segregated self-check file (B1, ``fix/corpus-selfcheck-segregation``). Read from
# ``corpus`` once that lands so the tier is named in exactly one place; the literal is the
# pre-merge fallback, and ``test_selfcheck_rows_never_enter_a_per_surface_count`` pins the
# two together the moment the constant exists.
SELFCHECK_FILENAME: str = getattr(corpus, "SELFCHECK_FILENAME", "selfcheck.jsonl")

# A value that must never surface in any per-surface block (mirrors test_telemetry.py).
SENSITIVE_MINT = "SoLSeCrEtMintAddr1111111111111111111111111"


def _outcome(
    surface_id: str,
    *,
    ok: bool,
    operation_id: str = "getThing",
    mode: str = "live",  # live => source "observed" (the qualifying rows)
    attempt: int = 1,
    args: dict | None = None,
    path: str = "/x/{id}",
    surface_rev: str = "rev1",
) -> corpus.CallOutcome:
    return corpus.outcome_from(
        operation_id=operation_id,
        tool_invoke={"method": "GET", "path": path},
        args=args if args is not None else {"id": "v"},
        status=200 if ok else 404,
        error_class="none" if ok else "not_found_404",
        latency_ms=1,
        mode=mode,
        auth_injected=False,
        ts=1,
        surface_id=surface_id,
        surface_rev=surface_rev,
        attempt=attempt,
    )


def _write(path: Path, outcomes: list[corpus.CallOutcome]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for outcome in outcomes:
            fh.write(json.dumps(corpus.to_record(outcome)) + "\n")


# --------------------------------------------------------------------------- #
# 1. The split itself
# --------------------------------------------------------------------------- #
def test_per_surface_fcc_splits_the_observed_rate_by_surface(tmp_path: Path) -> None:
    """Two APIs, two rates — the global 50% hides that one is 100% and one is 0%."""
    path = tmp_path / "corpus.jsonl"
    _write(
        path,
        [
            _outcome("good.example.com", ok=True),
            _outcome("good.example.com", ok=True),
            _outcome("bad.example.com", ok=False),
            _outcome("bad.example.com", ok=False),
        ],
    )

    assert aggregate(path).first_call_correct_rate == 0.5  # the number that hides it

    result = per_surface_fcc(path)
    rates = {
        entry.surface_id: entry.first_call_correct_rate for entry in result.entries
    }
    assert rates == {"good.example.com": 1.0, "bad.example.com": 0.0}
    assert {e.surface_id: e.observed_calls for e in result.entries} == {
        "good.example.com": 2,
        "bad.example.com": 2,
    }
    # The block's denominator is the sum of the entries it is made of — a reader must be
    # able to check the arithmetic without re-reading the corpus.
    assert result.observed_calls == sum(e.observed_calls for e in result.entries) == 4


def test_a_self_healed_call_is_not_first_call_correct_per_surface(
    tmp_path: Path,
) -> None:
    """Inherited from the corrected denominator (#357): attempt > 1 is a retry, and a
    retry that worked is not a FIRST-call correct. The per-surface split must not
    re-launder it."""
    path = tmp_path / "corpus.jsonl"
    _write(
        path,
        [
            _outcome("retry.example.com", ok=True, attempt=2),
            _outcome("retry.example.com", ok=True, attempt=1),
        ],
    )

    entry = per_surface_fcc(path).get("retry.example.com")
    assert entry is not None
    assert entry.observed_calls == 2
    assert entry.first_call_correct == 1
    assert entry.first_call_correct_rate == 0.5


# --------------------------------------------------------------------------- #
# 2. The self-check row — the whole reason this lands after the segregation
# --------------------------------------------------------------------------- #
def test_selfcheck_rows_never_enter_a_per_surface_count(tmp_path: Path) -> None:
    """A ``verify-docs --live`` 404 on an id GECKO invented is evidence about our
    placeholder, not about how an agent calls the API. Those rows are segregated by PATH
    into the sibling file, so the per-surface reader must read ONLY the path it was given
    and must never walk to a sibling.

    The failure this pins: a surface that exists ONLY in the self-check file gets NO entry
    (not a 0.0), and a surface that appears in both is counted on its agent-call rows only.
    """
    path = tmp_path / "corpus.jsonl"
    selfcheck = tmp_path / SELFCHECK_FILENAME
    if hasattr(corpus, "selfcheck_sibling"):  # pragma: no branch - post-merge pin
        assert corpus.selfcheck_sibling(path) == selfcheck

    _write(path, [_outcome("api.example.com", ok=True)])
    _write(
        selfcheck,
        [
            # Same surface: a self-check 404 that would drag its rate 1.0 -> 0.5.
            _outcome("api.example.com", ok=False, operation_id="getInvented"),
            # A surface Gecko ONLY ever self-checked: no agent has called it at all.
            _outcome("probe-only.example.com", ok=False, operation_id="getInvented"),
        ],
    )

    result = per_surface_fcc(path)
    ids = [entry.surface_id for entry in result.entries]
    assert ids == ["api.example.com"]
    assert "probe-only.example.com" not in ids
    assert (
        result.rate_for("probe-only.example.com") is None
    )  # not 0.0 — never evaluated

    entry = result.get("api.example.com")
    assert entry is not None
    assert entry.observed_calls == 1  # the self-check row is NOT in the denominator
    assert entry.first_call_correct_rate == 1.0

    # And the drill-down reads the same file, not the sibling.
    assert surface_rows(path, "probe-only.example.com") is None
    rows = surface_rows(path, "api.example.com")
    assert rows is not None
    assert [row.operation_id for row in rows.rows] == ["getThing"]


# --------------------------------------------------------------------------- #
# 3. "Could not answer" is not "answered zero"
# --------------------------------------------------------------------------- #
def test_a_surface_with_no_observed_rows_gets_no_entry(tmp_path: Path) -> None:
    """Synthetic (a faked recorded 200) and reported (survivorship-biased) rows are not
    evidence of a first call. A surface made only of them has nothing to publish."""
    path = tmp_path / "corpus.jsonl"
    _write(
        path,
        [
            _outcome("real.example.com", ok=True),
            _outcome("synthetic.example.com", ok=True, mode="recorded"),
        ],
    )

    result = per_surface_fcc(path)
    assert [entry.surface_id for entry in result.entries] == ["real.example.com"]
    assert result.get("synthetic.example.com") is None
    assert result.rate_for("synthetic.example.com") is None
    # Suppression is COUNTED, so an empty-looking result can't be mistaken for "no data
    # existed" — but the un-evaluable surface is never NAMED in the result.
    assert result.surfaces_not_evaluated == 1
    assert "synthetic.example.com" not in json.dumps(_as_json(result))


def test_an_unlabeled_legacy_row_fails_closed(tmp_path: Path) -> None:
    """No ``source`` field => not counted as observed. Failing OPEN here would inflate a
    per-API rate with rows whose provenance nobody can reconstruct."""
    path = tmp_path / "corpus.jsonl"
    record = corpus.to_record(_outcome("legacy.example.com", ok=True))
    record.pop("source")
    path.write_text(
        json.dumps(record)
        + "\n{not json}\n"  # malformed line: skipped, never fatal
        + json.dumps(corpus.to_record(_outcome("live.example.com", ok=True)))
        + "\n",
        encoding="utf-8",
    )

    result = per_surface_fcc(path)
    assert [entry.surface_id for entry in result.entries] == ["live.example.com"]
    assert result.rate_for("legacy.example.com") is None


def test_not_evaluated_is_a_distinct_value_from_a_real_zero(tmp_path: Path) -> None:
    """The zero-value class this repo keeps re-hitting: 0.0 must mean "we measured, and
    every observed first call failed" — a finding. Absence must mean silence."""
    path = tmp_path / "corpus.jsonl"
    _write(
        path,
        [
            _outcome("measured-zero.example.com", ok=False),
            _outcome("never-called.example.com", ok=True, mode="recorded"),
        ],
    )

    result = per_surface_fcc(path)
    assert result.rate_for("measured-zero.example.com") == 0.0
    assert result.rate_for("never-called.example.com") is None
    assert result.rate_for("nobody-has-heard-of-this.example.com") is None


def test_a_missing_corpus_is_an_empty_block_not_a_zero_rate(tmp_path: Path) -> None:
    result = per_surface_fcc(tmp_path / "does-not-exist.jsonl")
    assert result.entries == ()
    assert result.observed_calls == 0
    assert result.surfaces_not_evaluated == 0
    assert result.rate_for("anything") is None
    assert result.produced_by  # still labeled — an empty block is still a block


# --------------------------------------------------------------------------- #
# 4. Every block says where it came from, what it divided by, and who made it
# --------------------------------------------------------------------------- #
def test_every_block_carries_provenance_denominator_and_producer(
    tmp_path: Path,
) -> None:
    """A number screenshotted out of context must carry its own caveats."""
    path = tmp_path / "corpus.jsonl"
    _write(path, [_outcome("api.example.com", ok=True)])

    result = per_surface_fcc(path)
    assert result.provenance == "observed"
    assert result.arm == "observed-with-gecko"  # single arm: no without-Gecko control
    assert result.produced_by == "gecko.telemetry.per_surface_fcc"
    assert result.observed_calls == 1

    entry = result.entries[0]
    assert (entry.provenance, entry.arm) == ("observed", "observed-with-gecko")
    assert entry.produced_by == "gecko.telemetry.per_surface_fcc"
    assert entry.observed_calls == 1  # its OWN denominator, not the block's

    rows = surface_rows(path, "api.example.com")
    assert rows is not None
    assert (rows.provenance, rows.arm) == ("observed", "observed-with-gecko")
    assert rows.produced_by == "gecko.telemetry.surface_rows"
    assert rows.observed_calls == 1


# --------------------------------------------------------------------------- #
# 5. The drill-down
# --------------------------------------------------------------------------- #
def test_surface_rows_breaks_the_number_into_the_operations_behind_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "corpus.jsonl"
    _write(
        path,
        [
            _outcome("api.example.com", ok=True, operation_id="listThings"),
            _outcome("api.example.com", ok=False, operation_id="getThing"),
            _outcome("api.example.com", ok=False, operation_id="getThing"),
            _outcome("other.example.com", ok=True, operation_id="listThings"),
        ],
    )

    rows = surface_rows(path, "api.example.com")
    assert rows is not None
    assert rows.surface_id == "api.example.com"
    by_op = {row.operation_id: row for row in rows.rows}
    assert set(by_op) == {"listThings", "getThing"}
    assert by_op["getThing"].observed_calls == 2
    assert by_op["getThing"].first_call_correct_rate == 0.0
    assert by_op["getThing"].error_class_distribution == {"not_found_404": 2}
    assert by_op["listThings"].first_call_correct_rate == 1.0
    # Biggest denominator first — the drill-down leads with what moves the number.
    assert [row.operation_id for row in rows.rows] == ["getThing", "listThings"]
    # The drill-down reconciles EXACTLY with the entry it drills into.
    entry = per_surface_fcc(path).get("api.example.com")
    assert entry is not None
    assert sum(row.observed_calls for row in rows.rows) == entry.observed_calls
    assert sum(row.first_call_correct for row in rows.rows) == entry.first_call_correct


def test_surface_rows_carries_no_value_only_names_and_counts(tmp_path: Path) -> None:
    """Control plane, never data plane: the drill-down is templated paths, arg NAMES and
    counts. The mint the agent passed must not be reachable from any block."""
    path = tmp_path / "corpus.jsonl"
    _write(
        path,
        [
            _outcome(
                "api.example.com",
                ok=True,
                path="/v1/assets/{mint}",
                args={"mint": SENSITIVE_MINT, "limit": 50},
            )
        ],
    )

    rows = surface_rows(path, "api.example.com")
    assert rows is not None
    blob = json.dumps(_as_json(rows)) + json.dumps(_as_json(per_surface_fcc(path)))
    assert SENSITIVE_MINT not in blob
    assert rows.rows[0].path_template == "/v1/assets/{mint}"  # templated, never filled


# --------------------------------------------------------------------------- #
# 6. Local only — the wire stays GLOBAL
# --------------------------------------------------------------------------- #
def test_the_global_aggregate_is_never_grouped_by_surface(tmp_path: Path) -> None:
    """``aggregate`` keeps its shape: adding a per-surface field to the payload would make
    the phone-home carry an API-by-API profile of the consumer's stack."""
    assert "surface_id" not in UsageAggregate.__dataclass_fields__
    assert not any(
        name.startswith("per_surface") for name in UsageAggregate.__dataclass_fields__
    )
    assert not any(name.startswith("per_surface") for name in PAYLOAD_ALLOWED_KEYS)
    assert "surface_rows" not in PAYLOAD_ALLOWED_KEYS


def test_a_per_surface_key_fails_closed_at_the_egress_boundary() -> None:
    with pytest.raises(TelemetryError):
        assert_payload_allowlisted({"per_surface_fcc": [{"surface_id": "x"}]})


def test_per_surface_functions_take_no_sink_and_open_no_socket() -> None:
    """These are read-a-local-file functions. There is no sink argument to pass, so there
    is no accidental opt-in path — egress stays the one, GLOBAL, opt-in ``report``."""
    import inspect

    for fn in (per_surface_fcc, surface_rows):
        params = set(inspect.signature(fn).parameters)
        assert "sink" not in params
        assert not params & {"url", "endpoint", "session", "install_id"}


# --------------------------------------------------------------------------- #
# 7. A corpus is permanent and reused — retrieval must not be request-scoped
# --------------------------------------------------------------------------- #
def test_retrieval_tolerates_cross_request_reuse(tmp_path: Path) -> None:
    """The trap: gating a static, reused corpus behind a request/session id, after which
    it silently returns nothing. There is no such id to pass, and two independent reads of
    a file written by an unrelated earlier run agree exactly."""
    path = tmp_path / "corpus.jsonl"
    _write(path, [_outcome("api.example.com", ok=True)])

    first = per_surface_fcc(path)
    second = per_surface_fcc(path)  # a different "request"; same permanent artifact
    assert first == second
    assert surface_rows(path, "api.example.com") == surface_rows(
        path, "api.example.com"
    )

    import inspect

    for fn in (per_surface_fcc, surface_rows):
        assert not set(inspect.signature(fn).parameters) & {
            "request_id",
            "session_id",
            "run_id",
        }


def test_an_appended_row_is_picked_up_by_the_next_read(tmp_path: Path) -> None:
    """Append-only: the corpus grows across runs and the read reflects it — no snapshot,
    no cache keyed to a first caller."""
    path = tmp_path / "corpus.jsonl"
    _write(path, [_outcome("api.example.com", ok=True)])
    assert per_surface_fcc(path).rate_for("api.example.com") == 1.0

    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(corpus.to_record(_outcome("api.example.com", ok=False))))
        fh.write("\n")
    assert per_surface_fcc(path).rate_for("api.example.com") == 0.5


def _as_json(block: object) -> object:
    """Round-trip a frozen block through plain JSON types (the shape a caller would print
    or a report would render), so a leak test inspects everything reachable."""
    from dataclasses import asdict, is_dataclass

    assert is_dataclass(block) and not isinstance(block, type)
    return asdict(block)
