"""plan_sell — the Pump.fun ``sell`` account set whose SHAPE lives only in prose.

Offline (Pattern B, $0): an injected ``rpc_call`` serves the three reads plan_sell makes
— the mint's owner (→ token_program), the bonding_curve's creator bytes [49:81]
(→ creator_vault), and the bonding_curve's ``is_cashback_coin`` flag @82 (→ which of the
two account SHAPES this coin needs).

What this file pins is the thing an IDL cannot tell you. Both the shipped IDL
(pump-fun/pump-public-docs idl/pump.json) and the live Orquestra surface list **14**
named accounts for ``sell``; the only hint about the rest is the instruction's English
doc-comment ("For cashback coins, pass as remaining_accounts: [0] user_volume_accumulator,
[1] bonding_curve_v2"). The real instruction is 16 accounts for a normal coin and 17 for a
cashback coin (BREAKING_FEE_RECIPIENT.md), with the appended set ordered exactly as
``@pump-fun/pump-sdk@1.36.0 getSellInstructionInternal`` emits it.

Live ground truth (4 mainnet sells decoded 2026-08, see the PR body):
  non-cashback  2CmBdDdueZb3…jnTV  16 accounts, idx14 bonding_curve_v2, idx15 buyback
  cashback      24QWfburTbE2…h33C  17 accounts, idx14 user_volume_accumulator,
                                   idx15 bonding_curve_v2, idx16 buyback
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from gecko.providers.pumpfun import (
    FEE_PROGRAM_ID,
    PUMPFUN_PROGRAM_ID,
    SELL_BUILD_URL,
    SYSTEM_PROGRAM_ID,
    plan_sell,
    sell_remaining_accounts,
)

MINT = "8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump"
BONDING_CURVE = "EExN5XXyaaE3G3w93WdKbJgMUAH3sFgLJBsg5crNp3tH"
CREATOR = "Cgjdu87kEeTuUGbKh5mAmFnVSeLN189LDbSvNb24J7Mq"
CREATOR_VAULT = "9B1eLfPtyqyTepP98VPosL7s2cQWN29SKMhk2iNTVkqd"
ASSOCIATED_BONDING_CURVE = "6qg9ZgTnbeqdmzkuVT6Ffv95nmD2yX6KUxEyVRc1DmDH"
GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
EVENT_AUTHORITY = "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
USER = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"
FEE_CONFIG = "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt"
ASSOCIATED_USER = "CTAUKpZkmejuonDJnBRW43FMZx6WpkytQrF8Cty4GfVc"
USER_VOLUME_ACCUMULATOR = "Dhmt8HLWC5KFC3t3RzLiFrqgJwxuR6EgiVssbF34g8CL"
BONDING_CURVE_V2 = "5VHjhM7qJfaKd9skGBkU1nZCZ9r8XnzEeJ6wNGtwddyq"

# The 14 accounts the surface NAMES for `sell`, in surface order. Note what is absent
# versus a `buy`: NEITHER volume accumulator, and creator_vault sits BEFORE token_program.
_NAMED_SELL_ACCOUNTS = (
    "global",
    "fee_recipient",
    "mint",
    "bonding_curve",
    "associated_bonding_curve",
    "associated_user",
    "user",
    "system_program",
    "creator_vault",
    "token_program",
    "event_authority",
    "program",
    "fee_config",
    "fee_program",
)


def _creator_bytes() -> bytes:
    from solders.pubkey import Pubkey

    return bytes(Pubkey.from_string(CREATOR))


def _curve_blob(*, cashback: bool = False, mayhem: bool = False) -> str:
    """A BondingCurve blob carrying the creator @49 and the two shape flags @81/@82."""
    raw = bytearray(151)
    raw[49:81] = _creator_bytes()
    if mayhem:
        raw[81] = 1
    if cashback:
        raw[82] = 1
    return base64.b64encode(bytes(raw)).decode()


def _fake_rpc(*, cashback: bool = False, mayhem: bool = False):
    blob = _curve_blob(cashback=cashback, mayhem=mayhem)

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        addr = params[0]
        if addr == MINT:
            return {"result": {"value": {"owner": TOKEN_2022_PROGRAM}}}
        if addr == BONDING_CURVE:
            return {
                "result": {
                    "value": {"owner": PUMPFUN_PROGRAM_ID, "data": [blob, "base64"]}
                }
            }
        raise AssertionError(f"unexpected getAccountInfo for {addr}")

    return rpc


def _plan(*, cashback: bool = False, mayhem: bool = False) -> dict[str, Any]:
    return plan_sell(
        {
            "mint": MINT,
            "user": USER,
            "amount": 1_000_000,
            "min_sol_output": 26,
        },
        rpc_call=_fake_rpc(cashback=cashback, mayhem=mayhem),
    )


# --- payload shape ----------------------------------------------------------------


def test_plan_sell_returns_payload_shape() -> None:
    plan = _plan()
    assert plan["instruction"] == "sell"
    assert plan["feePayer"] == USER
    assert plan["build_url"] == SELL_BUILD_URL
    # `sell(amount, min_sol_output)` — two plain u64s, no OptionBool (that is buy-only)
    assert plan["args"] == {"amount": 1_000_000, "min_sol_output": 26}


def test_plan_sell_requires_its_bindings() -> None:
    with pytest.raises(ValueError) as exc:
        plan_sell({"mint": MINT, "user": USER}, rpc_call=_fake_rpc())
    assert "amount" in str(exc.value) and "min_sol_output" in str(exc.value)


def test_plan_sell_supplies_thirteen_named_accounts_and_flags_fee_recipient() -> None:
    accounts = _plan()["accounts"]
    named = set(_NAMED_SELL_ACCOUNTS) - {"fee_recipient"}
    # the 13 named accounts Gecko supplies + bonding_curve_v2, which the surface does not
    # name at all (it travels appended)
    assert set(accounts) == named | {"bonding_curve_v2"}
    assert "fee_recipient" not in accounts
    assert "fee_recipient" in _plan()["unresolved"]
    from solders.pubkey import Pubkey

    for value in accounts.values():
        Pubkey.from_string(value)  # raises if not a valid base58 pubkey


def test_plan_sell_does_not_name_the_volume_accumulators_like_a_buy() -> None:
    """A `sell` names NEITHER accumulator — global_volume_accumulator does not exist on
    it at all, and user_volume_accumulator is appended (cashback only), never named. A
    copy-paste of the buy account set would silently add both."""
    accounts = _plan()["accounts"]
    assert "global_volume_accumulator" not in accounts
    assert "user_volume_accumulator" not in accounts


def test_plan_sell_derives_the_known_addresses() -> None:
    accounts = _plan()["accounts"]
    assert accounts["global"] == GLOBAL
    assert accounts["bonding_curve"] == BONDING_CURVE
    assert accounts["associated_bonding_curve"] == ASSOCIATED_BONDING_CURVE
    assert accounts["associated_user"] == ASSOCIATED_USER
    assert accounts["creator_vault"] == CREATOR_VAULT
    assert accounts["event_authority"] == EVENT_AUTHORITY
    assert accounts["fee_config"] == FEE_CONFIG
    assert accounts["bonding_curve_v2"] == BONDING_CURVE_V2
    assert accounts["token_program"] == TOKEN_2022_PROGRAM
    assert accounts["program"] == PUMPFUN_PROGRAM_ID
    assert accounts["fee_program"] == FEE_PROGRAM_ID
    assert accounts["system_program"] == SYSTEM_PROGRAM_ID


# --- the SHAPE: 16 vs 17, read from the curve, not asked of the caller -------------


def test_non_cashback_coin_plans_the_sixteen_account_shape() -> None:
    plan = _plan(cashback=False)
    assert plan["cashback"]["is_cashback_coin"] is False
    assert plan["cashback"]["account_count"] == 16
    assert "user_volume_accumulator" not in plan["accounts"]
    remaining = plan["landing_plan"][-1]["remaining_accounts"]
    # 13 supplied + fee_recipient + bonding_curve_v2 + (1 unresolved buyback) = 16
    assert [entry["pubkey"] for entry in remaining] == [BONDING_CURVE_V2]


def test_cashback_coin_plans_the_seventeen_account_shape() -> None:
    plan = _plan(cashback=True)
    assert plan["cashback"]["is_cashback_coin"] is True
    assert plan["cashback"]["account_count"] == 17
    assert plan["accounts"]["user_volume_accumulator"] == USER_VOLUME_ACCUMULATOR
    remaining = plan["landing_plan"][-1]["remaining_accounts"]
    # the doc-comment's order: [0] user_volume_accumulator, [1] bonding_curve_v2
    assert [entry["pubkey"] for entry in remaining] == [
        USER_VOLUME_ACCUMULATOR,
        BONDING_CURVE_V2,
    ]


def test_the_shape_comes_from_the_curve_flag_not_a_caller_argument() -> None:
    """Same bindings, two curves — only the on-chain byte @82 differs, and the plan
    changes shape. A caller cannot ask for the wrong one."""
    assert _plan(cashback=False)["cashback"]["account_count"] == 16
    assert _plan(cashback=True)["cashback"]["account_count"] == 17


def test_mayhem_coin_is_flagged_not_claimed() -> None:
    plan = _plan(mayhem=True)
    assert plan["cashback"]["is_mayhem_mode"] is True
    assert "is_mayhem_mode" in plan["preconditions"]
    assert "UNVERIFIED" in plan["preconditions"]["is_mayhem_mode"]
    # a normal coin carries no such warning
    assert "is_mayhem_mode" not in _plan()["preconditions"]


# --- the honest gaps + preludes ---------------------------------------------------


def test_both_gaps_are_read_recipes_never_guessed_pubkeys() -> None:
    unresolved = _plan()["unresolved"]
    assert set(unresolved) == {"fee_recipient", "buyback_fee_recipient"}
    assert unresolved["fee_recipient"]["resolve"]["field_offset"] == 162
    buyback = unresolved["buyback_fee_recipient"]["resolve"]
    assert (buyback["read"], buyback["field_offset"], buyback["count"]) == (
        "global",
        741,
        8,
    )
    # deterministic pick: index 0 of the 8 (the SDK randomizes only to spread load)
    assert buyback["take"] == 0
    # no pubkey is baked into either recipe
    for gap in unresolved.values():
        assert "pubkey" not in gap


def test_sell_declares_no_ata_prelude_because_the_seller_already_holds_the_token() -> (
    None
):
    """The honest per-program answer, asserted so nobody 'helpfully' copies the buy
    prelude across: a sell's whole prelude is ComputeBudget."""
    kinds = [step["kind"] for step in _plan()["landing_plan"]]
    assert kinds == ["compute_budget", "compute_budget", "sell"]
    assert "create_idempotent_ata" not in kinds
    # and the reason is stated where a caller will read it
    assert "already" in _plan()["preconditions"]["associated_user"].lower()


