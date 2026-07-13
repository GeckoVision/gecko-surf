"""FCC eval harness — deterministic scoring, proven with a MOCK agent (no live Haiku).

Covers the load-bearing definitions (``value_kind`` / ``args_match`` — the mint-vs-symbol
gotcha), the two arms' tool shaping (RAW exposes auth, GECKO hides it), the caller-guard
well-formedness check, and the full ``evaluate_fcc`` plumbing driven by a scripted fake LLM.
"""

from __future__ import annotations

from pathlib import Path

from gecko.access import Session, public_session
from gecko.client import AgentApiClient
from gecko.evaluate import GoldenTask
from gecko.fcc_eval import (
    RunRecord,
    args_match,
    evaluate_fcc,
    fcc_rate,
    gecko_tools,
    hallucination_rate,
    lift,
    per_archetype,
    pick,
    raw_tools,
    retrieval_recall_at_k,
    run_variance,
    score,
    value_kind,
)

FIX = Path(__file__).resolve().parent / "fixtures"
TXODDS = FIX / "txodds_docs.yaml"
PEGANA = FIX / "pegana_openapi.json"
MINT = "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"  # jitoSOL mint (base58, 44 chars)
SYMBOL = "jitoSOL"


# --- scripted fake LLM (the injected seam; Anthropic messages shape) -------------------


class _Blk:
    def __init__(self, type: str, name=None, input=None) -> None:
        self.type = type
        self.name = name
        self.input = input


class _Resp:
    def __init__(self, content, stop_reason) -> None:
        self.content = content
        self.stop_reason = stop_reason


class _Messages:
    def __init__(self, policy) -> None:
        self._policy = policy
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        decision = self._policy(kwargs)
        if decision is None:
            return _Resp([], "end_turn")
        name, args = decision
        return _Resp([_Blk("tool_use", name=name, input=args)], "tool_use")


class FakeLLM:
    def __init__(self, policy) -> None:
        self.messages = _Messages(policy)


def _tool_names(kwargs) -> set[str]:
    return {t["name"] for t in kwargs["tools"]}


# --- value_kind ------------------------------------------------------------------------


def test_value_kind_distinguishes_mint_symbol_int():
    assert value_kind(MINT) == "mint"
    assert value_kind(SYMBOL) == "symbol"
    assert value_kind(18179550) == "int"
    assert value_kind("18179550") == "int"  # numeric string is an id, not a symbol
    assert value_kind(True) == "bool"  # bool before int (subclass trap)
    assert value_kind("") == "none"


# --- args_match: the disambiguation the golden-args harness is blind to ----------------


def test_args_match_mint_routed_as_symbol_is_false():
    # picks `symbol` for a mint value -> the gold `mint` key is absent -> wrong
    assert args_match({"mint": MINT}, {"symbol": MINT}) is False


def test_args_match_mint_routed_as_mint_is_true():
    assert args_match({"mint": MINT}, {"mint": MINT}) is True


def test_args_match_right_key_wrong_kind_is_false():
    # `mint` key but a symbol-shaped value -> kind mismatch -> wrong
    assert args_match({"mint": MINT}, {"mint": SYMBOL}) is False


def test_args_match_symbol_routed_as_mint_is_false():
    assert args_match({"symbol": SYMBOL}, {"mint": SYMBOL}) is False
    assert args_match({"symbol": SYMBOL}, {"symbol": SYMBOL}) is True


def test_args_match_int_id_kind():
    assert args_match({"fixtureId": 18179550}, {"fixtureId": 18179550}) is True
    assert args_match({"fixtureId": 18179550}, {"fixtureId": "18179550"}) is True
    assert args_match({"fixtureId": 18179550}, {"fixtureId": SYMBOL}) is False


def test_args_match_no_gold_params_is_vacuously_true():
    assert args_match({}, {}) is True
    assert args_match({}, {"extra": 1}) is True  # extra agent args ignored
    assert args_match({"fixtureId": 1}, {}) is False  # required omitted


# --- the two arms: RAW exposes auth, GECKO hides it ------------------------------------


def _txodds_client() -> AgentApiClient:
    return AgentApiClient(str(TXODDS), session=Session(jwt="x", api_token="x"))


