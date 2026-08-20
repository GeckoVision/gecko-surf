"""Phase 2b: the instruction↔PDA derivation graph, assembled + serialized.

The deliverable Berkay's orchestrator ingests — structured, not text. Reuses the
Anchor IDL fixture and the ORE source; checks the join (which account is a PDA,
what it derives from), the dependency-ordered derivation plan, the honest flag on
unresolvable PDAs, and that the JSON round-trips.
"""

from __future__ import annotations

import json

import pytest

from gecko.program_graph import ProgramGraph, build_program_graph
from tests.test_pda_extract import ORE_PROGRAM, ORE_SOURCE
from tests.test_pda_idl import ANCHOR_IDL


def test_graph_joins_instructions_to_pdas() -> None:
    graph = build_program_graph(idl=ANCHOR_IDL)
    assert graph.program_id == ORE_PROGRAM

    ix = {i.name: i for i in graph.instructions}
    assert set(ix) == {"init_config", "open_round", "deposit"}

    open_round = ix["open_round"]
    accts = {a.name: a for a in open_round.accounts}
    # round + miner are PDAs, authority is a plain signer
    assert accts["round"].is_pda and accts["miner"].is_pda
    assert accts["authority"].is_pda is False and accts["authority"].signer

    # round derives from the `id` ARG; miner from the `authority` ACCOUNT
    round_binds = {b.seed_name: b for b in accts["round"].derive_from}
    assert round_binds["id"].kind == "argument" and round_binds["id"].bound_to == "id"
    miner_binds = {b.seed_name: b for b in accts["miner"].derive_from}
    assert miner_binds["authority"].kind == "account"
    assert miner_binds["authority"].bound_to == "authority"


def test_derivation_order_lists_the_pda_accounts() -> None:
    graph = build_program_graph(idl=ANCHOR_IDL)
    open_round = {i.name: i for i in graph.instructions}["open_round"]
    assert set(open_round.derivation_order) == {"round", "miner"}


def test_dependent_pda_is_ordered_after_its_dependency() -> None:
    """A PDA seeded by ANOTHER PDA account in the same instruction must be derived
    after it — the derivation DAG, not just a list."""
    idl = {
        "address": ORE_PROGRAM,
        "instructions": [
            {
                "name": "stake",
                "accounts": [
                    {
                        "name": "miner",
                        "pda": {
                            "seeds": [
                                {"kind": "const", "value": list(b"miner")},
                                {"kind": "account", "path": "authority"},
                            ]
                        },
                    },
                    {
                        "name": "stake_acct",
                        "pda": {
                            "seeds": [
                                {"kind": "const", "value": list(b"stake")},
                                {
                                    "kind": "account",
                                    "path": "miner",
                                },  # depends on the miner PDA
                            ]
                        },
                    },
                    {"name": "authority", "signer": True},
                ],
                "args": [],
            }
        ],
    }
    order = build_program_graph(idl=idl).instructions[0].derivation_order
    assert order.index("miner") < order.index("stake_acct")


def test_unresolvable_pda_is_flagged_in_the_graph() -> None:
    graph = build_program_graph(idl=ANCHOR_IDL)
    deposit = {i.name: i for i in graph.instructions}["deposit"]
    vault = {a.name: a for a in deposit.accounts}["vault"]
    assert vault.is_pda
    assert vault.resolvable is False  # honest gap, surfaced in the join


def test_both_joined_fills_dropped_recipe() -> None:
    """IDL breadth (instructions) + source recovery (#4057 seeds): an IDL that
    dropped config's pda block still yields a resolvable config in the graph,
    recovered from source."""
    idl_missing = {
        "address": ORE_PROGRAM,
        "instructions": [
            {"name": "init_config", "accounts": [{"name": "config"}], "args": []}
        ],
    }
    graph = build_program_graph(idl=idl_missing, source=ORE_SOURCE)
    config_acct = {a.name: a for a in graph.instructions[0].accounts}["config"]
    assert config_acct.is_pda  # source rescued the recipe -> the join sees a PDA
    assert config_acct.resolvable


