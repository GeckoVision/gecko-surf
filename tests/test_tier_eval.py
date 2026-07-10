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

from gecko.evaluate import evaluate_tier, load_tier_labels
from gecko.ingest import extract_operations, load_spec

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden"
LABELS = GOLDEN / "tier_labels.jsonl"

SPEC_PATHS = {
    "privy": GOLDEN / "privy_openapi.json",
    "txodds": FIXTURES / "txodds_docs.yaml",
    "pegana": FIXTURES / "pegana_openapi.json",
}


def _labeled_operations() -> tuple[list, dict[str, str]]:
    """Build exactly the labeled ops (per spec, collision-free) + the id->tier map."""
    rows = load_tier_labels(LABELS)
    wanted: dict[str, set[str]] = {}
    for r in rows:
        wanted.setdefault(r["spec"], set()).add(r["operation_id"])
    labels = {r["operation_id"]: r["tier"] for r in rows}
    ops = []
    for spec_name, ids in wanted.items():
        spec = load_spec(str(SPEC_PATHS[spec_name]))
        by_id = {o.operation_id: o for o in extract_operations(spec)}
        for oid in ids:
            assert oid in by_id, f"{spec_name}: labeled op {oid!r} not in spec"
            ops.append(by_id[oid])
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
    # Sanity: the transfer class is actually exercised (not a vacuous pass).
    assert result.transfer_true >= 5
    assert result.transfer_high_pred >= 5


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
