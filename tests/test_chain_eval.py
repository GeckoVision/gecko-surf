"""Chain-FCC harness tests (§6, §12 Phase 1) — the keystone: PROVE a whole
``graph.plan()`` is first-plan-correct in recorded mode, $0, no live calls.

Strong tests: the two known TxLINE chains must score first-plan-correct end-to-end
(fixtures/snapshot -> odds via FixtureId; scores -> stat-validation via seq), and a
linked-but-type-mismatched chain must score FAIL (not a false pass).
"""

from __future__ import annotations

from pathlib import Path

from gecko.chain_eval import evaluate_chain, kind_matches_type
from gecko.client import AgentApiClient
from gecko.graph import build_graph, plan
from gecko.ingest import extract_operations, load_spec

FIX = Path(__file__).resolve().parent / "fixtures"
TXLINE = FIX / "txline_openapi.yaml"
TYPE_MISMATCH = FIX / "chain_typemismatch.json"


def _graph_and_client(path: Path) -> tuple[object, AgentApiClient]:
    g = build_graph(extract_operations(load_spec(str(path))))
    return g, AgentApiClient(str(path))


# --- the two known TxLINE chains score first-plan-correct -----------------------
def test_txline_chain1_fixtures_to_odds_is_first_plan_correct() -> None:
    """fixtures/snapshot --FixtureId--> odds/updates: the agent supplies nothing,
    the planner sources fixtureId, and the recorded chain threads it correctly."""
    g, client = _graph_and_client(TXLINE)
    p = plan(g, "getApiOddsUpdatesFixtureid", set())
    assert p is not None
    result = evaluate_chain(client, p)
    assert result.first_plan_correct, result.reason
    assert result.all_well_formed
    assert len(result.steps) == 2
    assert [s.operation_id for s in result.steps] == [
        "getApiFixturesSnapshot",
        "getApiOddsUpdatesFixtureid",
    ]
    # exactly one threaded join key: FixtureId -> fixtureId, found + kind-correct.
    assert len(result.threads) == 1
    thread = result.threads[0]
    assert thread.param == "fixtureId"
    assert thread.source_field == "FixtureId"
    assert thread.found and thread.kind_ok
    assert thread.value_kind == "int" and thread.param_type == "integer"


def test_txline_chain2_scores_to_stat_validation_is_first_plan_correct() -> None:
    """scores --seq--> stat-validation: the agent supplies fixtureId + statKey, the
    planner sources the non-id flow key seq, and the recorded chain threads it."""
    g, client = _graph_and_client(TXLINE)
    p = plan(g, "getApiScoresStat-validation", {"fixtureId", "statKey", "statKey2"})
    assert p is not None
    result = evaluate_chain(client, p, seed_args={"fixtureId": 12345, "statKey": 1})
    assert result.first_plan_correct, result.reason
    assert result.all_well_formed
    # the last step is the intent op and it consumes all three inputs.
    assert result.steps[-1].operation_id == "getApiScoresStat-validation"
    seq_threads = [t for t in result.threads if t.param == "seq"]
    assert len(seq_threads) == 1
    thread = seq_threads[0]
    assert thread.source_field == "seq"
    assert thread.found and thread.kind_ok
    assert thread.value_kind == "int" and thread.param_type == "integer"


def test_chain2_fails_first_plan_correct_when_seed_arg_missing() -> None:
    """Honest measurement: without the agent-supplied fixtureId, the producer step
    (a path-param op) is not well-formed, so the chain is NOT first-plan-correct."""
    g, client = _graph_and_client(TXLINE)
    p = plan(g, "getApiScoresStat-validation", {"fixtureId", "statKey", "statKey2"})
    assert p is not None
    # seed only statKey — fixtureId (a required path param on the producer) is absent.
    result = evaluate_chain(client, p, seed_args={"statKey": 1})
    assert not result.first_plan_correct
    assert not result.all_well_formed
    assert "not well-formed" in result.reason