def _cycle_idl(accounts: list[dict[str, object]]) -> dict[str, object]:
    return {
        "address": ORE_PROGRAM,
        "instructions": [{"name": "tangle", "accounts": accounts, "args": []}],
    }


def test_seed_dependency_cycle_is_flagged_not_rendered_as_a_plausible_order() -> None:
    """Two PDAs that seed from each other cannot be derived at all. The order the
    graph emits must not read as derivable: the cycle members are reported, and
    every account in the cycle is ``resolvable=False``."""
    idl = _cycle_idl(
        [
            {
                "name": "a",
                "pda": {"seeds": [{"kind": "account", "path": "b"}]},
            },
            {
                "name": "b",
                "pda": {"seeds": [{"kind": "account", "path": "a"}]},
            },
        ]
    )
    ix = build_program_graph(idl=idl).instructions[0]
    accts = {a.name: a for a in ix.accounts}

    assert ix.cycle == ("a", "b")  # the honest gap, named
    assert accts["a"].resolvable is False
    assert accts["b"].resolvable is False
    # never dropped — an omitted account is the worse failure
    assert set(ix.derivation_order) == {"a", "b"}


def test_self_seeded_pda_is_flagged() -> None:
    """A PDA whose own address is an input to its own derivation is un-derivable."""
    idl = _cycle_idl(
        [{"name": "a", "pda": {"seeds": [{"kind": "account", "path": "a"}]}}]
    )
    ix = build_program_graph(idl=idl).instructions[0]
    assert ix.cycle == ("a",)
    assert {a.name: a for a in ix.accounts}["a"].resolvable is False


def test_an_account_blocked_by_a_cycle_is_flagged_too() -> None:
    """A PDA seeded by a cycle member is equally un-derivable — reported, not ordered."""
    idl = _cycle_idl(
        [
            {"name": "a", "pda": {"seeds": [{"kind": "account", "path": "b"}]}},
            {"name": "b", "pda": {"seeds": [{"kind": "account", "path": "a"}]}},
            {"name": "c", "pda": {"seeds": [{"kind": "account", "path": "a"}]}},
        ]
    )
    ix = build_program_graph(idl=idl).instructions[0]
    assert ix.cycle == ("a", "b", "c")
    assert all(a.resolvable is False for a in ix.accounts if a.is_pda)


def test_an_acyclic_instruction_reports_no_cycle() -> None:
    graph = build_program_graph(idl=ANCHOR_IDL)
    assert all(ix.cycle == () for ix in graph.instructions)


def test_cycle_is_carried_into_the_json_payload() -> None:
    """The orchestrator ingests JSON — the cycle must survive the boundary."""
    idl = _cycle_idl(
        [
            {"name": "a", "pda": {"seeds": [{"kind": "account", "path": "b"}]}},
            {"name": "b", "pda": {"seeds": [{"kind": "account", "path": "a"}]}},
        ]
    )
    payload = json.loads(json.dumps(build_program_graph(idl=idl).to_json()))
    ix = payload["instructions"][0]
    assert ix["cycle"] == ["a", "b"]
    assert all(a["resolvable"] is False for a in ix["accounts"])


def test_agent_payload_carries_each_recipe_s_origin() -> None:
    """R7 — the origin must reach the AGENT, not only the CLI's stderr.

    The IDL drops `config`'s pda block (#4057) and regex-parsed source rescues it;
    the IDL states `round` itself. An agent reading `get_program_graph` has to be
    able to tell those apart — an IDL-stated recipe and one recovered from
    untrusted source text are not equally trustworthy.
    """
    idl_missing_config = {
        **ANCHOR_IDL,
        "instructions": [
            ix
            for ix in ANCHOR_IDL["instructions"]
            # init_config is the instruction carrying config's pda block
            if ix["name"] != "init_config"
        ],
    }
    payload = json.loads(
        json.dumps(
            build_program_graph(idl=idl_missing_config, source=ORE_SOURCE).to_json()
        )
    )
    assert payload["pdas"]["config"]["origin"] == "recovered"  # source rescued it
    assert payload["pdas"]["round"]["origin"] == "extracted"  # the IDL stated it


