"""Reading the token program that owns a mint — offline, and fail-closed at every edge.

The property under test is one sentence: this value is READ or it is UNKNOWN. There is no
third path where a plausible answer is manufactured, because the whole reason the field
exists is that the plausible answer ("classic SPL, like almost everything") is exactly the
one that makes a look-alike asset invisible.
"""

from __future__ import annotations

from typing import Any

from gecko.rpc import RpcError
from gecko.token_program import (
    CLASSIC_SPL_TOKEN_PROGRAM,
    TOKEN_2022_PROGRAM,
    classify_token_program,
    read_mint_token_programs,
    unknown_token_program,
)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"


class FakeNode:
    """Answers `getMultipleAccounts` from a mint -> owner map. Absent mint = null entry."""

    def __init__(self, owners: dict[str, str | None]) -> None:
        self.owners = owners
        self.batches: list[list[str]] = []

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        assert method == "getMultipleAccounts"
        self.batches.append(list(params[0]))
        return {
            "result": {
                "value": [
                    None if self.owners.get(m) is None else {"owner": self.owners[m]}
                    for m in params[0]
                ]
            }
        }


def test_the_two_token_programs_are_named_and_anything_else_keeps_its_address() -> None:
    assert classify_token_program(CLASSIC_SPL_TOKEN_PROGRAM) == "classic-spl-token"
    assert classify_token_program(TOKEN_2022_PROGRAM) == "token-2022"
    # An unrecognised owner is reported as itself. Naming it something friendly would be
    # a claim we cannot make; dropping it would hide the strangest case of all.
    assert classify_token_program("So11111111111111111111111111111111111111112") == (
        "So11111111111111111111111111111111111111112"
    )


def test_look_alike_mints_resolve_to_different_programs() -> None:
    node = FakeNode({USDC: CLASSIC_SPL_TOKEN_PROGRAM, USDG: TOKEN_2022_PROGRAM})

    read = read_mint_token_programs([USDC, USDG], rpc_url="http://node", rpc_call=node)

    assert read[USDC].name == "classic-spl-token"
    assert read[USDG].name == "token-2022"
    assert read[USDC].known and read[USDC].recognised


def test_repeated_mints_are_read_once() -> None:
    node = FakeNode({USDC: CLASSIC_SPL_TOKEN_PROGRAM})

    read = read_mint_token_programs(
        [USDC, USDC, USDC], rpc_url="http://node", rpc_call=node
    )

    assert node.batches == [[USDC]]
    assert read[USDC].address == CLASSIC_SPL_TOKEN_PROGRAM


def test_more_mints_than_one_batch_are_chunked_and_all_answered() -> None:
    mints = [f"mint{index:03d}" for index in range(105)]
    node = FakeNode({mint: CLASSIC_SPL_TOKEN_PROGRAM for mint in mints})

    read = read_mint_token_programs(mints, rpc_url="http://node", rpc_call=node)

    assert [len(batch) for batch in node.batches] == [100, 5]
    assert set(read) == set(mints)


def test_a_node_answer_of_the_wrong_length_is_unknown_not_misaligned() -> None:
    """The dangerous shape: a short `value` list zipped against the mints would attribute
    one mint's owner to another. Refuse the batch instead."""

    def short(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        return {"result": {"value": [{"owner": CLASSIC_SPL_TOKEN_PROGRAM}]}}

    read = read_mint_token_programs([USDC, USDG], rpc_url="http://node", rpc_call=short)

    assert [entry.name for entry in read.values()] == ["unknown", "unknown"]
    assert all(entry.address is None for entry in read.values())


def test_an_owner_that_is_not_a_string_is_unknown() -> None:
    def junk(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        return {"result": {"value": [{"owner": 7}]}}

    read = read_mint_token_programs([USDC], rpc_url="http://node", rpc_call=junk)

    assert read[USDC].name == "unknown"
    assert "no owner" in (read[USDC].reason or "")


def test_a_transport_failure_is_reported_by_class_only() -> None:
    def boom(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        raise RpcError("JSON-RPC getMultipleAccounts failed: code=-32603")

    read = read_mint_token_programs([USDC], rpc_url="http://node", rpc_call=boom)

    assert read[USDC].known is False
    assert "RpcError" in (read[USDC].reason or "")


def test_beyond_the_cap_a_mint_says_it_was_not_read() -> None:
    mints = [f"mint{index:03d}" for index in range(130)]
    node = FakeNode({mint: CLASSIC_SPL_TOKEN_PROGRAM for mint in mints})

    read = read_mint_token_programs(mints, rpc_url="http://node", rpc_call=node)

    assert sum(1 for entry in read.values() if entry.known) == 128
    unread = [entry for entry in read.values() if not entry.known]
    assert len(unread) == 2
    assert all("not read" in (entry.reason or "") for entry in unread)


def test_the_unknown_value_is_one_spelling_everywhere() -> None:
    entry = unknown_token_program(USDC, "because the node said nothing")

    assert entry.field() == {
        "address": None,
        "name": "unknown",
        "read": False,
        "recognised": False,
        "reason": "because the node said nothing",
    }
    assert entry.recognised is False
