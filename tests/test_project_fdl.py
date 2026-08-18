"""The FDL projector — does our graph become a flow that derives the RIGHT address?

Two things are being tested, and only one of them is shape. The shape tests
(his lint, his grammar, his node ids) keep the document publishable. The
translation tests keep it CORRECT: an FDL seed list is executed by
``resolve.pda@1``'s ``encodeSeedEntry``, so this module carries a faithful
byte-level copy of that encoder (:func:`encode_fdl_seed`) and derives the
projected seeds the way his worker would — offline, $0, falsifiable here rather
than in a mainnet simulation (Pattern B). One of those derivations is checked
against an address mainnet confirmed.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from gecko.pda import (
    OrderedPairPdaSeedNode,
    PdaNode,
    ResolverPdaSeedNode,
    VariablePdaSeedNode,
    b58_encode,
    derive_pda,
)
from gecko.program_graph import (
    AccountRef,
    InstructionGraph,
    ProgramGraph,
    SeedBinding,
    build_program_graph,
)
from gecko.project.fdl import (
    InvalidSlugError,
    UnknownInstructionError,
    UnprojectableArgError,
    UnprojectableSeedError,
    UnresolvedSeedProjectionError,
    to_fdl,
)

FIXTURES = Path(__file__).parent / "fixtures"
PROGRAM_ID = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

#: Chain fact, pinned in tests/test_let_me_buy_config.py: the receipts PDA for the
#: store "jonasbar", read off mainnet where the program owns the account.
JONASBAR_RECEIPTS = "H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V"

#: The nine accounts gecko/providers/configs/orquestra/let_me_buy.json documents for
#: make_purchase, in IDL order.
MAKE_PURCHASE_ACCOUNTS = [
    "receipts",
    "signer",
    "authority",
    "mint",
    "sender_token_account",
    "recipient_token_account",
    "token_program",
    "system_program",
    "associated_token_program",
]

#: Byte width per numeric seed kind — a copy of his INT_SEED_SIZES.
_INT_SEED_SIZES = {
    "u8": 1,
    "i8": 1,
    "u16": 2,
    "i16": 2,
    "u32": 4,
    "i32": 4,
    "u64": 8,
    "i64": 8,
    "u128": 16,
    "i128": 16,
}


def let_me_buy_graph() -> ProgramGraph:
    idl = json.loads((FIXTURES / "let_me_buy_idl.json").read_text())
    return build_program_graph(idl=idl)


def encode_fdl_seed(entry: str | Mapping[str, str], values: Mapping[str, Any]) -> bytes:
    """A faithful port of ``resolve.pda@1``'s ``encodeSeedEntry`` (resolvers/pda.ts),
    with ``$inputs.x`` / ``$node.field`` references resolved from ``values`` first —
    i.e. what his worker would actually hash. If this and our own
    :func:`gecko.pda.derive_pda` disagree, the translation is wrong."""

    def resolve(raw: str) -> str:
        return str(values[raw]) if raw.startswith("$") else raw

    if isinstance(entry, str):
        return resolve(entry).encode("utf-8")
    kind, value = entry["kind"], resolve(entry["value"])
    if kind == "pubkey":
        from solders.pubkey import Pubkey

        return bytes(Pubkey.from_string(value))
    if kind == "string":
        return value.encode("utf-8")
    if kind == "bytes":
        return base64.b64decode(value)
    return int(value).to_bytes(_INT_SEED_SIZES[kind], "little", signed=kind[0] == "i")


def pda_node(doc: Mapping[str, Any], node_id: str) -> dict[str, Any]:
    for node in doc["nodes"]:
        if node["id"] == node_id:
            return dict(node)
    raise AssertionError(f"no node {node_id!r} in {[n['id'] for n in doc['nodes']]}")


def seeds_of(doc: Mapping[str, Any], account: str) -> list[Any]:
    return list(pda_node(doc, f"pda_{account}")["in"]["seeds"])


# ---------------------------------------------------------------------------
# synthetic graphs — one PDA, one instruction, whatever seed shape is under test
# ---------------------------------------------------------------------------


def one_pda_idl(
    seeds: list[dict[str, Any]],
    args: list[dict[str, Any]],
    extra_accounts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "address": PROGRAM_ID,
        "instructions": [
            {
                "name": "act",
                "accounts": [
                    {"name": "thing", "writable": True, "pda": {"seeds": seeds}},
                    {"name": "authority", "writable": True, "signer": True},
                    *(extra_accounts or []),
                ],
                "args": args,
            }
        ],
    }


def project_one_pda(
    seeds: list[dict[str, Any]],
    args: list[dict[str, Any]],
    extra_accounts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    graph = build_program_graph(idl=one_pda_idl(seeds, args, extra_accounts))
    return to_fdl(graph, "act", slug="probe", intent="test")


def hand_built_graph(*seed_nodes: Any) -> ProgramGraph:
    """A graph carrying seed forms only SOURCE recovery produces (an ordered pair, a
    big-endian integer, an account-bound integer) — the IDL has no syntax for them,
    so they cannot be reached through :func:`build_program_graph`."""
    node = PdaNode(name="thing", seeds=tuple(seed_nodes), program_id=PROGRAM_ID)
    bindings = tuple(
        SeedBinding(name, "", "account", name)
        for seed in seed_nodes
        for name in (
            (seed.left, seed.right)
            if isinstance(seed, OrderedPairPdaSeedNode)
            else (seed.name,)
        )
    )
    return ProgramGraph(
        program_id=PROGRAM_ID,
        pdas={"thing": node},
        instructions=(
            InstructionGraph(
                name="act",
                args=(),
                accounts=(
                    AccountRef("thing", True, derive_from=bindings),
                    # every seed operand is supplied as a plain account slot
                    *(
                        AccountRef(name, False)
                        for name in dict.fromkeys(b.seed_name for b in bindings)
                    ),
                    AccountRef("authority", False, signer=True, writable=True),
                ),
                derivation_order=("thing",),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# seed-kind translation — one test per kind our graph can emit
# ---------------------------------------------------------------------------


def test_a_utf8_constant_becomes_his_bare_string_seed() -> None:
    doc = project_one_pda(
        [{"kind": "const", "value": list(b"receipts")}],
        [],
    )
    assert seeds_of(doc, "thing") == ["receipts"]


def test_a_32_byte_constant_becomes_a_pubkey_seed_not_a_byte_list() -> None:
    """A hardcoded program/account address baked into a seed round-trips to base58 —
    the form a human reviewing the flow can recognise."""
    raw = bytes(range(1, 33))
    doc = project_one_pda([{"kind": "const", "value": list(raw)}], [])
    assert seeds_of(doc, "thing") == [{"kind": "pubkey", "value": b58_encode(raw)}]


def test_a_non_printable_constant_becomes_exact_base64_bytes() -> None:
    raw = b"\x00\x01\xff\xfe"
    doc = project_one_pda([{"kind": "const", "value": list(raw)}], [])
    entry = seeds_of(doc, "thing")[0]
    assert entry == {"kind": "bytes", "value": base64.b64encode(raw).decode()}
    assert encode_fdl_seed(entry, {}) == raw


def test_a_literal_seed_starting_with_a_dollar_goes_out_as_bytes() -> None:
    """His interpreter resolves ANY string in a node's `in` that starts with `$`, at
    any depth. A constant seed whose text starts with `$` would be read as a
    reference and silently replaced — so it is emitted as raw bytes instead."""
    raw = b"$vault"
    doc = project_one_pda([{"kind": "const", "value": list(raw)}], [])
    entry = seeds_of(doc, "thing")[0]
    assert entry == {"kind": "bytes", "value": base64.b64encode(raw).decode()}
    assert encode_fdl_seed(entry, {}) == raw


def test_an_account_seed_becomes_a_pubkey_kind_bound_to_that_account() -> None:
    doc = project_one_pda(
        [{"kind": "account", "path": "authority"}],
        [],
    )
    assert seeds_of(doc, "thing") == [{"kind": "pubkey", "value": "$inputs.authority"}]


def test_a_string_arg_seed_becomes_a_string_kind_bound_to_that_input() -> None:
    doc = project_one_pda(
        [{"kind": "arg", "path": "store_name"}],
        [{"name": "store_name", "type": "string"}],
    )
    assert seeds_of(doc, "thing") == [{"kind": "string", "value": "$inputs.store_name"}]


def test_a_pubkey_arg_seed_becomes_a_pubkey_kind() -> None:
    doc = project_one_pda(
        [{"kind": "arg", "path": "owner"}],
        [{"name": "owner", "type": "pubkey"}],
    )
    assert seeds_of(doc, "thing") == [{"kind": "pubkey", "value": "$inputs.owner"}]


@pytest.mark.parametrize("idl_type", ["u8", "u16", "u32", "u64"])
def test_an_unsigned_integer_seed_carries_its_exact_width(idl_type: str) -> None:
    doc = project_one_pda(
        [{"kind": "arg", "path": "id"}],
        [{"name": "id", "type": idl_type}],
    )
    assert seeds_of(doc, "thing") == [{"kind": idl_type, "value": "$inputs.id"}]


def test_a_signed_integer_seed_stays_signed() -> None:
    """Our seed model records width and endianness but NOT signedness — u64 and i64
    are both 8-byte little-endian to it. His vocabulary distinguishes them, and the
    difference is only visible on a negative value, so the kind is taken from the
    IDL's declared arg type. Getting this wrong is a wrong address for exactly the
    inputs nobody tests."""
    doc = project_one_pda(
        [{"kind": "arg", "path": "index"}],
        [{"name": "index", "type": "i64"}],
    )
    entry = seeds_of(doc, "thing")[0]
    assert entry == {"kind": "i64", "value": "$inputs.index"}
    assert encode_fdl_seed(entry, {"$inputs.index": -3}) == (-3).to_bytes(
        8, "little", signed=True
    )


def test_a_bytes_arg_is_refused_because_fdl_has_no_bytes_input_type() -> None:
    """FLOW_INPUT_TYPES has no bytes, so the value cannot reach the flow at all —
    the whole instruction is unprojectable, seed or no seed."""
    with pytest.raises(UnprojectableArgError, match="bytes"):
        project_one_pda(
            [{"kind": "arg", "path": "blob"}],
            [{"name": "blob", "type": "bytes"}],
        )


def test_a_raw_bytes_seed_is_refused() -> None:
    """resolve.pda@1's bytes kind wants a base64 LITERAL; there is no input type
    that could carry one, so a bytes-encoded variable seed has no expressible form."""
    graph = hand_built_graph(
        VariablePdaSeedNode("blob", source="account", encoding="bytes")
    )
    with pytest.raises(UnprojectableSeedError, match="bytes"):
        to_fdl(graph, "act", slug="probe", intent="test")


def test_a_big_endian_seed_is_refused() -> None:
    """Every numeric seed kind resolve.pda@1 accepts is little-endian."""
    graph = hand_built_graph(
        VariablePdaSeedNode("index", source="account", encoding="be", width=8)
    )
    with pytest.raises(UnprojectableSeedError, match="big-endian"):
        to_fdl(graph, "act", slug="probe", intent="test")


def test_an_integer_seed_with_no_declared_arg_type_is_refused() -> None:
    """Signedness is not recoverable from an account-bound integer seed, and there is
    no correct guess — so it refuses rather than pick one."""
    graph = hand_built_graph(
        VariablePdaSeedNode("index", source="account", encoding="le", width=8)
    )
    with pytest.raises(UnprojectableSeedError, match="signed/unsigned"):
        to_fdl(graph, "act", slug="probe", intent="test")


def test_an_ordered_pair_seed_is_refused_not_flattened() -> None:
    """min/max(a, b) pool-pair ordering has no FDL form at all: resolve.pda@1 has no
    ordered-pair kind and the expression grammar has no function calls. Emitting the
    operands in declaration order would be a well-formed flow for the wrong pool."""
    graph = hand_built_graph(
        OrderedPairPdaSeedNode(left="token_a", right="token_b", select="min")
    )
    with pytest.raises(UnprojectableSeedError, match="ordered-pair"):
        to_fdl(graph, "act", slug="probe", intent="test")


# ---------------------------------------------------------------------------
# the refusal
# ---------------------------------------------------------------------------


def test_an_unresolved_seed_refuses_and_names_the_account_and_the_seed() -> None:
    """THE refusal. A seed that binds to nothing the instruction supplies must not
    become "just another input": the caller would have to invent the value, and a
    wrong PDA derives, compiles and simulates like a right one."""
    idl = one_pda_idl(
        [{"kind": "arg", "path": "store_name"}],
        [{"name": "unrelated_name", "type": "string"}],
    )
    graph = build_program_graph(idl=idl)
    with pytest.raises(UnresolvedSeedProjectionError) as excinfo:
        to_fdl(graph, "act", slug="probe", intent="test")
    message = str(excinfo.value)
    assert "thing" in message and "store_name" in message


def test_a_runtime_data_seed_refuses_rather_than_inventing_an_input() -> None:
    """An `account_field` seed (`bonding_curve.creator`) is a value read from another
    account at run time. FDL can decode an account, but it has no way to say "this
    seed is a guess" — so the projection stops."""
    idl = one_pda_idl(
        [{"kind": "account", "path": "bonding_curve.creator"}],
        [],
        extra_accounts=[{"name": "bonding_curve"}],
    )
    graph = build_program_graph(idl=idl)
    with pytest.raises(UnresolvedSeedProjectionError, match="thing"):
        to_fdl(graph, "act", slug="probe", intent="test")