def test_raw_exposes_auth_params_gecko_hides_them():
    client = _txodds_client()
    raw = {t.name: t for t in raw_tools(client.operations)}
    snap = raw["getApiOddsSnapshotFixtureid"]
    props = snap.input_schema["properties"]
    required = snap.input_schema.get("required", [])
    # RAW: auth headers are present AND required (the naive dump carries them through).
    assert "Authorization" in props and "X-Api-Token" in props
    assert "Authorization" in required and "fixtureId" in required

    gk = {t.name: t for t in gecko_tools(client, "latest odds for a fixture", k=8)}
    snap_g = gk["getApiOddsSnapshotFixtureid"]
    gprops = snap_g.input_schema["properties"]
    # GECKO: auth headers hidden; the agent only reasons about fixtureId (+ optional asOf).
    assert "Authorization" not in gprops and "X-Api-Token" not in gprops
    assert "fixtureId" in gprops


# --- score(): well-formedness penalizes RAW's required auth; args_match integration ----


def _snapshot_task() -> GoldenTask:
    return GoldenTask(
        goal="get the latest live odds for a football fixture",
        expect_ops=("getApiOddsSnapshotFixtureid",),
        archetype="keyword_echo",
        args={"fixtureId": 18179550},
    )


def test_raw_missing_auth_is_not_wellformed_gecko_is():
    client = _txodds_client()
    task = _snapshot_task()
    raw = {t.name: t.caller() for t in raw_tools(client.operations)}
    gk = {t.name: t.caller() for t in gecko_tools(client, task.goal, k=8)}
    op = "getApiOddsSnapshotFixtureid"
    agent_args = {"fixtureId": 18179550}  # the agent supplies only the real input

    raw_score = score(task, op, agent_args, raw[op], client.base_url)
    gk_score = score(task, op, agent_args, gk[op], client.base_url)
    # RAW forces Authorization/X-Api-Token (marked required) -> build_request raises.
    assert raw_score.well_formed is False and raw_score.fcc is False
    # GECKO hid them -> fixtureId alone is a well-formed, first-call-correct request.
    assert gk_score.well_formed is True and gk_score.args_match is True
    assert gk_score.fcc is True


def test_score_catches_mint_symbol_gotcha_end_to_end():
    client = AgentApiClient(str(PEGANA), session=public_session())
    task = GoldenTask(
        goal="get peg state by mint address",
        expect_ops=("state_by_mint",),
        archetype="keyword_echo",
        args={"mint": MINT},
    )
    gk = {t.name: t.caller() for t in gecko_tools(client, task.goal, k=8)}
    assert "state_by_mint" in gk  # retrieval surfaced it
    # Correct routing: mint value into the `mint` path param -> FCC.
    good = score(
        task, "state_by_mint", {"mint": MINT}, gk["state_by_mint"], client.base_url
    )
    assert good.fcc is True
    # The gotcha a golden-args harness can't see: right op, wrong param family.
    bad = score(
        task, "state_by_mint", {"symbol": MINT}, gk["state_by_mint"], client.base_url
    )
    assert bad.tool_correct is True and bad.args_match is False and bad.fcc is False


def test_out_of_scope_correct_iff_declined():
    task = GoldenTask(goal="water my plants", expect_ops=(), archetype="out_of_scope")
    assert score(task, None, {}, None, "").fcc is True  # declined -> correct
    assert score(task, "list_assets", {}, None, "").fcc is False  # called -> wrong


# --- pick(): reads one tool_use turn; declines cleanly --------------------------------


def test_pick_reads_tool_use_and_declines():
    picked, args = pick(
        FakeLLM(lambda kw: ("state", {"symbol": SYMBOL})),
        model="m",
        tools=[{"name": "state", "description": "", "input_schema": {}}],
        prompt="p",
    )
    assert picked == "state" and args == {"symbol": SYMBOL}
    none_picked, none_args = pick(
        FakeLLM(lambda kw: None), model="m", tools=[], prompt="p"
    )
    assert none_picked is None and none_args == {}


# --- evaluate_fcc plumbing + aggregation (scripted agent, no network) ------------------


