"""The read layer: name -> instance identifier, and every answer re-derived.

THE GAP THIS CLOSES, in a live agent's own words. Asked to "contribute 0.1 USDC to the
DEATON sale", it reported that "no tool anywhere in the surface could tell me WHICH
`admin` pubkey and `launch_id` correspond to the human-named DEATON sale... the actual
blocker — not a missing PDA-derivation capability (that part works well), but a missing
name -> instance-identifier lookup." It got past it by calling raw `getProgramAccounts`
and hex-dumping a 552-byte account by hand.

WHY A MEMCMP MATCH IS NOT THE ANSWER. Anyone can create a genuine account of a declared
type with their own admin, so "the discriminator matched" ties an account to a TYPE and
to nothing else. What ties it to the seeds a caller asked about is re-derivation: decode
the seed fields at their computed offsets, derive the PDA from them, and assert the
result IS the account's own address. A wrong offset decodes a wrong value, which derives
an address that does not match — so a bad read is self-refuting rather than plausible.

Every test here is offline: the transport is injected the way
``gecko.pda_testkit.verify_derivation`` injects it.
"""

from __future__ import annotations

import base64
import json
import struct
from typing import Any

import pytest

from gecko.pda import (
    ConstantPdaSeedNode,
    PdaNode,
    VariablePdaSeedNode,
    derive_pda,
)
from gecko.read_accounts import READ_ACCOUNTS_TOOL, read_accounts
from gecko.rpc import RpcError

PROGRAM = "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm"
ADMIN = "6Dw1xBGXChPeS69hovvYMF2nmRxgdoA711TKuuAbN5rV"
OTHER_ADMIN = "8ZJ1Y7wZQ8BvGaLTStbeRhPMyKGqiMcYyaMBtCJVsuVX"
RPC = "https://api.mainnet-beta.solana.com"

LAUNCH_DISCRIMINATOR = [144, 51, 51, 163, 206, 85, 213, 38]
USER_POSITION_DISCRIMINATOR = [251, 248, 209, 245, 83, 234, 17, 27]

_LAUNCH_SEEDS = [
    {"kind": "const", "value": list(b"launch")},
    {"kind": "account", "path": "launch.admin", "account": "Launch"},
    {"kind": "account", "path": "launch.launch_id", "account": "Launch"},
]

# jurassic_fi's real shape, trimmed to the fields these tests decode. `name` is a Borsh
# string and it is the whole point: "DEATON" is the handle a human gives, and the
# identifiers the caller actually needs (`admin`, `launch_id`) sit in front of it.
JURASSIC: dict[str, Any] = {
    "address": PROGRAM,
    "instructions": [
        {
            "name": "contribute",
            "accounts": [
                {"name": "launch", "pda": {"seeds": _LAUNCH_SEEDS}},
                {
                    "name": "user_position",
                    "pda": {
                        "seeds": [
                            {"kind": "const", "value": list(b"user_position")},
                            {"kind": "account", "path": "launch"},
                            {"kind": "account", "path": "contributor"},
                        ]
                    },
                },
                {"name": "contributor", "signer": True},
            ],
            "args": [],
        }
    ],
    "accounts": [
        {"name": "Launch", "discriminator": LAUNCH_DISCRIMINATOR},
        {"name": "UserPosition", "discriminator": USER_POSITION_DISCRIMINATOR},
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
                    {"name": "name", "type": "string"},
                    {"name": "symbol", "type": "string"},
                ],
            },
        },
        {
            "name": "UserPosition",
            "type": {
                "kind": "struct",
                "fields": [
                    {"name": "launch", "type": "pubkey"},
                    {"name": "contributor", "type": "pubkey"},
                    {"name": "contributed_amount", "type": "u64"},
                ],
            },
        },
    ],
}


# --- light fakes: bytes in, one injected transport out ---------------------------