def test_a_resolver_seed_on_a_hand_built_graph_refuses_by_seed_name() -> None:
    graph = hand_built_graph(
        ResolverPdaSeedNode(name="hashed", depends_on=(), reason="hashed seed")
    )
    graph = ProgramGraph(
        program_id=graph.program_id,
        pdas=graph.pdas,
        # the account keeps resolvable=True so the refusal must come from the SEED
        # branch, not from the account-level flag
        instructions=graph.instructions,
    )
    with pytest.raises(UnresolvedSeedProjectionError) as excinfo:
        to_fdl(graph, "act", slug="probe", intent="test")
    assert "hashed" in str(excinfo.value)


def test_an_unorderable_account_refuses() -> None:
    """A seed-dependency cycle leaves the derivation order arbitrary. The graph
    already flags it; the projector must not publish a flow built on it."""
    idl = {
        "address": PROGRAM_ID,
        "instructions": [
            {
                "name": "act",
                "accounts": [
                    {"name": "a", "pda": {"seeds": [{"kind": "account", "path": "b"}]}},
                    {"name": "b", "pda": {"seeds": [{"kind": "account", "path": "a"}]}},
                ],
                "args": [],
            }
        ],
    }
    with pytest.raises(UnresolvedSeedProjectionError, match="cycle"):
        to_fdl(build_program_graph(idl=idl), "act", slug="probe", intent="test")


