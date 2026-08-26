"""Two derivable recipes, one account name, and the map can only hold one.

``from_anchor_idl`` keeps ONE PDA recipe per account name for a whole program, and its
branch chain resolves every disagreement it recognises: a self-referential dead end
defers to its proven twin, an untyped seed defers to the typed spelling of the same
recipe, a resolvable declaration defers to a flagged one (pump.fun's `creator_vault`,
kept conservative on purpose). The chain had no ``else``. Two genuinely different but
both-resolvable recipes fell through it and the map kept whichever instruction the IDL
happened to list first — in a function whose own comment says "Order is not evidence."

MEASURED on the captured ORE IDL (``oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv``):
`automate` declares `miner` seeded on its `signer` account, `deploy` declares the same
name seeded on `authority`. Both derive cleanly; they are different addresses. The map
held `signer` for the whole program because `automate` is listed first — reverse the
instruction list and the program's recipe changed, which is the entire proof that
nothing but order decided it.

The answer is not to pick better. It is to say so: an account whose recipe two
instructions disagree about is not derivable from a name-keyed map, and the flagged node
carries the two operands in its reason so a caller can go and ask the instruction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gecko.pda import PdaNode, ResolverPdaSeedNode, UnresolvedSeedError, derive_pda
from gecko.pda_extract import from_anchor_idl

FIXTURES = Path(__file__).parent / "fixtures" / "orquestra"
ORE = "6alwvs9936laepljczqumb"
PUMPFUN = "6i6q26bmm46b89xlxo1kv"

SIGNER = "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi"
AUTHORITY = "6Dw1xBGXChPeS69hovvYMF2nmRxgdoA711TKuuAbN5rV"


def _idl(slug: str) -> dict:
    return json.loads((FIXTURES / slug / "idl.json").read_text(encoding="utf-8"))["idl"]


@pytest.fixture(scope="module")
def ore_idl() -> dict:
    return _idl(ORE)


@pytest.fixture(scope="module")
def pumpfun_idl() -> dict:
    return _idl(PUMPFUN)


def _reversed_instructions(idl: dict) -> dict:
    """The same program, declared bottom-up. Nothing about the PROGRAM changes."""
    return {**idl, "instructions": list(reversed(idl["instructions"]))}


def _reasons(node: PdaNode) -> str:
    return " ".join(s.reason for s in node.seeds if isinstance(s, ResolverPdaSeedNode))


# -- the arms can diverge ---------------------------------------------------


@pytest.mark.parametrize("account", ["miner", "automation"])
def test_the_fixture_really_does_declare_two_derivable_recipes(
    ore_idl: dict, account: str
) -> None:
    """Both recipes derive, and to DIFFERENT addresses — checked before anything else.

    Reads the two declarations straight out of the IDL text, so this arm does not
    depend on the function under test.
    """
    from gecko.pda_extract import instruction_pdas

    bound = {"signer": SIGNER, "authority": AUTHORITY}
    addresses = set()
    for name in ("automate", "deploy"):
        ix = next(i for i in ore_idl["instructions"] if i["name"] == name)
        node = instruction_pdas(ix, program_id=None, type_defs={})[account]
        assert node.resolvable
        addresses.add(
            derive_pda(
                PdaNode(
                    node.name, node.seeds, "oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv"
                ),
                bound,
            ).address
        )
    assert len(addresses) == 2


# -- order is not evidence --------------------------------------------------


@pytest.mark.parametrize("account", ["miner", "automation"])
def test_reversing_the_instruction_list_cannot_change_the_recipe(
    ore_idl: dict, account: str
) -> None:
    forward = from_anchor_idl(ore_idl)[account]
    backward = from_anchor_idl(_reversed_instructions(ore_idl))[account]
    assert forward == backward, (
        f"`{account}` is declared two different ways and the map kept whichever "
        "instruction came first — order decided a money-moving address"
    )


@pytest.mark.parametrize("account", ["miner", "automation"])
def test_a_disagreed_recipe_refuses_instead_of_deriving(
    ore_idl: dict, account: str
) -> None:
    node = from_anchor_idl(ore_idl)[account]
    assert not node.resolvable, "a recipe two instructions disagree about is not one"
    with pytest.raises(UnresolvedSeedError):
        derive_pda(
            PdaNode(
                node.name, node.seeds, "oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv"
            ),
            {"signer": SIGNER, "authority": AUTHORITY},
        )
    # legible: the two operands that disagree are named, so the caller knows what to
    # go and ask the instruction for
    reason = _reasons(node)
    assert "signer" in reason and "authority" in reason, reason


# -- the disagreements the chain already resolves, left alone ---------------


def test_the_conservative_keep_is_not_turned_into_a_conflict(
    pumpfun_idl: dict,
) -> None:
    """pump.fun `creator_vault` — nine instructions seed it on `bonding_curve.creator`
    (a flagged runtime read), `collect_creator_fee` on a plain `creator` account it
    happens to take. That is already decided, deliberately, in favour of the flagged
    recipe, and it must keep ITS reason rather than be relabelled a disagreement."""
    node = from_anchor_idl(pumpfun_idl)["creator_vault"]
    assert not node.resolvable
    assert "bonding_curve.creator" in _reasons(node)


def test_two_flagged_recipes_are_not_a_conflict(ore_idl: dict) -> None:
    """ORE `round`: `deploy` reads `board.round_id`, `checkpoint` reads
    `miner.round_id`. Neither derives, so nothing was overstated and the existing
    honest gap is the answer."""
    node = from_anchor_idl(ore_idl)["round"]
    assert not node.resolvable
    assert "round_id" in _reasons(node)


@pytest.mark.parametrize("account", ["board", "treasury", "config"])
def test_agreeing_declarations_stay_derivable(ore_idl: dict, account: str) -> None:
    """The control that proves the fix is not just flagging everything: these names
    are declared by many instructions, identically, and stay resolvable."""
    node = from_anchor_idl(ore_idl)[account]
    assert node.resolvable
