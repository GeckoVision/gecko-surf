"""The payer is ONE actor. An instruction with three signer slots has three.

Measured on the captured IDLs in ``tests/fixtures/orquestra`` — Meteora DLMM
(``LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo``) and pump.fun
(``6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P``) — where filling every unfilled
signer slot with the payer produced these wrong-actor plans:

    meteora initialize_position              payer, position, owner  -> all payer
    meteora initialize_position_by_operator  payer, base, operator   -> all payer
    meteora rebalance_liquidity              owner, rent_payer       -> all payer
    pumpfun create                           mint, user              -> all payer

None of that is caught downstream: ``prepare_instruction`` simulates with
``sigVerify: False``, so the runtime never checks that three DISTINCT keys signed,
and the residual is a clean-simulating transaction with the wrong actor in it.

The rule that DOES earn its place is the single unfilled signer: making a caller
repeat their own address under whatever local name the program chose is a question
with one answer. That arm is asserted here too, against the same instructions —
the two arms differ only in how many slots the caller left open.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gecko.prepare_instruction import plan_accounts
from gecko.program_graph import ProgramGraph, build_program_graph

FIXTURES = Path(__file__).parent / "fixtures" / "orquestra"
METEORA = "v48gsz901w84zriqe0elsl"
PUMPFUN = "6i6q26bmm46b89xlxo1kv"

PAYER = "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi"
OWNER = "6Dw1xBGXChPeS69hovvYMF2nmRxgdoA711TKuuAbN5rV"
POSITION = "8psNvWTrdNTiVRNzAgsou9kETXNJm2SXZyaKuJraVRtf"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _graph(slug: str) -> ProgramGraph:
    idl = json.loads((FIXTURES / slug / "idl.json").read_text(encoding="utf-8"))["idl"]
    return build_program_graph(idl)


@pytest.fixture(scope="module")
def meteora() -> ProgramGraph:
    return _graph(METEORA)


@pytest.fixture(scope="module")
def pumpfun() -> ProgramGraph:
    return _graph(PUMPFUN)


def _origin(origins: list[dict[str, Any]], account: str) -> str | None:
    return next((o["origin"] for o in origins if o["account"] == account), None)


def _missing(missing: list[dict[str, Any]], account: str) -> dict[str, Any] | None:
    return next((m for m in missing if m["account"] == account), None)


# -- the wrong actor --------------------------------------------------------


@pytest.mark.parametrize(
    ("program", "instruction", "signers"),
    [
        ("meteora", "initialize_position", ("payer", "position", "owner")),
        (
            "meteora",
            "initialize_position_by_operator",
            ("payer", "base", "operator"),
        ),
        ("meteora", "rebalance_liquidity", ("owner", "rent_payer")),
        ("pumpfun", "create", ("mint", "user")),
    ],
)
def test_the_payer_never_fills_a_second_signer_slot(
    request: pytest.FixtureRequest,
    program: str,
    instruction: str,
    signers: tuple[str, ...],
) -> None:
    graph: ProgramGraph = request.getfixturevalue(program)
    ix = next(i for i in graph.instructions if i.name == instruction)
    assert tuple(a.name for a in ix.accounts if a.signer) == signers, (
        "fixture drifted — this test is about the instructions that take several"
    )

    resolved, origins, missing = plan_accounts(graph, instruction, {}, payer=PAYER)

    filled_with_payer = [n for n in signers if resolved.get(n) == PAYER]
    assert filled_with_payer == [], (
        f"{instruction} is signed by {len(signers)} distinct actors; "
        f"{filled_with_payer} were handed the payer's key"
    )
    for name in signers:
        gap = _missing(missing, name)
        assert gap is not None, f"{name} was neither resolved nor reported"
        assert gap["signer"] is True
        # legible: the reason names the OTHER signers, so a caller can see what
        # the ambiguity is rather than being told "supply this".
        for other in signers:
            assert other in gap["why"], gap["why"]


def test_a_single_unfilled_signer_is_still_the_payer(meteora: ProgramGraph) -> None:
    """The arm that must NOT change — and the proof the two arms can diverge.

    Same instruction as the first case above. Name two of the three actors and the
    third is the payer, with one possible answer; leave all three open and it has
    three.
    """
    resolved, origins, missing = plan_accounts(
        meteora,
        "initialize_position",
        {"position": POSITION, "owner": OWNER},
        payer=PAYER,
    )
    assert resolved["payer"] == PAYER
    assert _origin(origins, "payer") == "supplied"
    assert _missing(missing, "payer") is None


def test_with_no_payer_at_all_the_only_signer_is_still_reported(
    meteora: ProgramGraph,
) -> None:
    """No payer, one open signer: a gap, never a fabricated address."""
    _, _, missing = plan_accounts(
        meteora,
        "initialize_position",
        {"position": POSITION, "owner": OWNER},
        payer=None,
    )
    assert _missing(missing, "payer") is not None


# -- the signer that is a PDA ----------------------------------------------


def test_a_pda_signer_is_derived_not_papered_over_with_the_payer(
    pumpfun: ProgramGraph,
) -> None:
    """pump.fun `set_mayhem_virtual_params` — one signer slot, and it is a PDA.

    `sol_vault_authority` is declared as a PDA on the constant seed `sol-vault`
    under a different program, and it is the instruction's only signer. Filling it
    from the payer does not merely mislabel one slot: `mayhem_token_vault` seeds on
    it, so the wrong address CASCADES into a second account that then looks
    perfectly well-formed.
    """
    resolved, origins, missing = plan_accounts(
        pumpfun, "set_mayhem_virtual_params", {"mint": MINT}, payer=PAYER
    )
    assert resolved["sol_vault_authority"] != PAYER
    assert _origin(origins, "sol_vault_authority") == "derived"

    # the cascade: the vault is the ATA of the DERIVED authority, not of the payer
    from gecko.pda import derive_pda

    vault = derive_pda(
        pumpfun.pdas["mayhem_token_vault"],
        {**resolved, "mint": MINT},
    ).address
    assert resolved["mayhem_token_vault"] == vault
    assert _missing(missing, "sol_vault_authority") is None