# --- negative: a linked-but-mismatched chain must score FAIL --------------------
def test_type_mismatched_chain_scores_fail_not_pass() -> None:
    """listOrders produces a STRING orderId; getOrder consumes an INTEGER orderId.
    The graph legitimately LINKS them (same entity, both id-shaped), so a plan forms
    — but threading a string into an integer id is not first-plan-correct. The
    harness must catch the type mismatch, not paper over it with a false pass."""
    g, client = _graph_and_client(TYPE_MISMATCH)
    p = plan(g, "getOrder", set())
    assert p is not None, "the string/integer orderId edge must still form a plan"
    result = evaluate_chain(client, p)
    # every step is well-formed (a string fills a path fine) — the failure is TYPE.
    assert result.all_well_formed
    assert not result.first_plan_correct
    assert len(result.threads) == 1
    thread = result.threads[0]
    assert thread.found  # the value WAS threaded...
    assert not thread.kind_ok  # ...but it is the wrong kind for the consuming param.
    assert thread.value_kind == "symbol" and thread.param_type == "integer"
    assert "does not match" in result.reason


def test_missing_source_field_is_reported_not_papered_over() -> None:
    """If recorded synthesis cannot thread a value (the source_field is absent from
    the producer's response), that is a real finding: found=False, chain fails, and
    the reason names the missing field — never a silent pass."""
    g, client = _graph_and_client(TXLINE)
    p = plan(g, "getApiOddsUpdatesFixtureid", set())
    assert p is not None
    # Corrupt the plan's explain to point at a field the producer does NOT emit.
    import dataclasses

    broken_explain = tuple(
        dataclasses.replace(e, source_field="NoSuchField") for e in p.explain
    )
    broken = dataclasses.replace(p, explain=broken_explain)
    result = evaluate_chain(client, broken)
    assert not result.first_plan_correct
    assert len(result.threads) == 1
    assert not result.threads[0].found
    assert "not in" in result.reason


# --- the kind/type matcher (the value-kind-correct half of the score) -----------
def test_kind_matches_type_matrix() -> None:
    assert kind_matches_type("int", "integer")
    assert kind_matches_type("int", "number")
    assert kind_matches_type("float", "number")
    assert kind_matches_type("symbol", "string")
    assert kind_matches_type("int", "string")  # a numeric string is a valid string id
    assert kind_matches_type("bool", "boolean")
    # mismatches
    assert not kind_matches_type("symbol", "integer")
    assert not kind_matches_type("bool", "integer")
    assert not kind_matches_type("int", "boolean")
    assert not kind_matches_type("int", None)  # unknown type -> cannot confirm correct


# --- R2: the harness RECORDS the route it took; it never refuses -----------------


def test_collection_route_is_recorded_not_refused() -> None:
    """`getApiFixturesSnapshot` returns a top-level array, so the join key is reached
    through a collection — element 0 of N, chosen by sort order.

    Until now nothing said so, and a caller could not tell "the id" from "the id of
    whichever fixture sorted first". That is the recurring root cause: the
    not-evaluated case (WHICH one?) falling into the type's zero value (a bare value
    that looks singular).

    It must RECORD, not refuse. chain_eval is a measurement harness; list→detail is the
    most common honest REST chain, and refusing here would fail this very test and
    retract a published benchmark row. Making a measurement refuse destroys the
    measurement — the refusal belongs one layer up, where an executable artifact is
    emitted and a policy decision is appropriate.
    """
    g, client = _graph_and_client(TXLINE)
    p = plan(g, "getApiOddsUpdatesFixtureid", set())
    assert p is not None
    result = evaluate_chain(client, p)

    assert result.first_plan_correct, "the published chain must stay green"
    thread = result.threads[0]
    assert thread.arity == "many", "the array crossing must be recorded"
    assert thread.kind_ok, "recording arity must not change the verdict"
    assert thread.source_pointer, "the route must be reported"
    assert not any(seg.isdigit() for seg in thread.source_pointer.split("/")), (
        "the recorded pointer stays index-free — an index would be an observation, "
        "not a fact about the surface"
    )


def test_arity_is_unknown_when_the_field_was_never_found() -> None:
    """A miss reports `unknown`, never `one`. 'I did not find it' and 'I found exactly
    one' are different answers and must not share a representation."""
    from gecko.chain_eval import _find_field

    found, value, pointer, arity = _find_field({"a": 1}, "nope")
    assert (found, value, pointer, arity) == (False, None, "", "unknown")
