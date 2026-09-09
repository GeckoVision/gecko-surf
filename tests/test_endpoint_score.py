"""One score per endpoint — what it measures, and the three ways it could lie.

The cases here are the ones that actually bit during construction, not invented ones.
"""

from __future__ import annotations

import gecko.find_start as fs
from gecko.endpoint_score import EndpointScore, Component, score_endpoints
from gecko.pda import ConstantPdaSeedNode, PdaNode, ResolverPdaSeedNode


def _pda(name: str = "acct", *, resolvable: bool) -> PdaNode:
    seeds: tuple = (ConstantPdaSeedNode(b"seed", "utf8"),)
    if not resolvable:
        seeds = (*seeds, ResolverPdaSeedNode("needs_a_human"))
    return PdaNode(
        name=name,
        seeds=seeds,
        program_id="Prog1111111111111111111111111111111111111",
    )


def _card(api_id, instruction, text, *, pdas=None, claims=()):
    return fs._Card(
        kind="start" if instruction else "surface",
        api_id=api_id,
        program_id="Prog1111111111111111111111111111111111111",
        instruction=instruction,
        intent_name=instruction,
        inputs=(),
        accounts=(),
        pdas=dict(pdas or {}),
        spec=fs.StartSpec(accounts=tuple(claims), recovered={}),
        notes=text,
        execute_url=None,
        operation=fs._operation(
            operation_id=f"{api_id}.{instruction}" if instruction else api_id,
            path=f"/{api_id}/{instruction or ''}",
            summary=fs._first_sentence(text),
            description=text,
            tags=[api_id],
        ),
        wired_intents=(),
        origins={},
        trust="external",
    )


def test_callable_is_measured_against_the_intents_own_accounts() -> None:
    """The regression this module shipped with for ten minutes.

    A card carries every PDA its PROGRAM declares. Scoring an instruction against all of
    them read `whirlpool.swap_v2` as BLOCKED on `bundled_position` and
    `reward_token_badge` — position bundling and reward distribution, which a swap never
    touches. That endpoint landed real mainnet swaps on 2026-09-08. A scorecard whose
    first act is to call our best-proven endpoint broken is measuring the wrong set.
    """
    card = _card(
        "prog",
        "swap",
        "Swap one token for another against a pool.",
        pdas={
            "pool": _pda("pool", resolvable=True),
            "rewards_vault": _pda(
                "rewards_vault", resolvable=False
            ),  # a DIFFERENT instruction's account
        },
        claims=("pool",),
    )
    (score,) = score_endpoints([card, _card("other", "unrelated", "Something else.")])[
        :1
    ]
    assert score.component("callable").verdict == "ok"
    assert score.component("callable").measured["accounts"] == 1, "only what it claims"


def test_callable_blocks_when_the_intents_own_account_cannot_be_derived() -> None:
    """pumpfun.buy, measured: it declares `creator_vault`, whose seed nothing can bind."""
    card = _card(
        "prog",
        "buy",
        "Buy a token from the bonding curve.",
        pdas={
            "curve": _pda("curve", resolvable=True),
            "creator_vault": _pda("creator_vault", resolvable=False),
        },
        claims=("curve", "creator_vault"),
    )
    score = score_endpoints([card, _card("other", "x", "Something else.")])[0]
    assert score.component("callable").verdict == "blocked"
    assert "creator_vault" in score.component("callable").reason
    assert score.rankable is False, (
        "an agent must not be offered a call we cannot derive"
    )


def test_a_surface_card_with_an_underivable_account_is_weak_not_blocked() -> None:
    """A gap somewhere in a program is a coverage gap. An instruction that needs none of
    those accounts is unaffected, and calling the whole program broken would say
    otherwise."""
    card = _card(
        "prog",
        None,
        "A program that does several things.",
        pdas={"a": _pda("a", resolvable=True), "b": _pda("b", resolvable=False)},
    )
    score = score_endpoints([card, _card("other", "x", "Something else.")])[0]
    assert score.component("callable").verdict == "weak"


def test_distinct_catches_siblings_whose_words_do_not_separate_them() -> None:
    """The case this component exists for, from a real program.

    The Swap Orchestrator (`DF1ow4ts…`, 3.9M transactions in 7 days) ships six swap
    variants whose descriptions differ by one clause: a slippage fee, a destination
    account, native SOL. Measured 2026-09-09 against its live surface, five of the six
    scored margin 0 — their own words do not separate them from a sibling, so an agent
    asking to swap picks by sort order rather than by evidence.
    """
    siblings = [
        _card("orch", "swap", "Executes a token swap"),
        _card("orch", "swap2", "Executes a token swap with a slippage fee"),
        _card(
            "orch",
            "swap_native",
            "Executes a token swap. Outputs native SOL by unwrapping WSOL.",
        ),
    ]
    scores = {s.instruction: s for s in score_endpoints(siblings)}
    assert scores["swap"].component("distinct").verdict == "weak"
    assert scores["swap"].component("distinct").measured["margin"] <= 0
    # The one carrying vocabulary the others lack is separable, and must not be flagged.
    assert scores["swap_native"].component("distinct").verdict == "ok"


def test_a_lone_card_is_unknown_not_distinct() -> None:
    """Nothing to be confused with is not the same as being distinct, and a scorecard
    that reported `ok` there would be counting an absence as a pass."""
    score = score_endpoints([_card("solo", "only", "The only thing here.")])[0]
    assert score.component("distinct").verdict == "unknown"


def test_the_verdict_is_the_worst_component_never_a_blend() -> None:
    """Two ok components must not average away a blocked one."""
    score = EndpointScore(
        "p",
        "i",
        (
            Component("callable", "blocked", "", {}),
            Component("findable", "ok", "", {}),
            Component("distinct", "ok", "", {}),
        ),
    )
    assert score.verdict == "blocked"
    assert score.rankable is False


def test_unknown_does_not_read_as_a_middling_result() -> None:
    """`unknown` is the absence of a measurement. Ordered between weak and ok it would
    let "we did not look" outrank a real weakness."""
    score = EndpointScore(
        "p",
        "i",
        (
            Component("callable", "unknown", "", {}),
            Component("findable", "weak", "", {}),
        ),
    )
    assert score.verdict == "weak", "a real weakness outranks an unmeasured question"
    all_unknown = EndpointScore("p", "i", (Component("callable", "unknown", "", {}),))
    assert all_unknown.verdict == "unknown"


def test_the_live_catalog_scores_and_the_proven_endpoints_are_ok() -> None:
    """The end-to-end tripwire. `whirlpool.swap_v2` and `let_me_buy.make_purchase` both
    landed real mainnet transactions on 2026-09-08; if either ever scores below `ok` on
    `callable`, this module is measuring the wrong thing again."""
    scores = {s.label: s for s in score_endpoints()}
    assert scores, "the wired catalog produced no scores"
    for proven in ("whirlpool.swap_v2", "let_me_buy.make_purchase"):
        assert scores[proven].component("callable").verdict == "ok", (
            f"{proven} landed on mainnet; a scorecard calling it uncallable is wrong"
        )