def _b58_bytes(value: str) -> bytes:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = 0
    for char in value:
        number = number * 58 + alphabet.index(char)
    raw = number.to_bytes(32, "big")
    return raw


def launch_data(launch_id: int, admin: str, name: str) -> bytes:
    """The on-chain bytes of one Launch, laid out exactly as the IDL declares them."""
    return (
        bytes(LAUNCH_DISCRIMINATOR)
        + struct.pack("<Q", launch_id)
        + _b58_bytes(admin)
        + bytes(32)  # payment_mint
        + struct.pack("<I", len(name.encode()))
        + name.encode()
        + struct.pack("<I", 0)  # symbol: empty
    )


def launch_address(launch_id: int, admin: str) -> str:
    node = PdaNode(
        name="launch",
        seeds=(
            ConstantPdaSeedNode(b"launch", "utf8"),
            VariablePdaSeedNode("admin", source="account", encoding="pubkey"),
            VariablePdaSeedNode("launch_id", source="account", encoding="le", width=8),
        ),
        program_id=PROGRAM,
    )
    return derive_pda(node, {"admin": admin, "launch_id": launch_id}).address


def fake_rpc(
    accounts: list[tuple[str, bytes]], *, calls: list[Any] | None = None
) -> Any:
    """A ``getProgramAccounts`` that honours ``dataSlice`` and records what it was asked."""

    def call(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if calls is not None:
            calls.append((method, params))
        assert method == "getProgramAccounts"
        config = params[1] if len(params) > 1 else {}
        window = config.get("dataSlice") or {"offset": 0, "length": None}
        size = {f.get("dataSize") for f in config.get("filters", []) if "dataSize" in f}
        out = []
        for address, data in accounts:
            if size and len(data) not in size:
                continue
            start = int(window["offset"])
            end = start + int(window["length"]) if window["length"] else len(data)
            out.append(
                {
                    "pubkey": address,
                    "account": {
                        "data": [base64.b64encode(data[start:end]).decode(), "base64"],
                        "owner": PROGRAM,
                    },
                }
            )
        return {"result": out}

    return call


# --- the capability ---------------------------------------------------------------


def test_a_human_name_maps_to_the_identifiers_that_derive_the_address() -> None:
    """The whole report in one assertion: "DEATON" -> the `admin` + `launch_id` a caller
    needs, read off the chain instead of hex-dumped by hand."""
    address = launch_address(100, ADMIN)
    result = read_accounts(
        JURASSIC,
        PROGRAM,
        "Launch",
        fields=("launch_id", "admin", "name"),
        rpc_url=RPC,
        rpc_call=fake_rpc([(address, launch_data(100, ADMIN, "DEATON"))]),
    )

    assert result["refused"] is False
    (instance,) = result["instances"]
    assert instance["address"] == address
    assert instance["fields"] == {
        "launch_id": 100,
        "admin": ADMIN,
        "name": "DEATON",
    }


def test_the_address_is_re_derived_from_the_decoded_seeds_and_asserted() -> None:
    """A memcmp match is not proof — anyone can open a genuine Launch with their own
    admin. Only re-derivation ties an account to the seeds a caller asked about."""
    address = launch_address(100, ADMIN)
    result = read_accounts(
        JURASSIC,
        PROGRAM,
        "Launch",
        rpc_url=RPC,
        rpc_call=fake_rpc([(address, launch_data(100, ADMIN, "DEATON"))]),
    )

    (instance,) = result["instances"]
    assert instance["verified"] is True
    assert instance["rederived"] == address
    # the fields the derivation actually proved, named — never the whole decode
    assert set(instance["witnessed_fields"]) == {"admin", "launch_id"}


def test_an_account_that_does_not_re_derive_is_unverified_never_good() -> None:
    """A wrong offset decodes a wrong value, which derives an address that is not the one
    it was read from. That is the self-refutation the whole design rests on, so the
    failing account is reported — never dropped, and never returned among the good."""
    impostor = launch_address(999, OTHER_ADMIN)
    result = read_accounts(
        JURASSIC,
        PROGRAM,
        "Launch",
        rpc_url=RPC,
        # genuine Launch bytes sitting at an address those bytes do not derive
        rpc_call=fake_rpc([(impostor, launch_data(100, ADMIN, "DEATON"))]),
    )

    assert result["instances"] == []
    (bad,) = result["unverified"]
    assert bad["address"] == impostor
    assert bad["verified"] is False
    assert bad["rederived"] == launch_address(100, ADMIN)
    assert "re-deriv" in bad["why"]


def test_every_match_comes_back_and_nothing_is_chosen_for_the_caller() -> None:
    """ "There was only one, so it must be the one" is a drain. Several instances of a
    type is the normal case and the caller picks."""
    first = launch_address(100, ADMIN)
    second = launch_address(101, OTHER_ADMIN)
    result = read_accounts(
        JURASSIC,
        PROGRAM,
        "Launch",
        fields=("name",),
        rpc_url=RPC,
        rpc_call=fake_rpc(
            [
                (first, launch_data(100, ADMIN, "DEATON")),
                (second, launch_data(101, OTHER_ADMIN, "TRICERATOPS")),
            ]
        ),
    )

    assert [i["address"] for i in result["instances"]] == [first, second]
    assert result["counts"] == {"matched": 2, "verified": 2, "unverified": 0}
    # no "best", no "match", no "the one" — the response has no selection anywhere
    assert "select" in result["note"] or "choose" in result["note"]


def test_a_by_name_recipe_verifies_a_type_the_idl_does_not_dot() -> None:
    """UserPosition is seeded on plain account paths (`launch`, `contributor`) and stores
    both as fields of the same name. The binding is INFERRED, so it is labelled that way
    — and it still has to survive re-derivation, which is what makes inferring it safe."""
    launch = launch_address(100, ADMIN)
    node = PdaNode(
        name="user_position",
        seeds=(
            ConstantPdaSeedNode(b"user_position", "utf8"),
            VariablePdaSeedNode("launch", source="account", encoding="pubkey"),
            VariablePdaSeedNode("contributor", source="account", encoding="pubkey"),
        ),
        program_id=PROGRAM,
    )
    address = derive_pda(node, {"launch": launch, "contributor": ADMIN}).address
    data = (
        bytes(USER_POSITION_DISCRIMINATOR)
        + _b58_bytes(launch)
        + _b58_bytes(ADMIN)
        + struct.pack("<Q", 42)
    )

    result = read_accounts(
        JURASSIC,
        PROGRAM,
        "UserPosition",
        fields=("contributed_amount",),
        rpc_url=RPC,
        rpc_call=fake_rpc([(address, data)]),
    )

    (instance,) = result["instances"]
    assert instance["verified"] is True
    bases = {seed["basis"] for seed in result["verified_by"]["seeds"]}
    assert "field-name-match" in bases


def test_a_fixed_size_type_filters_on_datasize_too() -> None:
    """UserPosition is all fixed-width, so its size is known and worth filtering on. A
    type carrying a string (Launch) has no static size and must not pretend otherwise."""
    calls: list[Any] = []
    read_accounts(
        JURASSIC,
        PROGRAM,
        "UserPosition",
        rpc_url=RPC,
        rpc_call=fake_rpc([], calls=calls),
    )
    (_method, params) = calls[0]
    assert {"dataSize": 8 + 32 + 32 + 8} in params[1]["filters"]

    calls.clear()
    read_accounts(
        JURASSIC, PROGRAM, "Launch", rpc_url=RPC, rpc_call=fake_rpc([], calls=calls)
    )
    (_method, params) = calls[0]
    assert not any("dataSize" in f for f in params[1]["filters"])


def test_a_datasize_filter_that_empties_the_result_is_retried_and_reported() -> None:
    """An empty list that could mean "none exist" or "our size arithmetic is wrong" is
    the failure this whole module refuses. So a size-filtered miss is retried without the
    size, and a discrepancy is stated rather than returned as absence."""
    launch = launch_address(100, ADMIN)
    padded = (
        bytes(USER_POSITION_DISCRIMINATOR)
        + _b58_bytes(launch)
        + _b58_bytes(ADMIN)
        + struct.pack("<Q", 42)
        + bytes(64)  # reserved tail the IDL does not declare
    )
    node = PdaNode(
        name="user_position",
        seeds=(
            ConstantPdaSeedNode(b"user_position", "utf8"),
            VariablePdaSeedNode("launch", source="account", encoding="pubkey"),
            VariablePdaSeedNode("contributor", source="account", encoding="pubkey"),
        ),
        program_id=PROGRAM,
    )
    address = derive_pda(node, {"launch": launch, "contributor": ADMIN}).address

    result = read_accounts(
        JURASSIC,
        PROGRAM,
        "UserPosition",
        rpc_url=RPC,
        rpc_call=fake_rpc([(address, padded)]),
    )

    assert len(result["instances"]) == 1
    assert "dataSize" in result["size_note"]


# --- the refusals. A refusal that names which one it hit IS the deliverable --------


def test_a_legacy_idl_with_no_discriminator_refuses_and_says_so() -> None:
    """48% of catalogue programs ship legacy IDLs with no account discriminators; their
    accounts cannot be found by memcmp at all."""
    legacy = {**JURASSIC, "accounts": [{"name": "Launch"}]}
    result = read_accounts(
        legacy, PROGRAM, "Launch", rpc_url=RPC, rpc_call=fake_rpc([])
    )

    assert result["refused"] is True
    assert result["code"] == "no-discriminator"
    assert "discriminator" in result["reason"]


def test_get_program_accounts_disabled_refuses_and_names_the_rpc() -> None:
    """The commonest failure on a public endpoint, and the one most easily mistaken for
    "there are none"."""

    def disabled(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        raise RpcError(
            "JSON-RPC getProgramAccounts failed: code=-32601 message='Method not found'"
        )

    result = read_accounts(JURASSIC, PROGRAM, "Launch", rpc_url=RPC, rpc_call=disabled)

    assert result["code"] == "rpc-method-unavailable"
    assert "getProgramAccounts" in result["reason"]


def test_an_uncomputable_offset_refuses_rather_than_guessing_one() -> None:
    """`symbol` sits behind `name`, a Borsh string, so its offset depends on runtime
    content. A plausible offset decodes a real, well-formed, wrong value."""
    result = read_accounts(
        JURASSIC,
        PROGRAM,
        "Launch",
        fields=("symbol",),
        rpc_url=RPC,
        rpc_call=fake_rpc([]),
    )

    assert result["code"] == "layout-uncomputable"
    assert "symbol" in result["reason"]


def test_a_type_nothing_can_re_derive_refuses_instead_of_listing_unproven_addresses() -> (
    None
):
    """Without a recipe there is no witness, and a list of addresses nobody can tie to
    anything is exactly the unproven answer this module exists to avoid."""
    idl = {
        **JURASSIC,
        "instructions": [],
        "accounts": [{"name": "Launch", "discriminator": LAUNCH_DISCRIMINATOR}],
    }
    result = read_accounts(idl, PROGRAM, "Launch", rpc_url=RPC, rpc_call=fake_rpc([]))

    assert result["code"] == "no-verification-recipe"


def test_an_unknown_account_type_names_the_ones_that_exist() -> None:
    result = read_accounts(
        JURASSIC, PROGRAM, "Sale", rpc_url=RPC, rpc_call=fake_rpc([])
    )

    assert result["code"] == "account-type-unknown"
    assert result["available"] == ["Launch", "UserPosition"]


def test_nothing_but_the_requested_fields_leaves_the_module() -> None:
    """Control plane (invariant #1): public chain state may be read and returned, the
    account blob may not. `dataSlice` means we never even fetch the rest of it."""
    address = launch_address(100, ADMIN)
    calls: list[Any] = []
    result = read_accounts(
        JURASSIC,
        PROGRAM,
        "Launch",
        fields=("launch_id", "admin"),
        rpc_url=RPC,
        rpc_call=fake_rpc([(address, launch_data(100, ADMIN, "DEATON"))], calls=calls),
    )

    rendered = json.dumps(result)
    assert "DEATON" not in rendered  # not asked for, so not read out
    assert "lamports" not in rendered and "base64" not in rendered
    # only the prefix the requested fields need is ever pulled off the wire
    assert calls[0][1][1]["dataSlice"] == {"offset": 0, "length": 48}


def test_the_tool_definition_tells_an_agent_it_must_choose() -> None:
    description = READ_ACCOUNTS_TOOL["description"]
    assert isinstance(description, str)
    assert "choose" in description or "chooses" in description
    schema = READ_ACCOUNTS_TOOL["inputSchema"]
    assert isinstance(schema, dict)
    assert schema["required"] == ["program_id", "account_type"]


@pytest.mark.parametrize("scheme", ["file:///etc/passwd", "ftp://example.com"])
def test_a_non_http_rpc_is_refused_before_anything_is_read(scheme: str) -> None:
    result = read_accounts(
        JURASSIC, PROGRAM, "Launch", rpc_url=scheme, rpc_call=fake_rpc([])
    )

    assert result["code"] == "rpc-failed"


# --- the tool boundary: the same engine, plus the one thing context forces ---------


def test_the_tool_routes_through_the_injected_catalogue_seam() -> None:
    """`read_accounts_result` takes the SAME `idl_fetch` seam `prepare_instruction` takes,
    so the surface holds one catalogue client and this path is falsifiable with no
    network at all."""
    from gecko.read_accounts import read_accounts_result

    address = launch_address(100, ADMIN)
    result = read_accounts_result(
        {"program_id": PROGRAM, "account_type": "Launch", "fields": ["name"]},
        idl_fetch=lambda _program_id: JURASSIC,
        rpc_url=RPC,
        rpc_call=fake_rpc([(address, launch_data(100, ADMIN, "DEATON"))]),
    )

    assert result["instances"][0]["fields"] == {"name": "DEATON"}


def test_a_catalogue_that_cannot_resolve_the_program_refuses_rather_than_raising() -> (
    None
):
    from gecko.read_accounts import read_accounts_result

    def failing(_program_id: str) -> dict[str, Any]:
        raise RuntimeError("no such project")

    result = read_accounts_result(
        {"program_id": PROGRAM, "account_type": "Launch"},
        idl_fetch=failing,
        rpc_url=RPC,
    )

    assert result["refused"] is True
    assert result["code"] == "argument-invalid"


def test_too_many_instances_refuses_with_the_count_instead_of_a_slice() -> None:
    """A truncated list IS a selection, and this module never selects. So the tool
    boundary — where the constraint is a context budget, not correctness — refuses and
    says how many there are."""
    from gecko.read_accounts import MAX_TOOL_INSTANCES, read_accounts_result

    crowd = [
        (launch_address(n, ADMIN), launch_data(n, ADMIN, f"SALE-{n}"))
        for n in range(MAX_TOOL_INSTANCES + 1)
    ]
    result = read_accounts_result(
        {"program_id": PROGRAM, "account_type": "Launch"},
        idl_fetch=lambda _program_id: JURASSIC,
        rpc_url=RPC,
        rpc_call=fake_rpc(crowd),
    )

    assert result["code"] == "too-many-instances"
    assert result["counts"]["matched"] == MAX_TOOL_INSTANCES + 1
    assert "instances" not in result  # none of them, rather than some of them


def test_the_library_itself_is_uncapped() -> None:
    """A script that wants all of them gets all of them — the cap is a rendering
    constraint at the agent boundary, not a claim about the chain."""
    from gecko.read_accounts import MAX_TOOL_INSTANCES

    crowd = [
        (launch_address(n, ADMIN), launch_data(n, ADMIN, f"SALE-{n}"))
        for n in range(MAX_TOOL_INSTANCES + 1)
    ]
    result = read_accounts(
        JURASSIC, PROGRAM, "Launch", rpc_url=RPC, rpc_call=fake_rpc(crowd)
    )

    assert len(result["instances"]) == MAX_TOOL_INSTANCES + 1


def test_the_surface_serves_it_and_never_takes_an_rpc_from_the_caller() -> None:
    """A caller-supplied RPC on an unauthenticated public mount is an SSRF proxy, which
    is why the URL is a field of the surface and not an argument of the tool."""
    from gecko.providers.catalog_surface import OrquestraCatalogSurface
    from gecko.read_accounts import READ_ACCOUNTS_TOOL

    assert "rpc_url" not in READ_ACCOUNTS_TOOL["inputSchema"]["properties"]  # type: ignore[index]

    address = launch_address(100, ADMIN)
    surface = OrquestraCatalogSurface(
        purchase_rpc_call=fake_rpc([(address, launch_data(100, ADMIN, "DEATON"))]),
        instruction_seams=(lambda _program_id: JURASSIC, lambda **_kwargs: ""),
    )
    out = surface.call_tool(
        "read_accounts", {"program_id": PROGRAM, "account_type": "Launch"}
    )

    assert out["instances"][0]["address"] == address


def test_a_singleton_type_is_answered_with_its_address_not_a_lookup() -> None:
    """A type seeded on constants alone has exactly ONE instance, so enumerating it is
    the wrong question. Answering "no recipe" would send a caller looking for a lookup
    that cannot exist; answering with the address ends the question."""
    idl = {
        **JURASSIC,
        "instructions": [
            {
                "name": "initialize_config",
                "accounts": [
                    {
                        "name": "config",
                        "pda": {"seeds": [{"kind": "const", "value": list(b"config")}]},
                    }
                ],
                "args": [],
            }
        ],
        "accounts": [{"name": "Config", "discriminator": LAUNCH_DISCRIMINATOR}],
        "types": [
            {
                "name": "Config",
                "type": {
                    "kind": "struct",
                    "fields": [{"name": "authority", "type": "pubkey"}],
                },
            }
        ],
    }

    result = read_accounts(idl, PROGRAM, "Config", rpc_url=RPC, rpc_call=fake_rpc([]))

    assert result["code"] == "singleton-account"
    assert (
        result["address"]
        == derive_pda(
            PdaNode(
                name="config",
                seeds=(ConstantPdaSeedNode(b"config", "bytes"),),
                program_id=PROGRAM,
            ),
            {},
        ).address
    )


def test_two_different_recipes_for_one_type_refuse_rather_than_pick() -> None:
    """Choosing between them would be a guess, and the wrong choice reports every real
    account as unverified — a false negative that reads exactly like a false positive
    caught."""
    idl = {
        **JURASSIC,
        "instructions": [
            JURASSIC["instructions"][0],
            {
                "name": "settle",
                "accounts": [
                    {
                        "name": "launch",
                        "pda": {
                            "seeds": [
                                {"kind": "const", "value": list(b"sale")},
                                {
                                    "kind": "account",
                                    "path": "launch.admin",
                                    "account": "Launch",
                                },
                            ]
                        },
                    }
                ],
                "args": [],
            },
        ],
    }

    result = read_accounts(idl, PROGRAM, "Launch", rpc_url=RPC, rpc_call=fake_rpc([]))

    assert result["code"] == "ambiguous-verification-recipe"
    assert "contribute" in result["reason"] and "settle" in result["reason"]
