"""Sprint 3 (part 1): plan_buy — the full Pump.fun ``buy`` account set → /build payload.

Offline (Pattern B, $0): an injected ``rpc_call`` returns canned ``getAccountInfo`` for
the TWO reads plan_buy makes — the mint's owner (→ token_program) and the bonding_curve
account whose bytes [49:81] are the creator (→ creator_vault). The plan must assemble the
15 accounts Gecko can supply first-call-correct, echo the args, set feePayer=user, point
at Orquestra's buy builder — and flag ``fee_recipient`` as an honest gap (NOT guessed).

The real on-chain gate against a live surfpool mainnet fork is env-gated
(GECKO_SURFPOOL_E2E=1) so the default suite stays offline and $0.

Ground truth (RPC-verified from real mainnet — see test_providers_pumpfun / test_pda_resolve):
  mint          8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump  (Token-2022)
  bonding_curve EExN5XXyaaE3G3w93WdKbJgMUAH3sFgLJBsg5crNp3tH
  creator       Cgjdu87kEeTuUGbKh5mAmFnVSeLN189LDbSvNb24J7Mq  (bonding_curve @ offset 49)
  creator_vault 9B1eLfPtyqyTepP98VPosL7s2cQWN29SKMhk2iNTVkqd
  assoc_bc      6qg9ZgTnbeqdmzkuVT6Ffv95nmD2yX6KUxEyVRc1DmDH  (Token-2022 ATA)
"""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest

from gecko.pda_testkit import SurfpoolError, SurfpoolFork, verify_derivation
from gecko.provider_config import load_packaged_provider
from gecko.providers.pumpfun import (
    BUILD_URL,
    FEE_PROGRAM_ID,
    PUMPFUN_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    plan_buy,
)

MINT = "8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump"
BONDING_CURVE = "EExN5XXyaaE3G3w93WdKbJgMUAH3sFgLJBsg5crNp3tH"
CREATOR = "Cgjdu87kEeTuUGbKh5mAmFnVSeLN189LDbSvNb24J7Mq"
CREATOR_VAULT = "9B1eLfPtyqyTepP98VPosL7s2cQWN29SKMhk2iNTVkqd"
ASSOCIATED_BONDING_CURVE = "6qg9ZgTnbeqdmzkuVT6Ffv95nmD2yX6KUxEyVRc1DmDH"
GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
EVENT_AUTHORITY = "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# A real, valid wallet pubkey chosen for the user-derived accounts (this is Global.authority
# from the live account — any valid pubkey works; ground truth computed independently below).
USER = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"
# Independently derived ground truth for the user-derived accounts (solders find_program_address).
FEE_CONFIG = "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt"
ASSOCIATED_USER = "CTAUKpZkmejuonDJnBRW43FMZx6WpkytQrF8Cty4GfVc"
USER_VOLUME_ACCUMULATOR = "Dhmt8HLWC5KFC3t3RzLiFrqgJwxuR6EgiVssbF34g8CL"

# The 16 accounts a pump `buy` needs. Gecko supplies 15; fee_recipient is the honest gap.
_ALL_BUY_ACCOUNTS = {
    "global",
    "fee_recipient",
    "mint",
    "bonding_curve",
    "associated_bonding_curve",
    "associated_user",
    "user",
    "system_program",
    "token_program",
    "creator_vault",
    "event_authority",
    "program",
    "global_volume_accumulator",
    "user_volume_accumulator",
    "fee_config",
    "fee_program",
}


def _creator_bytes() -> bytes:
    from solders.pubkey import Pubkey

    return bytes(Pubkey.from_string(CREATOR))


def _bonding_curve_data() -> str:
    """base64 blob whose bytes [49:81] are the real creator (BondingCurve layout)."""
    raw = bytearray(150)
    raw[49:81] = _creator_bytes()
    return base64.b64encode(bytes(raw)).decode()


def _fake_rpc():
    """A getAccountInfo fake dispatching on address: the mint (→ owner=Token-2022) and
    the bonding_curve (→ base64 data carrying the creator at offset 49). Records calls."""
    calls: list[tuple[str, list[Any]]] = []

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        calls.append((method, params))
        addr = params[0]
        if addr == MINT:
            return {"result": {"value": {"owner": TOKEN_2022_PROGRAM}}}
        if addr == BONDING_CURVE:
            return {
                "result": {
                    "value": {
                        "owner": PUMPFUN_PROGRAM_ID,
                        "data": [_bonding_curve_data(), "base64"],
                    }
                }
            }
        raise AssertionError(f"unexpected getAccountInfo for {addr}")

    rpc.calls = calls  # type: ignore[attr-defined]
    return rpc


def _plan() -> dict[str, Any]:
    return plan_buy(
        {
            "mint": MINT,
            "user": USER,
            "amount": 1_000_000,
            "max_sol_cost": 50_000_000,
            "track_volume": True,
        },
        rpc_call=_fake_rpc(),
    )


# --- offline, $0: the full account set assembles first-call-correct ---------


def test_plan_buy_returns_payload_shape() -> None:
    plan = _plan()
    assert plan["instruction"] == "buy"
    assert plan["feePayer"] == USER
    assert plan["build_url"] == BUILD_URL
    assert plan["args"] == {
        "amount": 1_000_000,
        "max_sol_cost": 50_000_000,
        "track_volume": True,
    }


