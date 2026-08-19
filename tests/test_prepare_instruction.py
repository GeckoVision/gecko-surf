"""prepare_instruction — the general path, falsified offline.

PATTERN B. Every seam is injected: the IDL comes from a literal, the builder is a fake
that records what it was handed, and the RPC is a dict. Nothing here touches a network,
and the live proof (jurassic_fi `contribute`, 21,368 CU on mainnet) is the final check,
never the debugger.

The IDL below is the jurassic_fi shape reduced to what these tests turn on: a root PDA
declared derivably in one instruction and self-referentially in another, a child PDA that
seeds on it, a pinned program account, and a two-argument instruction.
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.prepare_instruction import plan_accounts, prepare_instruction_result
from gecko.program_graph import build_program_graph

PROGRAM = "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm"
ADMIN = "6Dw1xBGXChPeS69hovvYMF2nmRxgdoA711TKuuAbN5rV"
BUYER = "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi"
SYSTEM = "11111111111111111111111111111111"

LAUNCH_SEEDS_DERIVABLE = [
    {"kind": "const", "value": [108, 97, 117, 110, 99, 104]},
    {"kind": "account", "path": "admin"},
    {"kind": "arg", "path": "params.launch_id"},
]
LAUNCH_SEEDS_SELF = [
    {"kind": "const", "value": [108, 97, 117, 110, 99, 104]},
    {"kind": "account", "path": "launch.admin", "account": "Launch"},
    {"kind": "account", "path": "launch.launch_id", "account": "Launch"},
]

IDL: dict[str, Any] = {
    "address": PROGRAM,
    "metadata": {"spec": "0.1.0"},
    "instructions": [
        {
            "name": "initialize_launch",
            "args": [{"name": "params", "type": {"defined": "InitParams"}}],
            "accounts": [
                {"name": "admin", "signer": True, "writable": True},
                {
                    "name": "launch",
                    "writable": True,
                    "pda": {"seeds": LAUNCH_SEEDS_DERIVABLE},
                },
                {"name": "system_program", "address": SYSTEM},
            ],
        },
        {
            "name": "contribute",
            "args": [
                {"name": "requested_amount", "type": "u64"},
                {"name": "min_accepted_amount", "type": "u64"},
            ],
            "accounts": [
                {"name": "contributor", "signer": True, "writable": True},
                {
                    "name": "launch",
                    "writable": True,
                    "pda": {"seeds": LAUNCH_SEEDS_SELF},
                },
                {
                    "name": "user_position",
                    "writable": True,
                    "pda": {
                        "seeds": [
                            {"kind": "const", "value": list(b"user_position")},
                            {"kind": "account", "path": "launch"},
                            {"kind": "account", "path": "contributor"},
                        ]
                    },
                },
                {"name": "system_program", "address": SYSTEM},
            ],
        },
    ],
    "accounts": [{"name": "Launch", "discriminator": [1, 2, 3, 4, 5, 6, 7, 8]}],
    "types": [
        {
            "name": "InitParams",
            "type": {
                "kind": "struct",
                "fields": [{"name": "launch_id", "type": "u64"}],
            },
        },
        {
            "name": "Launch",
            "type": {
                "kind": "struct",
                "fields": [
                    {"name": "admin", "type": "pubkey"},
                    {"name": "launch_id", "type": "u64"},
                ],
            },
        },
    ],
}

MINT_FOR_ORDER = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

VALUES = {
    "contributor": BUYER,
    "admin": ADMIN,
    "launch_id": 100,
    "params.launch_id": 100,
    "requested_amount": 100_000,
    "min_accepted_amount": 100_000,
}


def idl_fetch(_program_id: str) -> dict[str, Any]:
    return IDL


class RecordingBuilder:
    """A builder that records the plan it was handed and returns fixed bytes."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "AQAAtransaction"


def ok_rpc(_url: str, _method: str, _params: list[Any]) -> dict[str, Any]:
    return {"result": {"value": {"err": None, "unitsConsumed": 21368}}}


def reverting_rpc(_url: str, _method: str, _params: list[Any]) -> dict[str, Any]:
    return {
        "result": {
            "value": {
                "err": {"InstructionError": [0, {"Custom": 6031}]},
                "logs": ["Program log: AcceptedAmountBelowMinimum"],
            }
        }
    }


# ------------------------------------------------------------------- the happy path


def test_it_derives_the_pdas_and_hands_the_builder_a_complete_plan() -> None:
    builder = RecordingBuilder()
    result = prepare_instruction_result(
        {
            "program_id": PROGRAM,
            "instruction": "contribute",
            "payer": BUYER,
            "values": VALUES,
        },
        idl_fetch=idl_fetch,
        build_call=builder,
        rpc_call=ok_rpc,
        rpc_url="https://rpc.test",
    )

    assert result["refused"] is False
    assert result["signed"] is False, "this path must never claim to have signed"
    assert result["transaction_base64"] == "AQAAtransaction"
    assert result["simulation"]["compute_units"] == 21368

    handed = builder.calls[0]["accounts"]
    assert set(handed) == {"contributor", "launch", "user_position", "system_program"}
    # the child seeds on the root, so the root must already be resolved when it derives
    assert result["derivation_order"][0] == "launch"


