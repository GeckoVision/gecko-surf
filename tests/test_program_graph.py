"""Phase 2b: the instruction↔PDA derivation graph, assembled + serialized.

The deliverable Berkay's orchestrator ingests — structured, not text. Reuses the
Anchor IDL fixture and the ORE source; checks the join (which account is a PDA,
what it derives from), the dependency-ordered derivation plan, the honest flag on
unresolvable PDAs, and that the JSON round-trips.
"""

from __future__ import annotations

import json

import pytest

from gecko.program_graph import build_program_graph
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