def test_plan_buy_resolves_fifteen_accounts_flags_fee_recipient() -> None:
    plan = _plan()
    accounts = plan["accounts"]
    # 15 supplied, fee_recipient is the flagged gap → 15 + 1 = the full 16-account set.
    assert set(accounts) == _ALL_BUY_ACCOUNTS - {"fee_recipient"}
    assert "fee_recipient" in plan["unresolved"]
    assert "fee_recipient" not in accounts
    # every supplied account is a base58 string (valid pubkey)
    from solders.pubkey import Pubkey

    for name, value in accounts.items():
        assert isinstance(value, str)
        Pubkey.from_string(value)  # raises if not valid base58 pubkey


def test_plan_buy_derives_the_known_addresses() -> None:
    accounts = _plan()["accounts"]
    # mint-derived + constant accounts, pinned to real mainnet ground truth
    assert accounts["global"] == GLOBAL
    assert accounts["bonding_curve"] == BONDING_CURVE
    assert accounts["associated_bonding_curve"] == ASSOCIATED_BONDING_CURVE
    assert accounts["creator_vault"] == CREATOR_VAULT
    assert accounts["event_authority"] == EVENT_AUTHORITY
    assert accounts["fee_config"] == FEE_CONFIG
    # token_program RESOLVED from the mint owner (Token-2022 for this fixture)
    assert accounts["token_program"] == TOKEN_2022_PROGRAM
    # constants
    assert accounts["system_program"] == SYSTEM_PROGRAM_ID
    assert accounts["program"] == PUMPFUN_PROGRAM_ID
    assert accounts["fee_program"] == FEE_PROGRAM_ID
    # inputs echoed
    assert accounts["mint"] == MINT
    assert accounts["user"] == USER


def test_plan_buy_user_derived_accounts_match_ground_truth() -> None:
    # associated_user (buyer's ATA) + user_volume_accumulator are derived from USER;
    # they must equal an independent solders derivation (derive-match), not just be valid.
    accounts = _plan()["accounts"]
    assert accounts["associated_user"] == ASSOCIATED_USER
    assert accounts["user_volume_accumulator"] == USER_VOLUME_ACCUMULATOR


def test_plan_buy_makes_exactly_two_reads() -> None:
    # honest control-plane cost: exactly two getAccountInfo reads (mint owner + bonding_curve
    # creator). No response payload is stored — invariant #1.
    rpc = _fake_rpc()
    plan_buy(
        {
            "mint": MINT,
            "user": USER,
            "amount": 1,
            "max_sol_cost": 1,
            "track_volume": False,
        },
        rpc_call=rpc,
    )
    read_addrs = [params[0] for _method, params in rpc.calls]  # type: ignore[attr-defined]
    assert read_addrs == [MINT, BONDING_CURVE]


def test_plan_buy_missing_binding_raises() -> None:
    with pytest.raises(ValueError):
        plan_buy({"mint": MINT, "user": USER}, rpc_call=_fake_rpc())


# --- real on-chain gate: verify the mint-derived accounts on a surfpool fork (env-gated) ---


@pytest.mark.skipif(
    os.getenv("GECKO_SURFPOOL_E2E") != "1",
    reason="set GECKO_SURFPOOL_E2E=1 (and have surfpool + a mainnet RPC) to run the on-chain gate",
)
def test_plan_buy_mint_accounts_real_on_chain() -> None:
    """Fork mainnet locally; run plan_buy against the REAL chain (no fake rpc), then assert
    the mint-derived accounts hold real on-chain accounts with the expected owners. $0, no key."""
    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    _, apis = load_packaged_provider("orquestra")
    program = apis["pumpfun"].program
    assert program is not None
    pdas = program.pdas

    try:
        with SurfpoolFork(mainnet) as fork:
            plan = plan_buy(
                {
                    "mint": MINT,
                    "user": USER,
                    "amount": 1_000_000,
                    "max_sol_cost": 50_000_000,
                    "track_volume": True,
                },
                rpc_url=fork.rpc_url,
            )
            accounts = plan["accounts"]

            # the mint-derived accounts derived by plan_buy match ground truth …
            assert accounts["bonding_curve"] == BONDING_CURVE
            assert accounts["associated_bonding_curve"] == ASSOCIATED_BONDING_CURVE
            assert accounts["creator_vault"] == CREATOR_VAULT
            assert accounts["token_program"] == TOKEN_2022_PROGRAM

            # … and exist on-chain with the expected owners (the strongest correctness proof)
            bc = verify_derivation(
                pdas["bonding_curve"], {"mint": MINT}, rpc_url=fork.rpc_url
            )
            abc = verify_derivation(
                pdas["associated_bonding_curve"],
                {
                    "owner": BONDING_CURVE,
                    "token_program": TOKEN_2022_PROGRAM,
                    "mint": MINT,
                },
                rpc_url=fork.rpc_url,
            )
            fc = verify_derivation(pdas["fee_config"], {}, rpc_url=fork.rpc_url)
    except SurfpoolError as exc:
        pytest.skip(f"surfpool fork unavailable: {exc}")

    assert bc.address == BONDING_CURVE and bc.exists and bc.owner_matches
    # ATAs are owned by their token program (Token-2022 here), not the ATA program
    assert abc.address == ASSOCIATED_BONDING_CURVE and abc.exists
    assert abc.owner == TOKEN_2022_PROGRAM
    # fee_config is a real PDA owned by the fee program
    assert fc.address == FEE_CONFIG and fc.exists
    assert fc.owner == FEE_PROGRAM_ID
    # (creator_vault existence as a System-owned SOL vault is proven in test_providers_pumpfun)