def test_every_account_says_how_it_got_its_address() -> None:
    """The provenance an agent needs to decide how much to trust the call — and the field
    an A2A capability card would carry. `supplied` is the caller's claim and the only one
    nobody verified; conflating it with `derived` would hide exactly that."""
    builder = RecordingBuilder()
    result = prepare_instruction_result(
        {
            "program_id": PROGRAM,
            "instruction": "contribute",
            "payer": BUYER,
            "values": VALUES,
        },
        idl_fetch=idl_fetch,
        build_call=builder,
    )
    origins = {o["account"]: o["origin"] for o in result["account_origins"]}
    assert origins["system_program"] == "pinned"
    assert origins["contributor"] == "supplied"
    assert origins["launch"] == "derived"
    assert origins["user_position"] == "derived"


def test_a_pinned_account_is_never_asked_of_the_caller() -> None:
    """The IDL fixes the system program. Asking a caller for it is how a flow ends up
    parameterising something the program already decided."""
    graph = build_program_graph(idl=IDL, program_id=PROGRAM)
    resolved, _, missing = plan_accounts(graph, "contribute", VALUES)
    assert resolved["system_program"] == SYSTEM
    assert not missing


# ------------------------------------------------------------------------ refusals


def test_a_missing_argument_is_named_with_its_type() -> None:
    """The live case this exists for: `contribute` takes a requested amount AND a floor,
    and an agent that supplies only the first has not expressed the intent it has."""
    result = prepare_instruction_result(
        {
            "program_id": PROGRAM,
            "instruction": "contribute",
            "payer": BUYER,
            "values": {k: v for k, v in VALUES.items() if k != "min_accepted_amount"},
        },
        idl_fetch=idl_fetch,
        build_call=RecordingBuilder(),
    )
    assert result["code"] == "argument-missing"
    assert result["missing_arguments"] == [
        {"name": "min_accepted_amount", "type": "u64"}
    ]


def test_an_unknown_instruction_lists_the_real_ones() -> None:
    result = prepare_instruction_result(
        {"program_id": PROGRAM, "instruction": "withdraw_everything", "payer": BUYER},
        idl_fetch=idl_fetch,
        build_call=RecordingBuilder(),
    )
    assert result["code"] == "instruction-unknown"
    assert set(result["available"]) == {"initialize_launch", "contribute"}


def test_an_underivable_account_refuses_the_WHOLE_call() -> None:
    """A partial plan is not a smaller answer, it is a wrong one: the transaction fails at
    signing time far from here, or worse, carries a well-formed wrong address and lands."""
    builder = RecordingBuilder()
    thin = {
        k: v
        for k, v in VALUES.items()
        if k not in ("admin", "launch_id", "params.launch_id")
    }
    result = prepare_instruction_result(
        {
            "program_id": PROGRAM,
            "instruction": "contribute",
            "payer": BUYER,
            "values": thin,
        },
        idl_fetch=idl_fetch,
        build_call=builder,
    )
    assert result["code"] == "accounts-unresolved"
    assert builder.calls == [], "the builder must not be reached with a partial plan"
    named = {m["account"] for m in result["missing_accounts"]}
    assert "launch" in named
    missing_launch = next(
        m for m in result["missing_accounts"] if m["account"] == "launch"
    )
    assert missing_launch["needs"], "a refusal a caller cannot act on is a shrug"


def test_a_reverting_simulation_hands_over_no_bytes() -> None:
    result = prepare_instruction_result(
        {
            "program_id": PROGRAM,
            "instruction": "contribute",
            "payer": BUYER,
            "values": VALUES,
        },
        idl_fetch=idl_fetch,
        build_call=RecordingBuilder(),
        rpc_call=reverting_rpc,
        rpc_url="https://rpc.test",
    )
    assert result["code"] == "simulation-reverted"
    assert "transaction_base64" not in result, (
        "a refused call must carry no transaction"
    )
    assert result["error"] == {"InstructionError": [0, {"Custom": 6031}]}


def test_a_catalogue_that_cannot_resolve_the_program_is_a_refusal_not_a_crash() -> None:
    def failing(_program_id: str) -> dict[str, Any]:
        raise ConnectionError("catalogue unreachable")

    result = prepare_instruction_result(
        {"program_id": PROGRAM, "instruction": "contribute", "payer": BUYER},
        idl_fetch=failing,
        build_call=RecordingBuilder(),
    )
    assert result["code"] == "program-unknown"
    assert "ConnectionError" in result["reason"]


@pytest.mark.parametrize("program_id", ["", "   "])
def test_no_program_no_call(program_id: str) -> None:
    result = prepare_instruction_result(
        {"program_id": program_id, "instruction": "contribute"},
        idl_fetch=idl_fetch,
        build_call=RecordingBuilder(),
    )
    assert result["code"] == "program-unknown"


