"""What `disputed_recipe` did NOT make order-independent.

``9d993e9`` claims the flagged node it builds is "canonical in its two inputs so the
reversed-order graph is byte-identical", and proves it by reversing the IDL's
instruction list for ORE's `miner` and `automation`. That proof holds for those two —
and for exactly the shape they have: TWO disagreeing declarations, both resolvable.

Reversing the instruction list of every captured IDL and diffing the program-wide map
node for node finds two names where it still does not hold. Neither derives a wrong
address — both are flagged either way — but "order is not evidence" was the whole
claim, and for these two the program's answer still depends on the order it was read
in.

  pump.fun `associated_bonding_curve`   3 disagreeing recipes, not 2. The chain folds
        them pairwise, and once a dispute is in the map it is unresolvable, so the
        THIRD declaration falls off the end of the same branch chain and is dropped.
        Which two of the three get named in the reason is decided by IDL order. This
        node was order-STABLE before the fix and is order-unstable after it.

  ORE `round`                           two FLAGGED recipes (`miner.round_id` vs
        `board.round_id`). The new branch requires both sides resolvable, so this pair
        still falls off the end of the chain and the map keeps whichever came first.
        ``test_two_flagged_recipes_are_not_a_conflict`` asserts only that the reason
        mentions `round_id`, which is true of both orders, so it passes without
        touching the property the commit is about.

Both are pinned as strict xfail: they are the residual, and the day the fold becomes
n-ary and the flagged-vs-flagged pair stops falling through, these turn red as XPASS
and must be un-pinned rather than quietly kept.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gecko.pda_extract import from_anchor_idl, instruction_pdas

FIXTURES = Path(__file__).parent / "fixtures" / "orquestra"
ORE = "6alwvs9936laepljczqumb"
PUMPFUN = "6i6q26bmm46b89xlxo1kv"


def _idl(slug: str) -> dict:
    return json.loads((FIXTURES / slug / "idl.json").read_text(encoding="utf-8"))["idl"]


@pytest.fixture(scope="module")
def ore_idl() -> dict:
    return _idl(ORE)


@pytest.fixture(scope="module")
def pumpfun_idl() -> dict:
    return _idl(PUMPFUN)


def _reversed_instructions(idl: dict) -> dict:
    return {**idl, "instructions": list(reversed(idl["instructions"]))}


def _distinct_declarations(idl: dict, account: str) -> list[tuple]:
    """Every distinct seed tuple the IDL declares for one account name.

    Read straight from the instruction list rather than through the map under test,
    so the count that makes these cases interesting is established independently.
    """
    seen: dict[tuple, None] = {}
    for ix in idl.get("instructions", []):
        node = instruction_pdas(ix, program_id=None, type_defs={}).get(account)
        if node is not None:
            seen.setdefault(node.seeds, None)
    return list(seen)


# -- the arms can diverge: these names really are declared 3 / 2 ways --------


def test_associated_bonding_curve_really_is_declared_three_ways(
    pumpfun_idl: dict,
) -> None:
    """The precondition for the fold being n-ary rather than pairwise."""
    assert len(_distinct_declarations(pumpfun_idl, "associated_bonding_curve")) == 3


def test_round_really_is_declared_two_flagged_ways(ore_idl: dict) -> None:
    """The precondition for the flagged-vs-flagged pair: two recipes, neither derives."""
    declarations = _distinct_declarations(ore_idl, "round")
    assert len(declarations) == 2
    node = from_anchor_idl(ore_idl)["round"]
    assert not node.resolvable


# -- the residual ------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="disputed_recipe folds pairwise; the 3rd disagreeing recipe falls off the "
    "same branch chain, so which two the reason names is still IDL order",
)
def test_a_three_way_dispute_is_canonical_too(pumpfun_idl: dict) -> None:
    account = "associated_bonding_curve"
    forward = from_anchor_idl(pumpfun_idl)[account]
    backward = from_anchor_idl(_reversed_instructions(pumpfun_idl))[account]
    assert forward == backward, (
        f"`{account}` is declared three different ways and the flagged node names "
        "whichever two the IDL listed first"
    )


@pytest.mark.xfail(
    strict=True,
    reason="the new branch requires BOTH sides resolvable, so two flagged recipes "
    "still fall off the end of the chain and the first one wins",
)
def test_two_flagged_recipes_are_also_order_independent(ore_idl: dict) -> None:
    forward = from_anchor_idl(ore_idl)["round"]
    backward = from_anchor_idl(_reversed_instructions(ore_idl))["round"]
    assert forward == backward, (
        "`round` is declared on `miner.round_id` and on `board.round_id`; the map "
        "keeps whichever came first, so the gap it reports names the wrong account"
    )