def test_evaluate_fcc_plumbing_and_lift():
    client = _txodds_client()
    tasks = [
        _snapshot_task(),
        GoldenTask(goal="water my plants", expect_ops=(), archetype="out_of_scope"),
    ]

    def props_of(kw, tool):
        for t in kw["tools"]:
            if t["name"] == tool:
                return t["input_schema"].get("properties", {})
        return {}

    def policy(kw):
        # Out-of-scope prompt: decline in both arms.
        if "water" in kw["messages"][0]["content"]:
            return None
        # Positive: pick the snapshot op. Fill auth iff the arm exposed it (RAW does),
        # so RAW is well-formed too here — isolating the aggregation plumbing.
        args = {"fixtureId": 18179550}
        if "Authorization" in props_of(kw, "getApiOddsSnapshotFixtureid"):
            args |= {"Authorization": "Bearer x", "X-Api-Token": "y"}
        return "getApiOddsSnapshotFixtureid", args

    records = evaluate_fcc(
        "txodds", client, tasks, FakeLLM(policy), model="m", k=8, n_runs=3
    )
    # 2 tasks * 2 arms * 3 runs
    assert len(records) == 2 * 2 * 3
    # Both arms get the positive task first-call-correct here -> equal rate, zero lift.
    assert fcc_rate(records, "raw") == 1.0
    assert fcc_rate(records, "gecko") == 1.0
    assert lift(records) == 0.0
    mean, stdev = run_variance(records, "gecko")
    assert mean == 1.0 and stdev == 0.0
    arch = per_archetype(records, "gecko")
    assert arch["keyword_echo"] == 1.0 and arch["out_of_scope"] == 1.0


# --- Phase 0.1: hallucination (picked ∉ presented tool names) --------------------------


def _rec(**kw) -> RunRecord:
    """Minimal RunRecord builder for aggregation tests — booleans + names only."""
    base = dict(
        fixture="f",
        archetype="keyword_echo",
        goal="g",
        arm="gecko",
        run=0,
        picked=None,
        retrieval_hit=False,
        tool_correct=False,
        well_formed=False,
        args_match=False,
        fcc=False,
    )
    base.update(kw)
    return RunRecord(**base)  # type: ignore[arg-type]


def test_score_flags_pick_not_presented_as_hallucination():
    task = _snapshot_task()  # expects getApiOddsSnapshotFixtureid
    presented = {"getApiOddsSnapshotFixtureid", "listFixtures"}
    # An invented tool name the arm never offered -> hallucination.
    s = score(task, "totally_made_up", {}, None, "", presented=presented)
    assert s.hallucinated is True


def test_score_real_but_wrong_pick_is_not_hallucination():
    task = _snapshot_task()
    presented = {"getApiOddsSnapshotFixtureid", "listFixtures"}
    # A presented-but-wrong tool: real-but-wrong is NOT a hallucination.
    s = score(task, "listFixtures", {}, None, "", presented=presented)
    assert s.tool_correct is False and s.hallucinated is False


def test_score_decline_is_not_hallucination():
    task = _snapshot_task()
    s = score(task, None, {}, None, "", presented={"getApiOddsSnapshotFixtureid"})
    assert s.hallucinated is False


def test_hallucination_rate_over_arm():
    recs = [
        _rec(arm="raw", picked="nope", hallucinated=True),
        _rec(arm="raw", picked="listFixtures", hallucinated=False),
        _rec(arm="gecko", picked="listFixtures", hallucinated=False),
    ]
    assert hallucination_rate(recs, "raw") == 0.5
    assert hallucination_rate(recs, "gecko") == 0.0


# --- Phase 0.2: retrieval ceiling (recall@k over surfaced top-k) -----------------------


def test_retrieval_recall_at_k_over_arm():
    recs = [
        _rec(arm="gecko", goal="a", run=0, retrieval_hit=True),
        _rec(arm="gecko", goal="a", run=1, retrieval_hit=True),  # same task -> dedup
        _rec(arm="gecko", goal="b", run=0, retrieval_hit=False),
    ]
    # 1 of 2 distinct tasks had its gold op surfaced in the top-k.
    assert retrieval_recall_at_k(recs, "gecko") == 0.5


def test_evaluate_fcc_shows_gecko_lift_when_raw_omits_auth():
    client = _txodds_client()
    tasks = [_snapshot_task()]

    def policy(kw):
        # A REALISTIC agent supplies only the real input (fixtureId), never inventing an
        # auth token. RAW then fails well-formedness (auth required); GECKO succeeds.
        return "getApiOddsSnapshotFixtureid", {"fixtureId": 18179550}

    records = evaluate_fcc(
        "txodds", client, tasks, FakeLLM(policy), model="m", k=8, n_runs=2
    )
    assert fcc_rate(records, "raw") == 0.0
    assert fcc_rate(records, "gecko") == 1.0
    assert lift(records) == 1.0