def test_origin_is_absent_only_as_an_explicit_unknown() -> None:
    """A hand-built graph with no origin map emits ``origin: null`` rather than
    omitting the key or defaulting to ``extracted`` — a missing key would let a
    consumer read silence as 'this producer does not report origin', and a default
    of ``extracted`` is exactly the false confidence R3 closed on find_start."""
    graph = build_program_graph(idl=ANCHOR_IDL)
    bare = ProgramGraph(
        program_id=graph.program_id, pdas=graph.pdas, instructions=graph.instructions
    )
    payload = bare.to_json()
    assert all("origin" in p for p in payload["pdas"].values())
    assert all(p["origin"] is None for p in payload["pdas"].values())


def test_to_json_is_serializable_and_complete() -> None:
    graph = build_program_graph(idl=ANCHOR_IDL, source=ORE_SOURCE)
    payload = graph.to_json()
    # round-trips through JSON (plug-and-play for an orchestrator)
    reloaded = json.loads(json.dumps(payload))
    assert reloaded["program_id"] == ORE_PROGRAM
    assert "config" in reloaded["pdas"]
    # a const seed carries both its bytes and the human utf8 form
    config_seed = reloaded["pdas"]["config"]["seeds"][0]
    assert config_seed["kind"] == "const" and config_seed["utf8"] == "config"
    # instructions carry the derivation order
    names = {i["name"] for i in reloaded["instructions"]}
    assert {"init_config", "open_round", "deposit"} <= names


def test_requires_an_input() -> None:
    with pytest.raises(ValueError):
        build_program_graph()


# ---------------------------------------------------------------------------
# the sibling-recipe import rule — a recipe this instruction cannot satisfy is
# not this instruction's recipe
# ---------------------------------------------------------------------------


def _collateral_idl() -> dict[str, object]:
    """`main` (AwW51ngTLTvfd31gXBhZ2XYTCKsdSV553XKmc8WPAfiM), two instructions.

    `create_collateral` declares `collateral` from an ARG FIELD (`new_collateral.id`).
    `close_old_collateral_signatures` takes the same account as a plain slot — no `pda`
    block — and has NO ARGS AT ALL. Importing the sibling's recipe there invents a need
    for a struct field the caller of that instruction never builds.
    """
    return {
        "address": "AwW51ngTLTvfd31gXBhZ2XYTCKsdSV553XKmc8WPAfiM",
        "instructions": [
            {
                "name": "create_collateral",
                "args": [
                    {
                        "name": "new_collateral",
                        "type": {"defined": {"name": "NewCollateral"}},
                    }
                ],
                "accounts": [
                    {"name": "sender", "signer": True, "writable": True},
                    {"name": "coordinator"},
                    {
                        "name": "collateral",
                        "writable": True,
                        "pda": {
                            "seeds": [
                                {"kind": "const", "value": list(b"Collateral")},
                                {"kind": "arg", "path": "new_collateral.id"},
                                {"kind": "account", "path": "coordinator"},
                            ]
                        },
                    },
                    {
                        "name": "collateral_authority",
                        "pda": {
                            "seeds": [
                                {
                                    "kind": "const",
                                    "value": list(b"CollateralAuthority"),
                                },
                                {"kind": "account", "path": "collateral"},
                            ]
                        },
                    },
                ],
            },
            {
                "name": "close_old_collateral_signatures",
                "args": [],
                "accounts": [
                    {"name": "sender", "signer": True, "writable": True},
                    {"name": "coordinator"},
                    {"name": "collateral"},
                    {
                        "name": "collateral_authority",
                        "writable": True,
                        "pda": {
                            "seeds": [
                                {
                                    "kind": "const",
                                    "value": list(b"CollateralAuthority"),
                                },
                                {"kind": "account", "path": "collateral"},
                            ]
                        },
                    },
                    {"name": "collateral_admin_signatures", "writable": True},
                ],
            },
        ],
        "types": [
            {
                "name": "NewCollateral",
                "type": {"kind": "struct", "fields": [{"name": "id", "type": "u64"}]},
            }
        ],
    }


