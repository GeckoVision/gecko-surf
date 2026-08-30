"""Sprint 2: the resolution engine — read-to-resolve dotted-path seeds.

Offline (Pattern B, $0): an injected ``rpc_call`` returns a canned ``getAccountInfo``
for the bonding_curve account whose bytes [49:81] are the creator pubkey; the engine
must read that field, bind it, and derive the REAL ``creator_vault`` address — with no
network. The real on-chain gate against a live surfpool mainnet fork is env-gated
(GECKO_SURFPOOL_E2E=1).

Ground truth (RPC-verified from real mainnet):
  mint          8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump
  bonding_curve EExN5XXyaaE3G3w93WdKbJgMUAH3sFgLJBsg5crNp3tH
  creator       Cgjdu87kEeTuUGbKh5mAmFnVSeLN189LDbSvNb24J7Mq  (bonding_curve byte offset 49)
  creator_vault 9B1eLfPtyqyTepP98VPosL7s2cQWN29SKMhk2iNTVkqd
  mint owner    TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb   (Token-2022)
"""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest

from gecko.pda import PdaNode
from gecko.pda_resolve import (
    ResolveError,
    read_account_field_pubkey,
    read_account_owner,
    resolve_pda,
)
from gecko.pda_testkit import (
    start_failure_is_a_broken_gate,
    SurfpoolError,
    SurfpoolFork,
)
from gecko.provider_config import load_packaged_provider

MINT = "8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump"
BONDING_CURVE = "EExN5XXyaaE3G3w93WdKbJgMUAH3sFgLJBsg5crNp3tH"
CREATOR = "Cgjdu87kEeTuUGbKh5mAmFnVSeLN189LDbSvNb24J7Mq"
CREATOR_VAULT = "9B1eLfPtyqyTepP98VPosL7s2cQWN29SKMhk2iNTVkqd"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def _pumpfun_pdas() -> dict[str, PdaNode]:
    _, apis = load_packaged_provider("orquestra")
    program = apis["pumpfun"].program
    assert program is not None
    return program.pdas


def _creator_bytes() -> bytes:
    from solders.pubkey import Pubkey

    return bytes(Pubkey.from_string(CREATOR))


def _bonding_curve_account_data() -> str:
    """A base64 blob whose bytes [49:81] are the real creator pubkey — mirrors the
    on-chain BondingCurve layout (8 discriminator + 5×u64 + 1 bool = offset 49)."""
    raw = bytearray(150)
    raw[49:81] = _creator_bytes()
    return base64.b64encode(bytes(raw)).decode()


def _fake_rpc(*, data: str | None = None, owner: str | None = None):
    """A getAccountInfo fake that records calls and returns a canned account value."""
    calls: list[tuple[str, list[Any]]] = []

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        calls.append((method, params))
        value: dict[str, Any] = {"owner": owner or SYSTEM_PROGRAM}
        if data is not None:
            value["data"] = [data, "base64"]
        return {"result": {"value": value}}

    rpc.calls = calls  # type: ignore[attr-defined]
    return rpc


# --- unit: the two read primitives -----------------------------------------


def test_read_account_field_pubkey_extracts_the_offset_pubkey() -> None:
    rpc = _fake_rpc(data=_bonding_curve_account_data())
    got = read_account_field_pubkey(BONDING_CURVE, 49, rpc_call=rpc)
    assert got == CREATOR
    # it asked for the account with base64 encoding
    assert rpc.calls[0][0] == "getAccountInfo"
    assert rpc.calls[0][1][0] == BONDING_CURVE
    assert rpc.calls[0][1][1] == {"encoding": "base64"}


def test_read_account_owner_returns_owner() -> None:
    rpc = _fake_rpc(owner=TOKEN_2022_PROGRAM)
    assert read_account_owner(MINT, rpc_call=rpc) == TOKEN_2022_PROGRAM


