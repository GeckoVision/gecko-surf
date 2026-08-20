"""Where a field lives in an Anchor account, computed from the IDL rather than guessed.

Anchor IDL 0.1.0 seed entries carry the account TYPE alongside the path:

    {"kind": "account", "path": "launch.admin", "account": "Launch"}

We read `path` and throw `account` away, then report the seed as "runtime data". Measured
over 193 catalogue IDLs: 405 dotted account-kind seeds, **397 (98%) carry the `account`
key**, and **384 (95%) name a type that has an 8-byte discriminator**. For those, the IDL
hands us the memcmp filter and the decode target by name, with nothing inferred.

This module turns that into (discriminator, offset, width). The whole value of computing
it is that it can be WRONG in a detectable way: a bad offset decodes a bad seed, which
derives an address that will not match the account it was read from. Guessing an offset
has no such property, which is why `metadao_state.py` documents a hand-guessed offset
failing in exactly this shape.

So the rule here is refusal, not best-effort: if any field BEFORE the target is
variable-width — string, bytes, vec, option, or a fielded enum — the offset is not
computable and we say so. A plausible-looking offset is the failure mode this repo cares
about most, because the wrong value it decodes derives a real, valid, wrong address.
"""

from __future__ import annotations

import pytest

from gecko.idl_layout import LayoutError, account_discriminator, field_layout

# The real shape, from jurassic_fi's live IDL.
LAUNCH = {
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
                    {"name": "payment_mint", "type": "pubkey"},
                    {"name": "start_ts", "type": "i64"},
                ],
            },
        }
    ],
}


def test_the_offset_counts_from_after_the_discriminator() -> None:
    """Anchor puts an 8-byte discriminator first. An offset that forgets it is off by 8
    and decodes the neighbouring field — which is exactly the kind of wrong that still
    produces a well-formed value."""
    assert field_layout(LAUNCH, "Launch", "launch_id") == {
        "offset": 8,
        "width": 8,
        "type": "u64",
    }
    assert field_layout(LAUNCH, "Launch", "admin") == {
        "offset": 16,
        "width": 32,
        "type": "pubkey",
    }
    assert field_layout(LAUNCH, "Launch", "payment_mint")["offset"] == 48
    assert field_layout(LAUNCH, "Launch", "start_ts")["offset"] == 80


def test_the_discriminator_is_the_memcmp_filter_the_idl_already_names() -> None:
    assert account_discriminator(LAUNCH, "Launch") == bytes(
        [144, 51, 51, 163, 206, 85, 213, 38]
    )


@pytest.mark.parametrize(
    "variable",
    [
        "string",
        "bytes",
        {"vec": "u8"},
        {"option": "pubkey"},
    ],
)
def test_a_variable_width_field_before_the_target_refuses(variable: object) -> None:
    """The refusal that makes the rest trustworthy. Everything after a variable-width
    field sits at an offset that depends on runtime CONTENT, so no static answer exists."""
    idl = {
        "accounts": [{"name": "T", "discriminator": [1, 2, 3, 4, 5, 6, 7, 8]}],
        "types": [
            {
                "name": "T",
                "type": {
                    "kind": "struct",
                    "fields": [
                        {"name": "head", "type": variable},
                        {"name": "target", "type": "pubkey"},
                    ],
                },
            }
        ],
    }

    with pytest.raises(LayoutError, match="variable"):
        field_layout(idl, "T", "target")


def test_a_variable_width_field_AFTER_the_target_is_harmless() -> None:
    """Only what precedes the target moves it. Refusing here would throw away offsets we
    can compute exactly, and over-refusal is still a wrong answer."""
    idl = {
        "accounts": [{"name": "T", "discriminator": [1, 2, 3, 4, 5, 6, 7, 8]}],
        "types": [
            {
                "name": "T",
                "type": {
                    "kind": "struct",
                    "fields": [
                        {"name": "target", "type": "u64"},
                        {"name": "tail", "type": "string"},
                    ],
                },
            }
        ],
    }

    assert field_layout(idl, "T", "target")["offset"] == 8


def test_a_fixed_array_counts_its_full_width() -> None:
    idl = {
        "accounts": [{"name": "T", "discriminator": [1, 2, 3, 4, 5, 6, 7, 8]}],
        "types": [
            {
                "name": "T",
                "type": {
                    "kind": "struct",
                    "fields": [
                        {"name": "head", "type": {"array": ["u8", 32]}},
                        {"name": "target", "type": "u64"},
                    ],
                },
            }
        ],
    }

    assert field_layout(idl, "T", "target")["offset"] == 40


