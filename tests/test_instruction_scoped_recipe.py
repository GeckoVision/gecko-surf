"""An account's PDA recipe belongs to the (instruction, account) pair, not to the name.

``build_program_graph`` already knows this — it computes each instruction's OWN declared
recipe, documents two live divergences (Orca `whirlpool`, stableswap `token_vault`), and
then keeps only the resolvable flag and the seed bindings from it. ``plan_accounts`` then
derived from the PROGRAM-WIDE ``graph.pdas[name]``, so wherever the two disagreed the
correct node was computed and discarded.

MEASURED on the captured ORE IDL (``oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv``,
``tests/fixtures/orquestra/6alwvs9936laepljczqumb``). ``deploy`` takes both a `signer`
and an `authority` account and declares `miner` and `automation` seeded on `authority`;
five earlier instructions declare the same two names seeded on `signer`, and the
program-wide map keeps those because `automate` is listed first. Bind the two accounts to
different keys and the plan produced:

    miner       2DxDNCL1hRCV5c7VMBJT3ax9QZTKsTRLFZgTLcJvLQbm   (seeded on signer)
    deploy says AKwLPAP9PBkEe8YjV3rz8AjthLrW2JAviQSBiCLPJoQX   (seeded on authority)

Both off-curve, both well-formed, one wrong — and `deploy` stakes SOL. The hand-authored
``gecko/providers/configs/orquestra/ore.json`` records `miner` on `authority`, which is
the same answer arrived at by a human.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gecko.pda import derive_pda
from gecko.pda_extract import instruction_pdas
from gecko.prepare_instruction import plan_accounts
from gecko.program_graph import ProgramGraph, build_program_graph

FIXTURES = Path(__file__).parent / "fixtures" / "orquestra"
ORE = "6alwvs9936laepljczqumb"

SIGNER = "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi"
AUTHORITY = "6Dw1xBGXChPeS69hovvYMF2nmRxgdoA711TKuuAbN5rV"


@pytest.fixture(scope="module")
def ore_idl() -> dict:
    return json.loads((FIXTURES / ORE / "idl.json").read_text(encoding="utf-8"))["idl"]


@pytest.fixture(scope="module")
def ore(ore_idl: dict) -> ProgramGraph:
    return build_program_graph(ore_idl)


def _declared(ore_idl: dict, program_id: str | None, instruction: str) -> dict:
    """This instruction's OWN recipes, read straight from the IDL — the second arm."""
    ix = next(i for i in ore_idl["instructions"] if i["name"] == instruction)
    type_defs = {
        str(t.get("name")): t for t in ore_idl.get("types", []) if isinstance(t, dict)
    }
    return instruction_pdas(ix, program_id=program_id, type_defs=type_defs)


def test_the_fixture_really_does_declare_two_different_recipes(
    ore_idl: dict, ore: ProgramGraph
) -> None:
    """The arms can diverge — asserted before anything is measured against them."""
    declared = {
        name: _declared(ore_idl, ore.program_id, name)
        for name in ("automate", "deploy")
    }
    bound = {"signer": SIGNER, "authority": AUTHORITY}
    for account in ("miner", "automation"):
        by_deploy = declared["deploy"][account]
        by_automate = declared["automate"][account]
        assert by_deploy.required_bindings == ("authority",)
        assert by_automate.required_bindings == ("signer",)
        assert (
            derive_pda(by_deploy, bound).address
            != derive_pda(by_automate, bound).address
        ), "the two declarations must produce different addresses"
        # and the name-keyed map, which cannot hold both, no longer claims either
        assert not ore.pdas[account].resolvable


@pytest.mark.parametrize("account", ["miner", "automation"])
def test_the_plan_derives_what_this_instruction_declares(
    ore_idl: dict, ore: ProgramGraph, account: str
) -> None:
    resolved, origins, _ = plan_accounts(
        ore,
        "deploy",
        {"signer": SIGNER, "authority": AUTHORITY, "entropyVar": SIGNER},
        payer=None,
    )
    bound = {"signer": SIGNER, "authority": AUTHORITY}
    expected = derive_pda(_declared(ore_idl, ore.program_id, "deploy")[account], bound)
    by_the_other = derive_pda(
        _declared(ore_idl, ore.program_id, "automate")[account], bound
    )

    assert expected.address != by_the_other.address
    assert resolved[account] == expected.address, (
        f"`deploy` seeds {account} on `authority`; the plan used the recipe seeded "
        "on `signer`"
    )
    assert next(o["origin"] for o in origins if o["account"] == account) == "derived"


def test_the_instruction_scoped_recipe_is_carried_on_the_account(
    ore_idl: dict, ore: ProgramGraph
) -> None:
    """The node build_program_graph computes is now reachable, not thrown away.

    Without this the fix above is one lookup that happens to agree; with it, every
    consumer of the graph can see which recipe applies where.
    """
    deploy = next(ix for ix in ore.instructions if ix.name == "deploy")
    automate = next(ix for ix in ore.instructions if ix.name == "automate")
    miner_in_deploy = next(a for a in deploy.accounts if a.name == "miner")
    miner_in_automate = next(a for a in automate.accounts if a.name == "miner")

    assert miner_in_deploy.pda is not None
    assert miner_in_automate.pda is not None
    assert miner_in_deploy.pda.required_bindings == ("authority",)
    assert miner_in_automate.pda.required_bindings == ("signer",)
    # a slot this instruction does not declare as a PDA carries no recipe at all
    signer_slot = next(a for a in deploy.accounts if a.name == "signer")
    assert signer_slot.is_pda is False
    assert signer_slot.pda is None