# ---------------------------------------------------------------------------
# the arg-name mismatch the IDL loses
# ---------------------------------------------------------------------------


def test_mark_as_delivered_survives_the_arg_name_the_idl_loses() -> None:
    """mark_as_delivered's receipts seed points at `store_name`; its args are
    `_store_name` and `receipt_id`. A deriver that matches seeds to args by name
    finds NOTHING for the one seed that selects the store. Rust's unused-parameter
    underscore is a rename, not a different value — so the projection binds the seed
    and the wire arg to the same single input, and records the alias."""
    doc = to_fdl(
        let_me_buy_graph(), "mark_as_delivered", slug="mark-delivered", intent="fulfil"
    )
    assert seeds_of(doc, "receipts") == [
        "receipts",
        {"kind": "string", "value": "$inputs._store_name"},
    ]
    build = pda_node(doc, "ix_mark_as_delivered")["in"]
    # the SAME input feeds the seed and the instruction arg — they cannot disagree
    assert build["args"]["_store_name"] == "$inputs._store_name"
    assert doc["inputs"]["_store_name"]["type"] == "string"
    assert "store_name" not in doc["inputs"]
    assert doc["x-gecko"]["carriedOutsideFdl"]["argAliases"] == {
        "store_name": "_store_name"
    }


def test_the_alias_derives_the_address_mainnet_confirmed() -> None:
    """The alias is only worth anything if the seeds it produces hash to the real
    account. Executed through his encoder, not ours."""
    doc = to_fdl(
        let_me_buy_graph(), "mark_as_delivered", slug="mark-delivered", intent="fulfil"
    )
    node = pda_node(doc, "pda_receipts")["in"]
    seed_bytes = [
        encode_fdl_seed(entry, {"$inputs._store_name": "jonasbar"})
        for entry in node["seeds"]
    ]
    from solders.pubkey import Pubkey

    address, _ = Pubkey.find_program_address(
        seed_bytes, Pubkey.from_string(node["program"])
    )
    assert str(address) == JONASBAR_RECEIPTS


