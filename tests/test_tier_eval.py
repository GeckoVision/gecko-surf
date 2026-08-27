"""Falsifier 1a — the tier-classifier GATEKEEPER (semantic-depth §2.6, §6.1).

Pure/offline/$0. Runs ``evaluate_tier`` over a frozen, sha256-pinned tier-labels fixture
spanning read/write/transfer across three specs (Privy carries the real transfers —
``transfer``/``createTransferIntent``/``withdrawFunds``/on-off-ramp/swap; txodds + pegana add
read/write breadth). If L1+L2 cannot clear **precision >= 0.95 @ recall >= 0.80**, the tier
signal does NOT ship. This test is that gate.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gecko.evaluate import evaluate_tier, load_tier_labels
from gecko.evidence import Joined, Signal, Uninterpretable, corpus_rev, require_signal
from gecko.ingest import extract_operations, load_spec

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden"
LABELS = GOLDEN / "tier_labels.jsonl"

SPEC_PATHS = {
    "privy": GOLDEN / "privy_openapi.json",
    "txodds": FIXTURES / "txodds_docs.yaml",
    "pegana": FIXTURES / "pegana_openapi.json",
}


def _join_labels_to_ops(
    rows: list[dict[str, str]],
) -> tuple[list, dict[str, str], Signal]:
    """Join the labels to the specs by ``operation_id``, and refuse a join that did not land.

    The join is where this eval can go quiet without going red: a label whose id matches no
    operation is simply skipped by ``evaluate_tier``, so a broken key shrinks the
    denominator instead of failing, and the survivors can still clear the gate. ``Joined``
    makes the claim explicit — every labeled op must be found — and the refusal names both
    key shapes rather than reporting the shortfall as coverage.
    """
    wanted: dict[str, set[str]] = {}
    for r in rows:
        wanted.setdefault(r["spec"], set()).add(r["operation_id"])
    labels = {r["operation_id"]: r["tier"] for r in rows}
    ops = []
    matched: set[str] = set()
    for spec_name, ids in wanted.items():
        spec = load_spec(str(SPEC_PATHS[spec_name]))
        by_id = {o.operation_id: o for o in extract_operations(spec)}
        for oid in sorted(ids & set(by_id)):
            matched.add(oid)
            ops.append(by_id[oid])
    signal = require_signal(
        "tier labels -> spec operations",
        denominators={"labeled_ops": len(labels)},
        joined=Joined("labels->ops", claimed=set(labels), matched=matched),
        corpus=[corpus_rev(LABELS, name="tier_labels")],
    )
    return ops, labels, signal


def _labeled_operations() -> tuple[list, dict[str, str]]:
    """Exactly the labeled ops (per spec, collision-free) + the id->tier map."""
    ops, labels, _ = _join_labels_to_ops(load_tier_labels(LABELS))
    return ops, labels


def test_tier_labels_are_frozen() -> None:
    committed = (GOLDEN / "tier_labels.jsonl.sha256").read_text().strip()
    actual = hashlib.sha256(LABELS.read_bytes()).hexdigest()
    assert actual == committed, (
        "tier_labels.jsonl changed but its .sha256 was not re-frozen"
    )


def test_fixture_spans_all_tiers_and_at_least_two_specs() -> None:
    rows = load_tier_labels(LABELS)
    assert {r["tier"] for r in rows} == {"read", "write", "transfer"}
    assert len({r["spec"] for r in rows}) >= 2
    # Privy is the transfer-bearing spec.
    transfers = [r for r in rows if r["tier"] == "transfer"]
    assert transfers and all(r["spec"] == "privy" for r in transfers)
    assert {"transfer", "createTransferIntent", "withdrawFunds"} <= {
        r["operation_id"] for r in transfers
    }


def test_tier_precision_and_recall_clear_the_ship_gate() -> None:
    ops, labels = _labeled_operations()
    result = evaluate_tier(ops, labels)
    # The GATE: false transfer can block a paying call, so precision dominates.
    assert result.precision >= 0.95, (
        f"tier precision {result.precision:.3f} < 0.95 — tier signal must NOT ship. "
        f"confusion={dict(result.confusion)}"
    )
    assert result.recall >= 0.80, (
        f"tier recall {result.recall:.3f} < 0.80. confusion={dict(result.confusion)}"
    )
    # The transfer class must be exercised on BOTH sides before those two rates mean
    # anything: precision's denominator is the high-confidence transfer predictions, and
    # recall's is the labeled transfers. Either one empty and the gate clears on a class
    # nobody scored. Five is the floor a 0.95 precision claim needs to be worth stating.
    require_signal(
        "tier ship gate",
        denominators={
            "transfer_true": result.transfer_true,
            "transfer_high_pred": result.transfer_high_pred,
        },
        floor=5,
    )
    # The same question asked the way a gatekeeper should ask it. `clears_ship_gate` is
    # False on an unmeasured eval, so this no longer depends on the sanity lines above
    # being remembered at every other call site (see test_tier_eval_undetermined.py).
    assert result.clears_ship_gate()


def test_a_label_join_on_the_wrong_key_is_uninterpretable_not_a_thin_pass() -> None:
    """The labels join to the specs by ``operation_id``. Key them any other way — a spec
    prefix left on, a rename on one side — and the join yields a handful of ops that can
    still clear 0.95/0.80 on their own, while the class the gate exists to measure was
    never scored. The refusal has to happen at the JOIN, before the rates are computed.
    """
    rows = load_tier_labels(LABELS)
    mangled = [dict(r, operation_id=f"{r['spec']}:{r['operation_id']}") for r in rows]
    with pytest.raises(Uninterpretable) as excinfo:
        _join_labels_to_ops(mangled)
    assert excinfo.value.reason == "join_shortfall"
    # The message has to carry both key shapes, or the reader sees only a coverage number
    # and reads a wiring bug as a thin result.
    text = str(excinfo.value)
    for key in sorted({r["operation_id"] for r in mangled})[:3]:
        assert key in text
    assert "matched like nothing" in text


def test_the_real_join_covers_every_label_so_the_refusal_above_is_a_guard() -> None:
    # The contrast arm: as committed, every labeled op joins, so the test above fails for
    # the mangling and not because the join is broken for everyone.
    _, _, signal = _join_labels_to_ops(load_tier_labels(LABELS))
    assert signal.coverage == 1.0


def test_low_confidence_false_positive_is_contained_not_blocking() -> None:
    # mint_magic (POST /v1/auth/magic/mint) is a documented money-verb collision: "mint" is
    # a crypto verb, but this is magic-link auth. The classifier degrades it to transfer/LOW
    # (op.transfer_maybe, 12 pts) — it never blocks (12 + a 35-pt predicate < 60) and does not
    # count against the precision floor. Proof the trap is contained, not a paying-call block.
    from gecko.risk import classify_operation

    spec = load_spec(str(SPEC_PATHS["pegana"]))
    op = next(o for o in extract_operations(spec) if o.operation_id == "mint_magic")
    res = classify_operation(op)
    assert (res.tier, res.confidence) != ("transfer", "high")
