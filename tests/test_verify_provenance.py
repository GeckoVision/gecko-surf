"""VAS-1 — the CLAIMED -> verdict provenance tier (model + pure lift only).

These tests pin the surface model additions and the ``verdict_from_replay`` lift.
No network: every outcome is a synthetic ``CallOutcome`` built through the shipped
control-plane factory (light fakes, not mocks).
"""

from __future__ import annotations

import dataclasses
import json

from gecko import corpus, graph, validator
from gecko.graph import Node, VerifyVerdict


def _outcome(
    *,
    status: int | None,
    mode: str,
    error_class: str = "none",
) -> corpus.CallOutcome:
    """A synthetic capture outcome via the shipped, payload-free factory.

    ``source`` (did the status come off the wire?) is DERIVED from ``mode`` inside the
    factory, so we can never mislabel a recorded run as observed — which is exactly the
    signal the lift keys off.
    """
    return corpus.outcome_from(
        operation_id="getWidget",
        tool_invoke={"method": "GET", "path": "/widgets/{id}"},
        args={"id": "abc"},
        status=status,
        error_class=error_class,
        latency_ms=None,
        mode=mode,
        auth_injected=False,
        ts=0,
        surface_id="s1",
        surface_rev="r1",
    )


# --- the canonical provenance type carries CLAIMED, and is imported not redeclared ---
def test_claimed_is_a_canonical_provenance_value() -> None:
    values = set(graph.Provenance.__args__)  # type: ignore[attr-defined]
    assert values == {"EXTRACTED", "DECLARED", "INFERRED", "CLAIMED"}


def test_provenance_is_single_source_of_truth() -> None:
    # Consumers must import the canonical Literal, never redeclare a CLAIMED-bearing copy.
    src = (graph.__file__ or "").rsplit("/", 1)[0]
    import pathlib

    hits = [
        p.name
        for p in pathlib.Path(src).glob("*.py")
        if p.name != "graph.py" and "CLAIMED" in p.read_text()
    ]
    assert hits == [], f"CLAIMED redeclared/hardcoded outside graph.py: {hits}"


# --- the lift: an already-obtained outcome -> a verdict (no network) -----------------
def test_live_2xx_matching_shape_is_verified() -> None:
    v = validator.verdict_from_replay(_outcome(status=200, mode="live"))
    assert v.status == "VERIFIED"
    assert v.basis == ("replay:200",)
    assert v.shape_ok is True


def test_live_404_is_refuted_and_basis_names_it() -> None:
    v = validator.verdict_from_replay(
        _outcome(status=404, mode="live", error_class="not_found_404")
    )
    assert v.status == "REFUTED"
    assert "replay:404" in v.basis
    assert v.shape_ok is False


def test_live_preflight_contract_failure_is_refuted() -> None:
    # status None with a wire source == the request never formed (contract mismatch).
    v = validator.verdict_from_replay(
        _outcome(status=None, mode="live", error_class="malformed_request")
    )
    assert v.status == "REFUTED"
    assert any(b.startswith("contract-mismatch:") for b in v.basis)


def test_recorded_only_is_unverified_never_overclaimed() -> None:
    # Recorded mode fabricates a 200; it must NOT be sold as VERIFIED.
    v = validator.verdict_from_replay(_outcome(status=200, mode="recorded"))
    assert v.status == "UNVERIFIED"
    assert v.basis == ("no-access:recorded-only",)


# --- VerifyVerdict is control-plane clean + serializable ------------------------------
def test_verdict_carries_no_payload_field() -> None:
    fields = set(VerifyVerdict.__dataclass_fields__)
    assert fields == {"status", "basis", "shape_ok"}
    banned = ("payload", "body", "response", "value", "secret", "content", "data")
    assert not any(b in f for f in fields for b in banned)


def test_verdict_is_json_serializable() -> None:
    v = VerifyVerdict(status="VERIFIED", basis=("replay:200",), shape_ok=True)
    loaded = json.loads(json.dumps(dataclasses.asdict(v)))
    assert loaded == {"status": "VERIFIED", "basis": ["replay:200"], "shape_ok": True}


# --- additive / backward-compatible ---------------------------------------------------
def test_op_node_defaults_verify_to_none() -> None:
    node = Node(kind="operation", id="op:getWidget", name="getWidget")
    assert node.verify is None


def test_verify_field_does_not_change_graph_serialization() -> None:
    # serialize() must be byte-identical whether or not a verify verdict is attached:
    # the content hash excludes the verified-against-reality tier (it is drift-dependent).
    plain = Node(kind="operation", id="op:getWidget", name="getWidget")
    verified = dataclasses.replace(
        plain, verify=VerifyVerdict(status="VERIFIED", basis=("replay:200",))
    )
    g_plain = graph.SurfaceGraph(nodes=(plain,), edges=())
    g_verified = graph.SurfaceGraph(nodes=(verified,), edges=())
    assert g_plain.serialize() == g_verified.serialize()
