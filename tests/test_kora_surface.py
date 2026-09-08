"""The Kora surface: the wire contract, and the boundary that keeps a fee payer safe.

Everything here is offline and free. The three wire facts asserted below were verified
against a running kora 2.2.0-beta.8 node on 2026-09-07 and appear in no Kora document,
which is exactly why they need a test: a spec recovered from source is only worth what its
claims survive.
"""

from __future__ import annotations

import json

import pytest

from gecko.access import public_session
from gecko.client import AgentApiClient
from gecko.kora_surface import (
    KORA_SPEC_PATH,
    KORA_WRITE_OP_IDS,
    KoraBoundaryError,
    build_kora_catalog_surface,
    build_kora_surface,
    resolve_write_tool_names,
)

BASE = "https://kora.example.com"

READ_OPS = (
    "getVersion",
    "liveness",
    "getBlockhash",
    "getSupportedTokens",
    "getPayerSigner",
    "getConfig",
    "estimateTransactionFee",
)


def _client() -> AgentApiClient:
    return AgentApiClient(str(KORA_SPEC_PATH), base_url=BASE, session=public_session())


def test_the_spec_kora_ships_comprehends_to_nothing() -> None:
    """The premise of this whole surface, pinned so it cannot quietly stop being true.

    Kora's own generator emits schemas and no operations. If a future Kora ships real
    paths, this test fails and the honest move is to ingest theirs and delete ours.
    """
    from pathlib import Path

    from gecko.ingest import extract_operations

    shipped = Path.home() / "PycharmProjects" / "Gecko" / "kora" / "openapi.json"
    if not shipped.is_file():
        pytest.skip("the kora clone is not on this machine")
    spec = json.loads(shipped.read_text())
    assert spec.get("paths") == {}, (
        "kora now publishes paths; re-check whether ours is still needed"
    )
    assert extract_operations(spec) == []


def test_every_method_kora_serves_is_callable() -> None:
    ops = {op.operation_id for op in _client().operations}
    assert ops == set(READ_OPS) | set(KORA_WRITE_OP_IDS)
    assert len(ops) == 10


@pytest.mark.parametrize("operation_id", READ_OPS + KORA_WRITE_OP_IDS)
def test_the_request_carries_a_complete_jsonrpc_envelope(operation_id: str) -> None:
    """Wire fact 1 and 3: a per-method path, and an envelope the caller never fills in.

    The agent supplies only ``params``. ``jsonrpc``, ``id`` and ``method`` are pinned in
    the spec, so a call cannot be made with the wrong method name in the body.
    """
    request = _client().prepare(operation_id, {"body": {"params": {}}})
    assert request.method == "POST"
    assert request.url == f"{BASE}/{operation_id}"
    assert request.json_body["jsonrpc"] == "2.0"
    assert request.json_body["method"] == operation_id
    assert request.json_body["params"] == {}


def test_params_are_named_because_positional_is_refused() -> None:
    """Wire fact 2, and the one most likely to be got wrong.

    Solana's own JSON-RPC is positional, so an agent generalising from getBalance sends a
    list. Kora answers ``invalid type: map, expected a string``. The spec types params as
    an object, so a list cannot be built in the first place.
    """
    spec = json.loads(KORA_SPEC_PATH.read_text())
    for operation_id in READ_OPS + KORA_WRITE_OP_IDS:
        body = spec["paths"][f"/{operation_id}"]["post"]["requestBody"]
        schema = body["content"]["application/json"]["schema"]
        assert schema["properties"]["params"]["type"] == "object", operation_id


def test_the_fee_payers_arguments_are_the_ones_the_source_declares() -> None:
    """estimateTransactionFee is the call the whole gasless story rests on."""
    request = _client().prepare(
        "estimateTransactionFee",
        {
            "body": {
                "params": {
                    "transaction": "AA",
                    "fee_token": "EPjFW",
                    "sig_verify": False,
                }
            }
        },
    )
    assert request.json_body["params"] == {
        "transaction": "AA",
        "fee_token": "EPjFW",
        "sig_verify": False,
    }


def test_every_money_mover_is_recorded_on_a_live_surface() -> None:
    """The boundary: a live Kora mount may not relay a call that spends its fee payer."""
    surface = build_kora_surface(BASE, "block")
    recorded = surface.recorded_ops
    assert recorded == resolve_write_tool_names(_client())
    assert len(recorded) == len(KORA_WRITE_OP_IDS)
    for read_op in READ_OPS:
        assert read_op not in recorded, f"{read_op} is a read and must stay live"


def test_a_write_missing_from_the_spec_fails_closed() -> None:
    """If the spec drifts, refuse to build rather than serve an unpinned spender."""

    class Drifted:
        operations = [
            op for op in _client().operations if op.operation_id != "signTransaction"
        ]

    with pytest.raises(KoraBoundaryError, match="signTransaction"):
        resolve_write_tool_names(Drifted())  # type: ignore[arg-type]


def test_the_public_catalog_reaches_no_node_at_all() -> None:
    """What a public mount may serve: all ten methods, $0, no node, nobody's SOL."""
    surface = build_kora_catalog_surface("block")
    assert surface.mode == "recorded"
    assert len(surface.client.operations) == 10


def test_a_surface_without_a_node_is_refused() -> None:
    """There is no default fee payer, because no address is right for everyone."""
    with pytest.raises(KoraBoundaryError, match="base_url"):
        build_kora_surface("", "block")