# ---------------------------------------------------------------------------
# make_purchase — the nine accounts, and the addresses they derive
# ---------------------------------------------------------------------------


def test_make_purchase_projects_all_nine_documented_accounts() -> None:
    doc = to_fdl(let_me_buy_graph(), "make_purchase", slug="buy", intent="purchase")
    accounts = pda_node(doc, "ix_make_purchase")["in"]["accounts"]
    assert list(accounts) == MAKE_PURCHASE_ACCOUNTS


def test_the_three_pda_accounts_each_get_their_own_resolver_node() -> None:
    doc = to_fdl(let_me_buy_graph(), "make_purchase", slug="buy", intent="purchase")
    pda_ids = [n["id"] for n in doc["nodes"] if n["type"] == "resolve.pda@1"]
    # dependency order, straight from the graph
    assert pda_ids == [
        "pda_receipts",
        "pda_sender_token_account",
        "pda_recipient_token_account",
    ]
    assert pda_node(doc, "pda_sender_token_account")["in"]["program"] == ATA_PROGRAM


def test_a_pinned_program_account_is_a_literal_never_an_input() -> None:
    """The IDL pins the system/token/ATA program addresses. Declaring them as inputs
    would let a caller pass a different token program — the ATAs would derive
    somewhere else and the store would be credited at an address nobody owns."""
    doc = to_fdl(let_me_buy_graph(), "make_purchase", slug="buy", intent="purchase")
    accounts = pda_node(doc, "ix_make_purchase")["in"]["accounts"]
    assert accounts["token_program"] == TOKEN_PROGRAM
    assert accounts["system_program"] == SYSTEM_PROGRAM
    assert accounts["associated_token_program"] == ATA_PROGRAM
    for pinned in ("token_program", "system_program", "associated_token_program"):
        assert pinned not in doc["inputs"]
    # and the ATA seed reads that same literal, not an input
    assert seeds_of(doc, "sender_token_account")[1] == {
        "kind": "pubkey",
        "value": TOKEN_PROGRAM,
    }


