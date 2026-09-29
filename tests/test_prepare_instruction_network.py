"""prepare_instruction on a network other than the one the surface pins.

Measured 2026-09-28: the hosted `prepare_instruction` had no `network` argument, so a
devnet store `initialize` was simulated against the pinned MAINNET RPC and refused with
`simulation-reverted / AccountNotFound` — the accounts exist, on devnet.
`prepare_purchase` already took `network`; this is the same vocabulary, the same public
endpoints, the same refusal code.

Offline: the IDL is a literal, the builder records its plan, the RPC records its URL.
Nothing here signs or broadcasts.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from gecko.networks import APPROVABLE_NETWORKS, DEFAULT_RPC_URLS
from gecko.prepare_instruction import (
    PREPARE_INSTRUCTION_TOOL,
    prepare_instruction_result,
)
from gecko.providers.catalog_surface import OrquestraCatalogSurface
from gecko.simulate import BuiltTx

PROGRAM = "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm"
AUTHORITY = "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi"
SYSTEM = "11111111111111111111111111111111"

IDL: dict[str, Any] = {
    "address": PROGRAM,
    "metadata": {"spec": "0.1.0"},
    "instructions": [
        {
            "name": "initialize",
            "args": [],
            "accounts": [
                {"name": "authority", "signer": True, "writable": True},
                {"name": "system_program", "address": SYSTEM},
            ],
        }
    ],
}

ARGS = {"program_id": PROGRAM, "instruction": "initialize", "payer": AUTHORITY}


class RecordingRpc:
    """Answers every method and remembers which URL each call went to."""

    def __init__(self, err: Any = None) -> None:
        self.urls: list[str] = []
        self.err = err

    def __call__(self, url: str, method: str, _params: list[Any]) -> dict[str, Any]:
        self.urls.append(url)
        if method == "getLatestBlockhash":
            return {"result": {"value": {"blockhash": None}}}
        if method == "getBlockHeight":
            return {"result": 1}
        value: dict[str, Any] = {"err": self.err, "unitsConsumed": 4242}
        if self.err is not None:
            value["logs"] = ["Program log: AccountNotFound"]
        return {"result": {"value": value}}


class RecordingBuilder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, plan: Mapping[str, Any]) -> BuiltTx:
        self.calls.append(dict(plan))
        return BuiltTx(tx="AQAAtransaction", encoding="base64")


def _surface(rpc: RecordingRpc, builder: RecordingBuilder, **kw: Any) -> Any:
    return OrquestraCatalogSurface(
        purchase_rpc_call=rpc,
        instruction_seams=(lambda _pid: IDL, builder),
        **kw,
    )


def test_devnet_simulates_against_the_public_devnet_rpc() -> None:
    rpc, builder = RecordingRpc(), RecordingBuilder()
    out = _surface(rpc, builder).call_tool(
        "prepare_instruction", {**ARGS, "network": "devnet"}
    )
    assert out["refused"] is False, out
    assert out["network"] == "devnet"
    assert rpc.urls and set(rpc.urls) == {DEFAULT_RPC_URLS["devnet"]}


def test_omitting_network_is_unchanged() -> None:
    rpc, builder = RecordingRpc(), RecordingBuilder()
    surface = _surface(rpc, builder)
    out = surface.call_tool("prepare_instruction", dict(ARGS))
    assert out["refused"] is False, out
    assert set(rpc.urls) == {surface.instruction_rpc_url}
    assert out["network"] == "mainnet"


def test_naming_the_pinned_network_keeps_the_pinned_rpc() -> None:
    """A surface pinned to a private mainnet node keeps using it when the caller says
    `mainnet`; only a DIFFERENT network swaps to that network's public endpoint."""
    rpc, builder = RecordingRpc(), RecordingBuilder()
    pinned = "https://mainnet.private-node.example"
    out = _surface(rpc, builder, instruction_rpc_url=pinned).call_tool(
        "prepare_instruction", {**ARGS, "network": "mainnet"}
    )
    assert out["refused"] is False
    assert set(rpc.urls) == {pinned}


@pytest.mark.parametrize(
    "bad", ["mainnet-beta", "https://api.devnet.solana.com", "unknown", 3, "fork"]
)
def test_a_network_without_a_public_rpc_is_refused_before_anything_runs(
    bad: Any,
) -> None:
    rpc, builder = RecordingRpc(), RecordingBuilder()
    out = _surface(rpc, builder).call_tool(
        "prepare_instruction", {**ARGS, "network": bad}
    )
    assert out["refused"] is True
    assert out["code"] == "argument-invalid"
    assert rpc.urls == [] and builder.calls == []


def test_a_devnet_revert_says_which_network_refused() -> None:
    rpc, builder = RecordingRpc(err="AccountNotFound"), RecordingBuilder()
    out = prepare_instruction_result(
        {**ARGS, "network": "devnet"},
        idl_fetch=lambda _pid: IDL,
        build_call=builder,
        rpc_call=rpc,
        rpc_url="https://api.mainnet-beta.solana.com",
    )
    assert out["code"] == "simulation-reverted"
    assert out["network"] == "devnet"
    assert set(rpc.urls) == {DEFAULT_RPC_URLS["devnet"]}


def test_the_tool_offers_network_as_an_optional_closed_set() -> None:
    schema = PREPARE_INSTRUCTION_TOOL["inputSchema"]
    assert isinstance(schema, dict)
    network = schema["properties"]["network"]
    # the same approvable set prepare_purchase enumerates; `fork` is in it and refused
    assert network["enum"] == sorted(APPROVABLE_NETWORKS)
    assert "network" not in schema["required"]


def test_prepare_purchase_reads_the_same_endpoints() -> None:
    from gecko import prepare_purchase

    assert prepare_purchase.DEFAULT_RPC_URLS is DEFAULT_RPC_URLS


def test_a_library_caller_that_did_not_say_is_labelled_unknown() -> None:
    """A fork rehearsal hands its fork's URL and no network; calling that mainnet
    would be the fork-mistaken-for-mainnet confusion `gecko.networks` exists to stop."""
    rpc, builder = RecordingRpc(), RecordingBuilder()
    out = prepare_instruction_result(
        dict(ARGS),
        idl_fetch=lambda _pid: IDL,
        build_call=builder,
        rpc_call=rpc,
        rpc_url="http://127.0.0.1:8899",
    )
    assert out["refused"] is False
    assert out["network"] == "unknown"
    assert set(rpc.urls) == {"http://127.0.0.1:8899"}