def test_an_instruction_with_no_args_cannot_need_an_arg_field() -> None:
    """`new_collateral.id` is the 2nd most common unhinted need in the whole catalogue
    (36 of 196 IDLs), and every one of them is this artifact.

    `close_old_collateral_signatures` takes `args: []`. There is no `new_collateral` to
    hold an `id`, so a plan that asks its caller for `new_collateral.id` asks for
    something that does not exist in that instruction — a wrong-but-plausible need. The
    account is a slot the caller supplies; say that.
    """
    graph = build_program_graph(idl=_collateral_idl())
    close = {i.name: i for i in graph.instructions}["close_old_collateral_signatures"]
    collateral = {a.name: a for a in close.accounts}["collateral"]

    assert collateral.is_pda is False, (
        "an instruction that does not declare an account as a PDA, and could not "
        "satisfy the sibling recipe anyway, must report a caller-supplied slot"
    )
    assert collateral.caller_must_supply == ()
    assert "collateral" not in close.derivation_order

    # and the recipe is NOT destroyed — it stays in the program-wide map, where it is
    # true, with its origin. Only the claim about THIS instruction stops.
    assert "collateral" in graph.pdas
    assert graph.origins["collateral"] == "extracted"


def test_the_sibling_recipe_still_lands_where_this_instruction_can_satisfy_it() -> None:
    """The other half of the rule: `create_collateral` declares `collateral` itself and
    still derives it, and `collateral_authority` — declared by both — is untouched."""
    graph = build_program_graph(idl=_collateral_idl())
    ixs = {i.name: i for i in graph.instructions}

    create = {a.name: a for a in ixs["create_collateral"].accounts}
    assert create["collateral"].is_pda and create["collateral"].satisfiable
    assert create["collateral_authority"].satisfiable
    # dependency-first: the authority is seeded on the collateral
    order = ixs["create_collateral"].derivation_order
    assert order.index("collateral") < order.index("collateral_authority")

    # `collateral_authority` is declared by BOTH instructions; the close path keeps it,
    # now derived from a caller-supplied `collateral`.
    close = {a.name: a for a in ixs["close_old_collateral_signatures"].accounts}
    assert close["collateral_authority"].is_pda
    assert close["collateral_authority"].satisfiable


def _escrow_idl() -> dict[str, object]:
    """`escrow` (BUMb1Z3Fc7cH3VPJWwjzyfifYYcZYJGYUp9idHsdyw7D), reduced.

    `post_sell_order` declares `user_token_account` as the ATA of `user` + `token`.
    `cancel_order` takes the same NAME as an OPTIONAL, undeclared slot — and has no
    `user` account at all (the wallet there is `initiator`).
    """
    ata_program = [
        140,
        151,
        37,
        143,
        78,
        36,
        137,
        241,
        187,
        61,
        16,
        41,
        20,
        142,
        13,
        131,
        11,
        90,
        19,
        153,
        218,
        255,
        16,
        132,
        4,
        142,
        123,
        216,
        219,
        233,
        248,
        89,
    ]
    token_program = [
        6,
        221,
        246,
        225,
        215,
        101,
        161,
        147,
        217,
        203,
        225,
        70,
        206,
        235,
        121,
        172,
        28,
        180,
        133,
        237,
        95,
        91,
        55,
        145,
        58,
        140,
        245,
        133,
        126,
        255,
        0,
        169,
    ]
    return {
        "address": "BUMb1Z3Fc7cH3VPJWwjzyfifYYcZYJGYUp9idHsdyw7D",
        "instructions": [
            {
                "name": "post_sell_order",
                "args": [],
                "accounts": [
                    {"name": "user", "signer": True, "writable": True},
                    {"name": "token"},
                    {
                        "name": "user_token_account",
                        "writable": True,
                        "pda": {
                            "seeds": [
                                {"kind": "account", "path": "user"},
                                {"kind": "const", "value": token_program},
                                {"kind": "account", "path": "token"},
                            ],
                            "program": {"kind": "const", "value": ata_program},
                        },
                    },
                ],
            },
            {
                "name": "cancel_order",
                "args": [{"name": "order_id", "type": "u64"}],
                "accounts": [
                    {"name": "authority", "signer": True, "writable": True},
                    {"name": "initiator"},
                    {
                        "name": "user_token_account",
                        "docs": ["Required for SELL cancels (refund destination)."],
                        "writable": True,
                        "optional": True,
                    },
                    {"name": "token"},
                    {"name": "token_program"},
                ],
            },
        ],
    }