def test_the_two_atas_are_owned_by_different_accounts() -> None:
    """Deriving both ATAs from the same owner builds a purchase that pays the buyer
    back — the trap the config calls out. The projected seeds must keep the buyer
    (`signer`) and the store (`authority`) apart."""
    doc = to_fdl(let_me_buy_graph(), "make_purchase", slug="buy", intent="purchase")
    assert seeds_of(doc, "sender_token_account")[0] == {
        "kind": "pubkey",
        "value": "$inputs.signer",
    }
    assert seeds_of(doc, "recipient_token_account")[0] == {
        "kind": "pubkey",
        "value": "$inputs.authority",
    }


def test_every_projected_pda_derives_what_our_own_deriver_derives() -> None:
    """Cross-check: his encoder over the projected seeds, versus derive_pda over our
    recipe. Two independent paths to the same address, or the translation is wrong."""
    graph = let_me_buy_graph()
    doc = to_fdl(graph, "make_purchase", slug="buy", intent="purchase")
    from solders.pubkey import Pubkey

    bindings = {
        "store_name": "jonasbar",
        "signer": "H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V",
        "authority": "GDwnP2fPfeaVLQNu1WMHZLwHnJBBrkxdvE7qzMPRhb3P",
        "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "token_program": TOKEN_PROGRAM,
    }
    values = {f"$inputs.{k}": v for k, v in bindings.items()}
    for account in ("receipts", "sender_token_account", "recipient_token_account"):
        node = pda_node(doc, f"pda_{account}")["in"]
        seed_bytes = [encode_fdl_seed(e, values) for e in node["seeds"]]
        projected, _ = Pubkey.find_program_address(
            seed_bytes, Pubkey.from_string(node["program"])
        )
        ours = derive_pda(graph.pdas[account], bindings)
        assert str(projected) == ours.address, account


