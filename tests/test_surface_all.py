"""Below-scale "surface ALL usable tools" rule — Gecko must be strictly >= the raw
OpenAPI dump on small/clean APIs.

The lexical catalog structurally CANNOT surface a zero-overlap paraphrase op: when any op
genuinely matches, ``Catalog.search_scored`` returns only the score>0 matches and drops
every score-0 op — so bumping ``limit`` never recovers a paraphrase the query shares no
token with (verified: pegana ``current`` is absent even at limit=30). Top-k retrieval on a
small surface therefore HURTS first-call-correct vs "dump all ops."

Fix: below a scale threshold, the agent-facing ``search`` returns EVERY usable tool (no
truncation), so a zero-overlap-paraphrase op is always visible and pickable — exactly like
the raw dump. Above the threshold, top-k retrieval stays on. The retrieval-eval substrate
``search_scored`` is intentionally left as the pure ranker (this suite does not touch it).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gecko.access import Session, public_session
from gecko.client import AgentApiClient
from gecko.evaluate import load_golden
from gecko.fcc_eval import evaluate_fcc, fcc_rate, lift
from gecko.scale import SURFACE_ALL_MAX_OPS, should_surface_all

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden"


def _pegana() -> AgentApiClient:
    return AgentApiClient(
        str(FIXTURES / "pegana_openapi.json"), session=public_session()
    )


def _txodds() -> AgentApiClient:
    return AgentApiClient(
        str(FIXTURES / "txodds_docs.yaml"),
        session=Session(jwt="recorded-mode", api_token="recorded-mode"),
    )


# The specific zero-overlap ops top-k drops today (verified None-at-any-k in the lexical
# baseline: private/2026-07-01-lexical-baseline.md — rank None->None even after the 0/97 fix).
_DROPPED_TODAY = {
    "pegana": ("which formula revision is live for computing peg", "current"),
    "txodds": (
        "push me betting prices continuously as they change without polling",
        "getApiOddsStream",
    ),
}

_CASES = [("pegana", _pegana), ("txodds", _txodds)]


@pytest.mark.parametrize("name,client_factory", _CASES)
def test_small_surface_search_returns_all_usable_tools(name, client_factory) -> None:
    """On a below-scale surface, ``search`` returns EVERY usable tool regardless of query or
    limit — no top-k truncation."""
    client = client_factory()
    usable = {t["name"] for t in client.list_tools()}
    assert len(usable) <= SURFACE_ALL_MAX_OPS
    surfaced = {h["name"] for h in client.search("anything at all", limit=5)}
    assert surfaced == usable, (
        f"{name}: below-scale search must surface all {len(usable)} usable tools, "
        f"missing {usable - surfaced}"
    )


@pytest.mark.parametrize("name,client_factory", _CASES)
def test_zero_overlap_paraphrase_op_now_surfaced(name, client_factory) -> None:
    """The exact op top-k drops today (None at any k) is now surfaced for its paraphrase
    query — the dropped->surfaced proof, at the default limit=5."""
    client = client_factory()
    query, op = _DROPPED_TODAY[name]
    surfaced = {h["name"] for h in client.search(query, limit=5)}
    assert op in surfaced, f"{name}: {op!r} still dropped for {query!r}"


@pytest.mark.parametrize("name,client_factory", _CASES)
def test_golden_positive_tasks_recall_is_one(name, client_factory) -> None:
    """Every positive golden task's expected op is surfaced on the small fixtures — full
    recall (all ops shown), in particular the paraphrase_no_overlap archetype."""
    client = client_factory()
    for t in load_golden(GOLDEN / f"{name}_tasks.jsonl"):
        if not t.expect_ops:  # out-of-scope: not a recall task
            continue
        surfaced = {h["name"] for h in client.search(t.goal, limit=5)}
        assert set(t.expect_ops) & surfaced, (
            f"{name}/{t.archetype}: none of {t.expect_ops} surfaced for {t.goal!r}"
        )


@pytest.mark.parametrize("name,client_factory", _CASES)
def test_search_dict_contract_intact(name, client_factory) -> None:
    """Surface-all keeps the frozen agent-facing search shape."""
    client = client_factory()
    for hit in client.search("live odds peg", limit=5):
        assert set(hit) == {"name", "summary", "path", "method"}


@pytest.mark.parametrize("name,client_factory", _CASES)
def test_surface_all_keeps_genuine_hits_ranked_first(name, client_factory) -> None:
    """Below-scale does not throw away relevance: a genuine keyword hit still ranks at the
    top; the previously-dropped ops are APPENDED, not interleaved ahead of real matches."""
    client = client_factory()
    query, expected_top = (
        ("get peg state by mint address", "state_by_mint")
        if name == "pegana"
        else (
            "get the latest live odds for a football fixture",
            "getApiOddsSnapshotFixtureid",
        )
    )
    hits = client.search(query, limit=5)
    assert hits[0]["name"] == expected_top


# --- FCC-arm parity: the metric that regressed (GECKO 1.00 -> 0.70 on small Pegana) ----
#
# The recall tests above prove the agent-facing SEARCH surfaces the dropped ops. This suite
# closes the loop the eval actually reports on: that the FCC EVAL ARM (``gecko_tools`` ->
# scored pick) no longer LOWERS first-call-correct vs the raw all-ops dump on a small surface.
# ``test_surface_all`` (search recall) and ``test_fcc_eval`` (scoring plumbing) each covered
# half; nothing pinned the number the context-engineering finding moved — this does.


class _Blk:
    def __init__(self, type: str, name: str | None = None, input: dict | None = None):
        self.type, self.name, self.input = type, name, input


class _Resp:
    def __init__(self, content: list) -> None:
        self.content = content


class _OracleMessages:
    """A perfect picker: on a positive task it calls the first EXPECTED op the arm actually
    presents (supplying the gold args); if the arm never presents it — the pre-fix top-k
    drop — it declines, which scores the task 0. So the arm's FCC is gated purely on whether
    surface-all put the right tool in front of the model."""

    def __init__(self, goal_map: dict) -> None:
        self._goal_map = goal_map

    def create(self, **kwargs):
        goal = kwargs["messages"][0]["content"].split("\n")[0]
        task = self._goal_map[goal]
        if not task.expect_ops:  # out-of-scope: the correct move is to decline
            return _Resp([])
        presented = {t["name"] for t in kwargs["tools"]}
        for op in task.expect_ops:
            if op in presented:
                return _Resp([_Blk("tool_use", op, dict(task.args))])
        return _Resp([])  # expected op was truncated away -> forced miss


class _OracleLLM:
    def __init__(self, goal_map: dict) -> None:
        self.messages = _OracleMessages(goal_map)


def test_fcc_eval_arm_never_lowers_fcc_on_small_surface() -> None:
    """Regression guard for the exact reported drop (GECKO 1.00 -> 0.70 on clean Pegana):
    with a perfect picker, the GECKO eval arm must reach FULL FCC and NOT fall below the raw
    all-ops dump — compression that never lowers first-call-correct. Disabling surface-all
    reintroduces the top-k truncation and this test fails (measured: gecko 0.70, lift -0.30)."""
    client = AgentApiClient(
        str(FIXTURES / "pegana_openapi.json"), session=public_session()
    )
    tasks = load_golden(GOLDEN / "pegana_tasks.jsonl")
    llm = _OracleLLM({t.goal: t for t in tasks})
    records = evaluate_fcc("pegana", client, tasks, llm, model="m", k=8, n_runs=2)
    assert fcc_rate(records, "gecko") == 1.0, "surface-all must restore full FCC parity"
    assert fcc_rate(records, "raw") == 1.0
    assert lift(records) == 0.0, (
        "GECKO must never score below the raw all-ops dump here"
    )


def test_fcc_eval_arm_regression_is_falsifiable() -> None:
    """The above is a real guard, not a tautology: force the pre-fix top-k behavior (no
    surface-all) and the GECKO arm drops below the raw dump — reproducing the finding."""
    client = AgentApiClient(
        str(FIXTURES / "pegana_openapi.json"), session=public_session()
    )
    client._surface_all = False  # simulate the pre-fix small-surface truncation
    tasks = load_golden(GOLDEN / "pegana_tasks.jsonl")
    llm = _OracleLLM({t.goal: t for t in tasks})
    records = evaluate_fcc("pegana", client, tasks, llm, model="m", k=8, n_runs=2)
    assert fcc_rate(records, "gecko") < fcc_rate(records, "raw")
    assert lift(records) < 0.0


def _synthetic_spec(n_ops: int) -> dict:
    """A minimal, auth-free OpenAPI spec with ``n_ops`` GET operations (all usable)."""
    paths: dict[str, dict] = {}
    for i in range(n_ops):
        paths[f"/thing/{i}"] = {
            "get": {
                "operationId": f"getThing{i}",
                "summary": f"Get thing number {i}",
                "responses": {"200": {"description": "ok"}},
            }
        }
    return {
        "openapi": "3.0.0",
        "info": {"title": "synthetic", "version": "1"},
        "paths": paths,
    }


def test_large_surface_still_truncates() -> None:
    """Above the threshold, top-k retrieval stays on: search truncates to ``limit`` and the
    surface-all rule is off."""
    client = AgentApiClient(_synthetic_spec(SURFACE_ALL_MAX_OPS + 15))
    assert len(client.list_tools()) > SURFACE_ALL_MAX_OPS
    assert should_surface_all(client.list_tools()) is False
    hits = client.search("get thing number 7", limit=5)
    assert len(hits) == 5, "large surface must still truncate to limit"


def test_threshold_boundary() -> None:
    """At exactly the op-count threshold the surface is shown in full; one over, it isn't."""
    at = AgentApiClient(_synthetic_spec(SURFACE_ALL_MAX_OPS))
    over = AgentApiClient(_synthetic_spec(SURFACE_ALL_MAX_OPS + 1))
    assert should_surface_all(at.list_tools()) is True
    assert should_surface_all(over.list_tools()) is False
    assert len(at.search("get thing number 3", limit=5)) == len(at.list_tools())
