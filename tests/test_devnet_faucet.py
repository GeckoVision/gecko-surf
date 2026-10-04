"""The bootcamp's devnet faucet: one call funds a buyer, on devnet only, never twice."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from solders.keypair import Keypair
from solders.transaction import Transaction
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gecko.registry import api as registry_api
from gecko.registry.api import registry_routes
from gecko.registry.devnet_faucet import (
    CLASS_MINTS,
    DEVNET_GENESIS,
    KEY_ENV,
    DevnetFaucet,
    FaucetError,
    build_faucet_from_env,
)
from gecko.registry.store import SurfaceStore

BLOCKHASH = "4sGjMW1sUnHzSxGspuhpqLDx6wiyjNtZAMdL4VZHirAn"


class FakeDevnet:
    """Answers the five RPC methods the faucet uses; records what was sent."""

    def __init__(
        self,
        *,
        genesis: str = DEVNET_GENESIS,
        lamports: dict[str, int] | None = None,
        tokens: dict[tuple[str, str], int] | None = None,
    ) -> None:
        self.genesis = genesis
        self.lamports = lamports or {}
        self.tokens = tokens or {}
        self.sent: list[Transaction] = []

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getGenesisHash":
            return {"result": self.genesis}
        if method == "getBalance":
            return {"result": {"value": self.lamports.get(params[0], 0)}}
        if method == "getTokenAccountsByOwner":
            raw = self.tokens.get((params[0], params[1]["mint"]), 0)
            value = (
                [
                    {
                        "account": {
                            "data": {
                                "parsed": {
                                    "info": {"tokenAmount": {"amount": str(raw)}}
                                }
                            }
                        }
                    }
                ]
                if raw
                else []
            )
            return {"result": {"value": value}}
        if method == "getLatestBlockhash":
            return {"result": {"value": {"blockhash": BLOCKHASH}}}
        if method == "sendTransaction":
            self.sent.append(Transaction.from_bytes(base64.b64decode(params[0])))
            return {"result": "5ig" + "1" * 60}
        raise AssertionError(method)


FAUCET = Keypair()
BUYER = str(Keypair().pubkey())


def stocked(**overrides: Any) -> FakeDevnet:
    tokens = {(str(FAUCET.pubkey()), mint): 50_000 * 10**6 for mint in CLASS_MINTS}
    return FakeDevnet(
        lamports={str(FAUCET.pubkey()): 5 * 10**9}, tokens=tokens, **overrides
    )


def faucet(chain: FakeDevnet) -> DevnetFaucet:
    return DevnetFaucet(FAUCET, rpc_call=chain)


def test_a_new_buyer_gets_both_class_tokens_and_sol_in_one_signed_transaction() -> None:
    chain = stocked()
    funded = faucet(chain).fund(BUYER)

    (tx,) = chain.sent
    assert tx.message.account_keys[0] == FAUCET.pubkey(), "the faucet pays the fees"
    # system transfer, then (create ATA, token transfer) for each mint
    assert len(tx.message.instructions) == 1 + 2 * len(CLASS_MINTS)
    assert funded.sent_lamports == 50_000_000
    assert funded.sent_tokens == {mint: 20 * 10**6 for mint in CLASS_MINTS}
    assert funded.as_json()["explorer"].endswith("?cluster=devnet")


def test_a_buyer_that_already_has_the_kit_is_sent_nothing() -> None:
    chain = stocked()
    chain.lamports[BUYER] = 2 * 10**9
    for mint in CLASS_MINTS:
        chain.tokens[(BUYER, mint)] = 1
    funded = faucet(chain).fund(BUYER)

    assert chain.sent == []
    assert funded.signature is None
    assert funded.as_json()["note"] == "already funded: nothing to send"


def test_only_the_missing_piece_is_sent() -> None:
    chain = stocked()
    chain.lamports[BUYER] = 2 * 10**9
    chain.tokens[(BUYER, CLASS_MINTS[0])] = 5
    funded = faucet(chain).fund(BUYER)

    assert funded.sent_lamports == 0
    assert list(funded.sent_tokens) == [CLASS_MINTS[1]]
    assert len(chain.sent[0].message.instructions) == 2


def test_anything_but_devnet_is_refused_before_signing() -> None:
    chain = stocked(genesis="5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d")  # mainnet
    with pytest.raises(FaucetError) as refused:
        faucet(chain).fund(BUYER)
    assert refused.value.code == "not-devnet"
    assert chain.sent == []


@pytest.mark.parametrize("address", ["not-an-address", "", "0" * 44])
def test_a_malformed_address_is_refused(address: str) -> None:
    with pytest.raises(FaucetError) as refused:
        faucet(stocked()).fund(address)
    assert refused.value.status == 400


def test_a_program_derived_address_is_refused() -> None:
    from gecko.store_accounts import derive_ata
    from gecko.landing import TOKEN_PROGRAM_ID

    pda = derive_ata(BUYER, CLASS_MINTS[0], token_program=TOKEN_PROGRAM_ID)
    with pytest.raises(FaucetError) as refused:
        faucet(stocked()).fund(pda)
    assert refused.value.code == "not-a-wallet"


def test_an_empty_faucet_says_so_and_sends_nothing() -> None:
    chain = FakeDevnet(lamports={str(FAUCET.pubkey()): 5 * 10**9})
    with pytest.raises(FaucetError) as refused:
        faucet(chain).fund(BUYER)
    assert refused.value.code == "faucet-empty"
    assert chain.sent == []


def test_the_key_comes_from_the_environment_or_the_faucet_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert build_faucet_from_env() is None
    monkeypatch.setenv(KEY_ENV, "__unset__")
    assert build_faucet_from_env() is None
    monkeypatch.setenv(KEY_ENV, "not json")
    assert build_faucet_from_env() is None
    monkeypatch.setenv(KEY_ENV, json.dumps(list(bytes(FAUCET))))
    built = build_faucet_from_env()
    assert built is not None and built.address == str(FAUCET.pubkey())


# ---------------------------------------------------------------- the route


def client(faucet_obj: DevnetFaucet | None) -> TestClient:
    registry_api._faucet_ip_counts.clear()
    return TestClient(
        Starlette(routes=registry_routes(SurfaceStore([]), None, faucet=faucet_obj))
    )


def test_the_route_funds_a_buyer() -> None:
    chain = stocked()
    reply = client(faucet(chain)).post(
        "/registry/class-wallet/faucet", json={"buyer": BUYER}
    )
    assert reply.status_code == 200
    assert reply.json()["network"] == "devnet"
    assert len(chain.sent) == 1


def test_the_route_is_503_without_a_key() -> None:
    reply = client(None).post("/registry/class-wallet/faucet", json={"buyer": BUYER})
    assert reply.status_code == 503


def test_the_route_needs_a_buyer() -> None:
    reply = client(faucet(stocked())).post("/registry/class-wallet/faucet", json={})
    assert reply.status_code == 400
    assert "buyer" in reply.json()["error"]


def test_the_route_is_rate_limited_per_ip() -> None:
    http = client(faucet(stocked()))
    codes = [
        http.post("/registry/class-wallet/faucet", json={}).status_code
        for _ in range(registry_api._FAUCET_MAX_PER_HOUR + 1)
    ]
    assert codes[-1] == 429