# ---------------------------------------------------------------------------
# document shape — his lint, his grammar
# ---------------------------------------------------------------------------


def test_the_document_ends_the_two_ways_every_real_flow_must() -> None:
    doc = to_fdl(let_me_buy_graph(), "make_purchase", slug="buy", intent="purchase")
    types = [n["type"] for n in doc["nodes"]]
    assert types.count("solana.compose_transaction@1") == 1
    assert types[-1] == "solana.compose_transaction@1"
    assert types.count("orquestra.build_instruction@1") >= 1
    assert doc["fdl"] == "1.0"
    assert doc["outputs"]["transactions"]["type"] == "transaction[]"


def test_only_registered_node_types_are_emitted() -> None:
    """There is no map.over and no flow.call in his engine."""
    registered = {
        "resolve.pda@1",
        "resolve.ata@1",
        "resolve.pda_state@1",
        "resolve.account_data@1",
        "resolve.blockhash@1",
        "resolve.constant@1",
        "resolve.accounts_by_filter@1",
        "resolve.quote@1",
        "external.http@1",
        "orquestra.build_instruction@1",
        "solana.compose_transaction@1",
        "solana.system_transfer@1",
        "solana.sync_native@1",
        "logic.assert@1",
        "logic.find_in_array@1",
    }
    graph = let_me_buy_graph()
    for ix in graph.instructions:
        doc = to_fdl(graph, ix.name, slug="probe", intent="test")
        assert {n["type"] for n in doc["nodes"]} <= registered


def test_every_reference_resolves_to_something_declared() -> None:
    """His compiler rejects an unresolvable `$inputs.x` or `$node`. Cheaper to catch
    here than in a publish round-trip."""
    graph = let_me_buy_graph()
    for ix in graph.instructions:
        doc = to_fdl(graph, ix.name, slug="probe", intent="test")
        declared_inputs = set(doc["inputs"])
        seen_nodes: set[str] = set()
        for node in doc["nodes"]:
            for ref in _refs(node["in"]):
                root, _, path = ref.lstrip("$").rstrip("?").partition(".")
                if root == "inputs":
                    assert path.split(".")[0] in declared_inputs, ref
                else:
                    assert root in seen_nodes, f"{ref} used before it is produced"
            seen_nodes.add(node["id"])