def test_read_account_owner_asks_for_base64_encoding() -> None:
    """Regression: with no encoding the RPC defaults to base58, and mainnet providers
    reject that above 128 bytes ("Encoded binary (base 58) data should be less than 128
    bytes"). A Token-2022 mint with extensions is routinely larger, so an owner lookup —
    which does not even read the data — used to hard-fail on exactly the mints Pump
    trades. Only the owner is used; the encoding just has to be one the RPC will serve."""
    rpc = _fake_rpc(owner=TOKEN_2022_PROGRAM)
    read_account_owner(MINT, rpc_call=rpc)
    assert rpc.calls[0][0] == "getAccountInfo"  # type: ignore[attr-defined]
    assert rpc.calls[0][1] == [MINT, {"encoding": "base64"}]  # type: ignore[attr-defined]


def test_read_missing_account_raises() -> None:
    def rpc(_url: str, _method: str, _params: list[Any]) -> dict[str, Any]:
        return {"result": {"value": None}}

    with pytest.raises(ResolveError):
        read_account_owner(MINT, rpc_call=rpc)
    with pytest.raises(ResolveError):
        read_account_field_pubkey(BONDING_CURVE, 49, rpc_call=rpc)


def test_read_field_too_short_raises() -> None:
    short = base64.b64encode(b"\x00" * 60).decode()  # < offset 49 + 32
    rpc = _fake_rpc(data=short)
    with pytest.raises(ResolveError):
        read_account_field_pubkey(BONDING_CURVE, 49, rpc_call=rpc)


# --- the engine: resolve creator_vault from the config recipe, offline ------


def test_resolve_creator_vault_reads_bonding_curve_and_derives() -> None:
    # The config's creator_vault resolver seed carries {"read": "bonding_curve",
    # "field_offset": 49}. The engine derives bonding_curve from {mint}, reads the
    # creator field, binds it, and derives the vault — first-plan-correct.
    pdas = _pumpfun_pdas()
    rpc = _fake_rpc(data=_bonding_curve_account_data())
    got = resolve_pda(pdas, "creator_vault", {"mint": MINT}, rpc_call=rpc)
    assert got.address == CREATOR_VAULT
    # it read the bonding_curve ADDRESS (derived from mint), not anything else
    assert rpc.calls[0][1][0] == BONDING_CURVE


# --- real on-chain gate: env-gated, $0, no key ------------------------------


@pytest.mark.skipif(
    os.getenv("GECKO_SURFPOOL_E2E") != "1",
    reason="set GECKO_SURFPOOL_E2E=1 (and have surfpool + a mainnet RPC) to run the on-chain gate",
)
def test_resolve_against_surfpool_fork() -> None:
    """Fork mainnet locally; the engine reads the REAL bonding_curve, extracts the
    creator, and derives creator_vault == 9B1eLf… ; and read_account_owner(mint)
    proves the token_program is resolvable from the mint owner. $0, no key, no sign."""
    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    pdas = _pumpfun_pdas()
    try:
        with SurfpoolFork(mainnet) as fork:
            vault = resolve_pda(
                pdas, "creator_vault", {"mint": MINT}, rpc_url=fork.rpc_url
            )
            owner = read_account_owner(MINT, rpc_url=fork.rpc_url)
            vault_owner = read_account_owner(vault.address, rpc_url=fork.rpc_url)
    except SurfpoolError as exc:
        # Installed and would not start = a BROKEN gate, not an absent one.
        if start_failure_is_a_broken_gate():
            pytest.fail(
                "surfpool IS installed and the fork did not start, so this "
                "gate is broken rather than absent — a skip here would claim "
                f"the environment cannot do what it demonstrably can: {exc}"
            )
        pytest.skip(f"surfpool fork unavailable: {exc}")

    # (a) read the real creator, derive the real creator_vault SOL vault (System-owned)
    assert vault.address == CREATOR_VAULT
    assert vault_owner == SYSTEM_PROGRAM
    # (b) the mint owner resolves the token_program (Token-2022 for this fixture)
    assert owner == TOKEN_2022_PROGRAM
