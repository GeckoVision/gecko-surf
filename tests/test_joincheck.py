"""Cross-domain join precision — a measurement of whether derived joins are RIGHT.

`graph-engineer`'s half of the scoring question. It needs no human labels, because a
wrong join is provable from the provider's own text: Birdeye names seven parameter
components `address`, and their descriptions disagree ("the token contract" vs "a
trader" vs "a pair contract"). A join across two of those is wrong in the worst way
available — it type-checks, validates, executes, and returns a plausible number.

The headline result these tests pin: the shipped genericity demotion catches **every**
cross-domain join on Birdeye, and confirming `address` surface-wide would make sixteen
of them plannable. That is the security verdict, restated as a number.
"""

from __future__ import annotations

from pathlib import Path

from gecko.client import AgentApiClient
from gecko.joincheck import (
    cross_domain_joins,
    domain_of,
    incompatible,
    precision,
)
from gecko.surface import Surface

REPO = Path(__file__).resolve().parents[1]
BIRDEYE = REPO / "examples/birdeye_demo/spec/birdeye_openapi.json"


def _graph_and_descriptions(hints: dict[str, str] | None = None):
    client = AgentApiClient(str(BIRDEYE), surface_id="birdeye", declared_hints=hints)
    graph = Surface.of(client).graph
    ops = {o.operation_id: o for o in client.operations}
    described: dict[str, str] = {}
    for node in graph.nodes:
        op = ops.get(node.owner)
        if op is None:
            continue
        if node.kind == "param":
            for p in op.parameters:
                if p.name == node.name:
                    described[node.id] = p.description or ""
        elif node.kind == "field":
            for f in op.response_fields or []:
                if f.name == node.name:
                    described[node.id] = getattr(f, "description", "") or ""
    return graph, described


def test_domain_is_none_unless_the_text_settles_it() -> None:
    """`None` is the common and correct answer. Guessing a domain manufactures
    contradictions out of prose, and a false positive here would invite someone to
    loosen a real control to silence it."""
    assert domain_of("The address of the token contract.") == "token"
    assert domain_of("The address of a trader.") == "actor"
    assert domain_of("The address of a pair contract") == "pair"
    assert domain_of(None) is None
    assert domain_of("") is None
    assert domain_of("An identifier.") is None, "names no domain"
    assert domain_of("the wallet that owns the token contract") is None, (
        "two domains in one sentence settles nothing — ambiguity must not coin-flip"
    )


def test_incompatible_requires_both_sides_known() -> None:
    """Absence of a rule is not evidence of compatibility, and one unknown side is not a
    contradiction. This table may only ever ADD suspicion."""
    assert incompatible("token", "actor")
    assert incompatible("actor", "token"), "symmetric"
    assert not incompatible("token", None)
    assert not incompatible(None, None)
    assert not incompatible("token", "token")


def test_shipped_demotion_catches_every_cross_domain_join() -> None:
    """THE RESULT. Birdeye carries cross-domain joins in its raw edges, and **none**
    reaches the plannable set.

    This is the genericity demotion doing exactly the job it was built for, measured
    rather than asserted. If this ever returns non-zero, a provably-wrong join has become
    plannable and the demotion has a hole.
    """
    graph, described = _graph_and_descriptions()
    raw = cross_domain_joins(graph, described)
    assert raw, (
        "birdeye must exercise this check — its `address` collides across domains"
    )

    plannable_suspects = [
        e
        for e in graph.feeds_edges()
        if incompatible(
            domain_of(described.get(e.src)), domain_of(described.get(e.dst))
        )
    ]
    assert plannable_suspects == [], (
        f"{len(plannable_suspects)} provably-wrong join(s) are PLANNABLE — the "
        "genericity demotion has a hole"
    )


def test_confirming_a_colliding_name_makes_wrong_joins_plannable() -> None:
    """The security verdict, as a number.

    Confirming `address=solana-token-mint` surface-wide — the "obvious fix" for a
    derivation that refuses everything — promotes cross-domain joins into the plannable
    set. A trader's wallet becomes a valid input to a token-price call.

    This test exists so that if anyone proposes surface-wide confirmation again, the cost
    is a failing test rather than an argument.
    """
    graph, described = _graph_and_descriptions({"address": "solana-token-mint"})
    plannable_suspects = [
        e
        for e in graph.feeds_edges()
        if incompatible(
            domain_of(described.get(e.src)), domain_of(described.get(e.dst))
        )
    ]
    assert plannable_suspects, (
        "confirming a colliding name is expected to promote wrong joins; if this now "
        "passes cleanly, either the confirmation path or this check changed — find out "
        "which before celebrating"
    )


def test_precision_reports_its_sample_size() -> None:
    """The two integers travel together so nobody can quote the ratio without n.

    An edge whose sides name no domain is not evidence either way; counting it as correct
    would inflate the number. This is precision on the judgeable subset, and it says so.
    """
    graph, described = _graph_and_descriptions()
    suspects, considered = precision(graph, described)
    assert considered > 0
    assert 0 <= suspects <= considered