# ---------------------------------------- IDL order is not dependency order


def test_a_pinned_account_listed_AFTER_the_pda_that_seeds_on_it_still_resolves() -> (
    None
):
    """Found end-to-end on jurassic_fi, and it is a one-line ordering mistake.

    `contribute` lists `payment_vault` at index 5 and `token_program` at index 6, and the
    vault's associated-token recipe seeds on the token program. Walking accounts in IDL
    order tried to derive the vault while its own seed was still unresolved, and reported
    a missing binding for an account the instruction PINS two slots later.

    The graph already computes `derivation_order`; the fix is to use it. Pinned and
    supplied accounts resolve first, then PDAs derive in dependency order.
    """
    token_program = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    idl: dict[str, Any] = {
        "address": PROGRAM,
        "metadata": {"spec": "0.1.0"},
        "instructions": [
            {
                "name": "contribute",
                "args": [],
                "accounts": [
                    {"name": "contributor", "signer": True, "writable": True},
                    {"name": "mint"},
                    # the vault comes BEFORE the program it seeds on — the real IDL's order
                    {
                        "name": "vault",
                        "writable": True,
                        "pda": {
                            "seeds": [
                                {"kind": "account", "path": "contributor"},
                                {"kind": "account", "path": "token_program"},
                                {"kind": "account", "path": "mint"},
                            ],
                        },
                    },
                    {"name": "token_program", "address": token_program},
                ],
            }
        ],
        "accounts": [],
        "types": [],
    }
    graph = build_program_graph(idl=idl, program_id=PROGRAM)
    resolved, origins, missing = plan_accounts(
        graph, "contribute", {"mint": MINT_FOR_ORDER}, payer=BUYER
    )

    assert missing == [], f"nothing should be missing: {missing}"
    assert resolved["token_program"] == token_program
    assert "vault" in resolved, "the vault must derive from the pinned token program"
    origin_of = {o["account"]: o["origin"] for o in origins}
    assert origin_of["vault"] == "derived"
    assert origin_of["token_program"] == "pinned"


# ------------------------------------------- the spelling that cost a round trip


def test_the_input_name_find_start_advertises_binds_the_dotted_seed() -> None:
    """`find_start` names the input `launch_id`; the recipe's seed is `params.launch_id`,
    because Anchor writes an argument-struct field as a dotted path. A live agent supplied
    exactly what it had been told to supply and was refused for a missing binding — a full
    round trip lost to two spellings of one value.
    """
    graph = build_program_graph(idl=IDL, program_id=PROGRAM)
    short = {k: v for k, v in VALUES.items() if k != "params.launch_id"}

    resolved, origins, missing = plan_accounts(graph, "contribute", short, payer=BUYER)

    assert missing == [], f"nothing should be missing: {missing}"
    # and it derives the SAME address the dotted spelling does — an alias, not a fallback
    dotted, _origins, _missing = plan_accounts(
        graph,
        "contribute",
        {k: v for k, v in VALUES.items() if k != "launch_id"},
        payer=BUYER,
    )
    assert resolved["launch"] == dotted["launch"]

    aliases = {o["account"]: o.get("aliased_seeds") for o in origins}
    assert aliases["launch"] == {"params.launch_id": "launch_id"}, (
        "an accommodation the caller cannot see is one they cannot check"
    )


def test_an_ambiguous_short_name_binds_nothing() -> None:
    """Two dotted seeds sharing a last segment have no unambiguous short name, and
    substituting a value under an ambiguous one is a guess — which derives a real,
    correctly formatted, wrong address."""
    from gecko.pda import ConstantPdaSeedNode, PdaNode, VariablePdaSeedNode
    from gecko.prepare_instruction import seed_aliases

    node = PdaNode(
        name="pool",
        seeds=(
            ConstantPdaSeedNode(b"pool", "utf8"),
            VariablePdaSeedNode("a.id", source="argument", encoding="le", width=8),
            VariablePdaSeedNode("b.id", source="argument", encoding="le", width=8),
        ),
        program_id=PROGRAM,
    )

    assert seed_aliases(node, {"id": 7}) == {}


def test_the_mirror_spelling_binds_too() -> None:
    """The caller holding `params.launch_id` for a recipe spelling it `launch_id` is the
    same mismatch read from the other end."""
    from gecko.pda import ConstantPdaSeedNode, PdaNode, VariablePdaSeedNode
    from gecko.prepare_instruction import seed_aliases

    node = PdaNode(
        name="launch",
        seeds=(
            ConstantPdaSeedNode(b"launch", "utf8"),
            VariablePdaSeedNode("launch_id", source="argument", encoding="le", width=8),
        ),
        program_id=PROGRAM,
    )

    assert seed_aliases(node, {"params.launch_id": 100}) == {
        "launch_id": ("params.launch_id", 100)
    }