def test_an_unknown_type_refuses_rather_than_assuming_a_width() -> None:
    """A defaulted width is the jurassic defect in another costume: u64 read as u8 gives a
    different, perfectly valid address."""
    idl = {
        "accounts": [{"name": "T", "discriminator": [1, 2, 3, 4, 5, 6, 7, 8]}],
        "types": [
            {
                "name": "T",
                "type": {
                    "kind": "struct",
                    "fields": [
                        {"name": "head", "type": {"defined": {"name": "Mystery"}}},
                        {"name": "target", "type": "u64"},
                    ],
                },
            }
        ],
    }

    with pytest.raises(LayoutError):
        field_layout(idl, "T", "target")


def test_a_missing_type_or_field_refuses() -> None:
    with pytest.raises(LayoutError, match="Nope"):
        field_layout(LAUNCH, "Nope", "admin")
    with pytest.raises(LayoutError, match="nope"):
        field_layout(LAUNCH, "Launch", "nope")


def test_an_account_with_no_discriminator_refuses() -> None:
    """48% of catalogue programs ship legacy IDLs with no account discriminators. They
    cannot be enumerated by memcmp, and saying so is the honest answer."""
    idl = {"accounts": [{"name": "T"}], "types": LAUNCH["types"]}

    with pytest.raises(LayoutError, match="discriminator"):
        account_discriminator(idl, "T")


# --- the seed extractor stops discarding what the IDL told it ---------------------


def test_a_dotted_account_seed_carries_its_read_recipe() -> None:
    """The `account` key on an Anchor seed names the exact struct the field is read from:

        {"kind": "account", "path": "launch.admin", "account": "Launch"}

    We read `path` and threw `account` away, then said "runtime data" — discarding the one
    fact that turns an unresolvable seed into a mechanical read. 98% of dotted seeds in the
    catalogue carry it.

    The seed stays UNRESOLVABLE either way: a recipe for how to read a value is not the
    value, and the node must not become resolvable just because we know where to look.
    """
    from gecko.pda_extract import instruction_pdas

    instruction = {
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
                    ]
                },
            }
        ],
    }

    nodes = instruction_pdas(
        instruction,
        program_id="11111111111111111111111111111111",
        type_defs={},
        layout_idl=LAUNCH,
    )

    seed = nodes["launch"].seeds[1]
    assert seed.resolve == {
        "read": "launch",
        "account_type": "Launch",
        "field": "admin",
        "offset": 16,
        "width": 32,
        "type": "pubkey",
        "discriminator": [144, 51, 51, 163, 206, 85, 213, 38],
    }
    assert not nodes["launch"].resolvable, (
        "knowing where to read is not knowing the value"
    )


def test_an_uncomputable_layout_leaves_the_seed_exactly_as_before() -> None:
    """No recipe is better than a guessed one. When the offset cannot be computed the seed
    keeps its honest "runtime data" reason and carries no resolve block."""
    from gecko.pda_extract import instruction_pdas

    idl = {
        "accounts": [{"name": "T", "discriminator": [1, 2, 3, 4, 5, 6, 7, 8]}],
        "types": [
            {
                "name": "T",
                "type": {
                    "kind": "struct",
                    "fields": [
                        {"name": "head", "type": "string"},
                        {"name": "admin", "type": "pubkey"},
                    ],
                },
            }
        ],
    }
    instruction = {
        "name": "x",
        "args": [],
        "accounts": [
            {
                "name": "thing",
                "pda": {
                    "seeds": [
                        {"kind": "account", "path": "thing.admin", "account": "T"}
                    ]
                },
            }
        ],
    }

    nodes = instruction_pdas(
        instruction,
        program_id="11111111111111111111111111111111",
        type_defs={},
        layout_idl=idl,
    )

    assert nodes["thing"].seeds[0].resolve is None


def test_without_the_layout_idl_nothing_changes() -> None:
    """Additive: every existing caller passes no layout and must be unaffected."""
    from gecko.pda_extract import instruction_pdas

    instruction = {
        "name": "contribute",
        "args": [],
        "accounts": [
            {
                "name": "launch",
                "pda": {
                    "seeds": [
                        {"kind": "account", "path": "launch.admin", "account": "Launch"}
                    ]
                },
            }
        ],
    }

    nodes = instruction_pdas(
        instruction, program_id="11111111111111111111111111111111", type_defs={}
    )

    assert nodes["launch"].seeds[0].resolve is None
