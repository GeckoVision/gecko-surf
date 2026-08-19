"""`gaps: []` is not "ready to run", and the plan now says so out loud.

A live agent, handed jurassic_fi's `contribute` plan, reported that an empty `gaps` list
"reads as 'this plan is ready to run'" while five of the instruction's eight accounts had
no obtainable value. It was not misreading. `gaps` grades DERIVATION RECIPES: empty means
every recipe in the plan is trusted, and it has never said anything about whether the
caller can produce the values those recipes consume.

So `gaps` keeps its meaning — redefining it would break every consumer that reads it
correctly — and the second fact gets its own field.
"""

from __future__ import annotations

from gecko.find_start import (
    READINESS_NOT_ASSESSED,
    DeriveStep,
    Readiness,
    find_start,
    format_result,
)

CONTRIBUTE = "contribute usdc to a jurassic finance token sale"


def _start(intent: str) -> object:
    result = find_start(intent)
    return next(p for p in result.starts if p.kind == "start")


def test_an_empty_gaps_list_no_longer_stands_alone_as_the_verdict() -> None:
    point = _start(CONTRIBUTE)

    assert point.gaps == ()  # every recipe here really is trusted
    assert point.readiness.status == "blocked"  # and the plan still cannot run
    assert "admin" in point.readiness.must_obtain
    assert "launch_id" in point.readiness.must_obtain


def test_the_readiness_note_says_what_gaps_does_and_does_not_cover() -> None:
    """The distinction is the deliverable. A field that is merely present but unexplained
    reproduces the same misreading one level down."""
    note = _start(CONTRIBUTE).readiness.note

    assert "gaps" in note.lower()
    assert "read_accounts" in note


def test_every_plan_step_says_who_produces_its_address() -> None:
    """`provenance` grades the recipe; `supplied_by` says whether there IS one to run.
    A step can be `extracted` — the artifact really does name the slot — and still have
    no obtainable value."""
    point = _start(CONTRIBUTE)

    assert {s.supplied_by for s in point.derive_plan} <= {"derived", "caller"}
    assert all(s.supplied_by == "derived" for s in point.derive_plan)


def test_a_step_that_never_said_who_supplies_it_defaults_to_the_caller() -> None:
    """The fail-closed default: an unlabelled step must not read as one Gecko derives."""
    assert DeriveStep(account="x", provenance="extracted").supplied_by == "caller"


def test_a_hand_built_start_point_is_not_assessed_rather_than_approved() -> None:
    """A missing assessment and a passed one must never be the same value."""
    assert READINESS_NOT_ASSESSED.status == "not_assessed"
    assert isinstance(READINESS_NOT_ASSESSED, Readiness)


def test_the_rendered_plan_prints_readiness_and_keeps_the_provenance_tag() -> None:
    text = format_result(find_start(CONTRIBUTE))

    assert "readiness:  BLOCKED" in text
    assert "still to obtain:" in text
    # the bracket stays the provenance tag; who supplies the address rides beside it
    assert "[recovered]" in text