def _refs(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith("$") else []
    if isinstance(value, list):
        return [r for item in value for r in _refs(item)]
    if isinstance(value, dict):
        return [r for item in value.values() for r in _refs(item)]
    return []


def test_a_slug_his_schema_would_reject_is_refused_here() -> None:
    with pytest.raises(InvalidSlugError):
        to_fdl(let_me_buy_graph(), "initialize", slug="Not Kebab", intent="init")


def test_an_unknown_instruction_names_the_ones_that_exist() -> None:
    with pytest.raises(UnknownInstructionError, match="make_purchase"):
        to_fdl(let_me_buy_graph(), "refund", slug="probe", intent="test")


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_node_ids_are_derived_from_names_not_from_position() -> None:
    doc = to_fdl(let_me_buy_graph(), "make_purchase", slug="buy", intent="purchase")
    assert [n["id"] for n in doc["nodes"]] == [
        "pda_receipts",
        "pda_sender_token_account",
        "pda_recipient_token_account",
        "ix_make_purchase",
        "tx",
    ]


def test_projecting_twice_yields_a_byte_identical_document() -> None:
    """His compiler content-addresses the canonicalized document. A node id that
    moved between runs would republish an identical flow as a new one."""
    first = to_fdl(let_me_buy_graph(), "make_purchase", slug="buy", intent="purchase")
    second = to_fdl(let_me_buy_graph(), "make_purchase", slug="buy", intent="purchase")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    digests = {
        hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()
        for d in (first, second)
    }
    assert len(digests) == 1


def test_input_order_is_stable_when_a_type_is_overridden() -> None:
    """An override changes a type in place; it never reorders the document."""
    base = to_fdl(let_me_buy_graph(), "add_product", slug="add", intent="list")
    overridden = to_fdl(
        let_me_buy_graph(),
        "add_product",
        slug="add",
        intent="list",
        inputs={"price": "u64"},
    )
    assert list(base["inputs"]) == list(overridden["inputs"])
    assert overridden["inputs"]["price"]["type"] == "u64"


def test_an_override_can_declare_an_input_the_graph_does_not_need() -> None:
    doc = to_fdl(
        let_me_buy_graph(),
        "initialize",
        slug="init",
        intent="init",
        inputs={"referrer": "pubkey"},
    )
    assert doc["inputs"]["referrer"] == {"type": "pubkey"}


def test_an_override_outside_his_input_types_is_refused() -> None:
    with pytest.raises(UnprojectableArgError, match="FLOW_INPUT_TYPES"):
        to_fdl(
            let_me_buy_graph(),
            "initialize",
            slug="init",
            intent="init",
            inputs={"blob": "bytes"},
        )


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


def test_meta_is_populated_and_names_the_programs_the_flow_derives_under() -> None:
    doc = to_fdl(let_me_buy_graph(), "make_purchase", slug="buy", intent="purchase")
    meta = doc["meta"]
    assert meta["slug"] == "buy"
    assert meta["name"] == "Make Purchase"
    assert meta["intent"] == "purchase"
    assert meta["programs"] == [PROGRAM_ID, ATA_PROGRAM]


def test_side_effects_asserts_what_the_idl_states_and_nulls_what_it_does_not() -> None:
    """`null` is an explicit "this producer does not know" — never `false`, which
    would read as "checked, and it does not"."""
    doc = to_fdl(let_me_buy_graph(), "make_purchase", slug="buy", intent="purchase")
    effects = doc["meta"]["sideEffects"]
    assert effects["signers"] == ["signer"]
    assert "receipts" in effects["writes"] and "mint" not in effects["writes"]
    assert effects["createsAccounts"] is None
    assert effects["movesValue"] is None


def test_the_provenance_tier_rides_along_because_fdl_has_no_field_for_it() -> None:
    """A resolve.pda@1 node states a recipe with no room to say whether it came from
    the program's own IDL or from regex-parsed source. A reviewer needs that."""
    doc = to_fdl(let_me_buy_graph(), "make_purchase", slug="buy", intent="purchase")
    origins = doc["x-gecko"]["carriedOutsideFdl"]["pdaOrigins"]
    assert origins == {
        "receipts": "extracted",
        "sender_token_account": "extracted",
        "recipient_token_account": "extracted",
    }
