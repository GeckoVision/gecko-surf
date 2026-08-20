"""Where a missing seed value can be read from — the hop between two of our own tools.

`prepare_instruction` refuses `launch` with `caller_must_supply=('admin', 'params.launch_id')`
and stops there. `read_accounts(account_type="Launch")` returns exactly `{admin, launch_id}`,
verified by re-derivation. Two tools, one holding what the other needs, and no path between
them — the most repeated finding of the week, reported by two independent agents and then
found again a layer deeper.

Measured across 196 cached catalogue IDLs: 181 PDA accounts need seed values, and
`read_accounts` could supply EVERY value for 121 of them (66.9%).

This names the source. It does NOT fetch the value and it does not choose an instance —
a resolved address decides who gets paid, and picking "the only one" is the drain. The
caller makes one more call, with the tool and the arguments spelled out.
"""

from __future__ import annotations

import json

from gecko.value_sources import value_sources

IDL = {
    "accounts": [
        {"name": "Launch", "discriminator": [144, 51, 51, 163, 206, 85, 213, 38]},
    ],
    "types": [
        {
            "name": "Launch",
            "type": {
                "kind": "struct",
                "fields": [
                    {"name": "launch_id", "type": "u64"},
                    {"name": "admin", "type": "pubkey"},
                ],
            },
        }
    ],
    "instructions": [
        {
            "name": "contribute",
            "args": [],
            "accounts": [
                {
                    "name": "launch",
                    "pda": {
                        "seeds": [
                            {"kind": "const", "value": [108, 97, 117, 110, 99, 104]},
                            {
                                "kind": "account",
                                "path": "launch.admin",
                                "account": "Launch",
                            },
                            {
                                "kind": "account",
                                "path": "launch.launch_id",
                                "account": "Launch",
                            },
                        ]
                    },
                }
            ],
        },
        {
            "name": "initialize_launch",
            "args": [{"name": "params", "type": "InitializeLaunchParams"}],
            "accounts": [{"name": "launch", "pda": {"seeds": []}}],
        },
    ],
}

PROGRAM = "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm"


def test_a_missing_seed_value_names_the_account_it_can_be_read_from() -> None:
    found = value_sources(
        IDL, PROGRAM, instruction="contribute", needed=("admin", "params.launch_id")
    )

    assert set(found) == {"admin", "params.launch_id"}
    assert found["admin"]["account_type"] == "Launch"
    assert found["admin"]["field"] == "admin"
    assert found["admin"]["tool"] == "read_accounts"
    # the dotted spelling resolves to the bare field it actually reads
    assert found["params.launch_id"]["field"] == "launch_id"


def test_the_hint_is_a_source_not_a_value() -> None:
    """It must never carry an address or an amount. Naming where to look is safe;
    supplying the value is a binding decision that belongs to the caller."""
    found = value_sources(IDL, PROGRAM, instruction="contribute", needed=("admin",))

    hint = found["admin"]
    assert set(hint) == {"tool", "arguments", "account_type", "field", "note"}
    # the arguments must ASK for the field: read_accounts returns only its witness
    # fields by default, so a hint that omits `fields` sends the caller to a payload
    # without the value it just named.
    assert hint["arguments"] == {
        "program_id": PROGRAM,
        "account_type": "Launch",
        "fields": ["admin"],
    }
    assert "choose" in hint["note"].lower() or "which" in hint["note"].lower()


def test_a_value_no_witnessed_account_stores_gets_no_hint() -> None:
    """No hint is better than a wrong one. `requested_amount` is the caller's own number
    and lives in no account."""
    found = value_sources(
        IDL, PROGRAM, instruction="contribute", needed=("requested_amount",)
    )

    assert found == {}


def test_an_instruction_that_CREATES_the_account_gets_no_hint() -> None:
    """You cannot read a Launch to build the call that creates it. Measured earlier as a
    real false positive of name matching: `initialize.authority <- Distributor`."""
    found = value_sources(
        IDL, PROGRAM, instruction="initialize_launch", needed=("admin",)
    )

    assert found == {}


def test_a_type_with_no_witness_is_not_offered() -> None:
    """`read_accounts` refuses 75.9% of account types. Pointing a caller at a tool that
    will refuse them is worse than saying nothing."""
    unwitnessable = {
        "accounts": [{"name": "Thing"}],  # no discriminator -> no witness
        "types": IDL["types"],
        "instructions": IDL["instructions"],
    }

    assert (
        value_sources(
            unwitnessable, PROGRAM, instruction="contribute", needed=("admin",)
        )
        == {}
    )


def test_an_account_this_instruction_derives_is_never_offered_as_a_read() -> None:
    """`user_position` is seeded on `launch`, so `launch` appears as a needed VALUE — and
    the plan derives it as soon as its own seeds are known.

    Offering "read `UserPosition.launch` to get `launch`" sends the caller backwards
    through the chain being resolved. Found by walking the hops as an agent would: a name
    match cannot tell a seed value from an account the plan already owns.
    """
    idl = json.loads(json.dumps(IDL))
    idl["accounts"].append(
        {"name": "UserPosition", "discriminator": [251, 248, 1, 2, 3, 4, 5, 6]}
    )
    idl["types"].append(
        {
            "name": "UserPosition",
            "type": {
                "kind": "struct",
                "fields": [
                    {"name": "launch", "type": "pubkey"},
                    {"name": "contributor", "type": "pubkey"},
                ],
            },
        }
    )
    idl["instructions"][0]["accounts"].append(
        {"name": "user_position", "pda": {"seeds": []}}
    )

    found = value_sources(
        idl, PROGRAM, instruction="contribute", needed=("launch", "admin")
    )

    assert "launch" not in found, "an account of this instruction is derived, not read"
    assert "admin" in found


def test_a_caller_supplied_slot_of_this_instruction_still_gets_a_hint() -> None:
    """The other side of the previous guard, and it was wrong first.

    Excluding EVERY account of the instruction silently dropped the hint for
    `payment_mint` — a readonly slot with no `pda` block, which the caller must supply and
    which `Launch` happens to store. Only accounts that declare a `pda` block are derived;
    the rest are exactly what a hint is for.
    """
    idl = json.loads(json.dumps(IDL))
    idl["types"][0]["type"]["fields"].append({"name": "payment_mint", "type": "pubkey"})
    idl["instructions"][0]["accounts"].append({"name": "payment_mint"})  # no pda block

    found = value_sources(
        idl, PROGRAM, instruction="contribute", needed=("payment_mint",)
    )

    assert found["payment_mint"]["account_type"] == "Launch"
    assert found["payment_mint"]["arguments"]["fields"] == ["payment_mint"]