def test_preconditions_name_the_lazy_v2_account_and_the_graduated_curve() -> None:
    preconditions = _plan()["preconditions"]
    assert "ADDRESS" in preconditions["bonding_curve_v2"]
    assert "PumpSwap" in preconditions["bonding_curve"]


# --- the appended-set assembler (single source of truth) --------------------------


def test_sell_remaining_accounts_matches_the_sdk_metas() -> None:
    """pump-sdk 1.36.0 getSellInstructionInternal: bonding_curve_v2 is READ-ONLY, the
    buyback recipient is writable, and user_volume_accumulator (cashback only) leads and
    is writable."""
    plain = sell_remaining_accounts(
        BONDING_CURVE_V2, "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL"
    )
    assert plain == [
        {"pubkey": BONDING_CURVE_V2, "isWritable": False, "isSigner": False},
        {
            "pubkey": "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL",
            "isWritable": True,
            "isSigner": False,
        },
    ]
    cash = sell_remaining_accounts(
        BONDING_CURVE_V2,
        "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL",
        USER_VOLUME_ACCUMULATOR,
    )
    assert cash[0] == {
        "pubkey": USER_VOLUME_ACCUMULATOR,
        "isWritable": True,
        "isSigner": False,
    }
    assert cash[1:] == plain
    assert all(not entry["isSigner"] for entry in cash)


# --- the intent is routable --------------------------------------------------------


def test_plan_sell_is_a_registered_intent() -> None:
    from gecko.providers.cli import intent_registries, start_specs

    intent = intent_registries()["pumpfun"]["plan_sell"]
    assert intent.instruction == "sell"
    assert set(intent.inputs) == {"mint", "user", "amount", "min_sol_output"}
    spec = start_specs()["pumpfun"]["plan_sell"]
    assert "bonding_curve_v2" in spec.accounts
    assert {gap.name for gap in spec.gaps} >= {
        "fee_recipient",
        "buyback_fee_recipient",
    }
    # the sell prelude is ComputeBudget only — no ATA step
    assert [prelude.kind for prelude in spec.preludes] == ["compute_budget"]