def test_an_undeclared_slot_does_not_inherit_a_sibling_ata_recipe() -> None:
    """The same NAME is not the same ACCOUNT.

    `cancel_order`'s `user_token_account` is an optional refund destination with no `pda`
    block. Importing `post_sell_order`'s ATA recipe marks it derivable-in-principle and
    then reports the instruction blocked on `user` — an account `cancel_order` does not
    take. The truthful report is a caller-supplied slot.
    """
    graph = build_program_graph(idl=_escrow_idl())
    cancel = {i.name: i for i in graph.instructions}["cancel_order"]
    ata = {a.name: a for a in cancel.accounts}["user_token_account"]

    assert ata.is_pda is False
    assert ata.derive_from == ()
    assert "user" not in ata.caller_must_supply
    assert cancel.derivation_order == ()

    # the declaring instruction is unchanged
    post = {i.name: i for i in graph.instructions}["post_sell_order"]
    posted = {a.name: a for a in post.accounts}["user_token_account"]
    assert posted.is_pda and posted.satisfiable


def test_the_4057_deferrals_are_not_imports_and_do_not_change() -> None:
    """The gate above is on IMPORTS ONLY — an instruction that has no declaration of
    its own. The two deferrals that keep this repo honest both start from a declaration
    the instruction DID make, and neither goes through it.

    1. jurassic_fi's self-referential root: `claim` declares `launch` from `launch.admin`
       — a dead end — and correctly takes the sibling's derivable recipe, because the
       program re-derives the address from the stored fields and rejects a mismatch.
    2. pump.fun's `creator_vault`: a seed reading ANOTHER account is not a dead end, and
       must still refuse to be rescued by a sibling.
    """
    from tests.test_pda_idl import _foreign_account_read_idl, _self_referential_idl

    graph = build_program_graph(idl=_self_referential_idl())
    claim = {i.name: i for i in graph.instructions}["claim"]
    launch = {a.name: a for a in claim.accounts}["launch"]
    assert launch.is_pda and launch.resolvable
    # derived from the sibling's inputs, not from a read of itself
    assert [b.seed_name for b in launch.derive_from] == ["admin", "params.launch_id"]

    pump = build_program_graph(idl=_foreign_account_read_idl())
    buy = {i.name: i for i in pump.instructions}["buy"]
    vault = {a.name: a for a in buy.accounts}["creator_vault"]
    assert vault.is_pda and vault.resolvable is False
    # `buy` DOES take `bonding_curve`, so the seed binds — and the recipe is still not
    # resolvable, because nothing typed the field read off it. `resolvable` and
    # `satisfiable` are different questions and both are reported.
    assert [b.bound_to for b in vault.derive_from] == ["bonding_curve"]
    assert vault.caller_must_supply == ()
    assert vault.satisfiable is False


def test_a_source_recovered_recipe_still_reaches_an_instruction_that_can_bind_it() -> (
    None
):
    """The import path is narrowed, not closed. The #4057 rescue lands through it —
    the IDL dropped `config`'s pda block entirely, source recovered it, and the
    instruction can satisfy every seed — so it must still arrive."""
    idl_missing = {
        "address": ORE_PROGRAM,
        "instructions": [
            {"name": "init_config", "accounts": [{"name": "config"}], "args": []}
        ],
    }
    graph = build_program_graph(idl=idl_missing, source=ORE_SOURCE)
    config = {a.name: a for a in graph.instructions[0].accounts}["config"]
    assert config.is_pda and config.satisfiable
    assert graph.origins["config"] == "recovered"
