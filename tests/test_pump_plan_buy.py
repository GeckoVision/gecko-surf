"""Sprint 3 (part 1): plan_buy — the full Pump.fun ``buy`` account set → /build payload.

Offline (Pattern B, $0): an injected ``rpc_call`` returns canned ``getAccountInfo`` for
the TWO reads plan_buy makes — the mint's owner (→ token_program) and the bonding_curve
account whose bytes [49:81] are the creator (→ creator_vault). The plan must declare the
post-Apr-2026 18-account ``buy`` truth: assemble the 16 accounts Gecko can supply
first-call-correct (incl. the purely-derived ``bonding_curve_v2``), echo the args, set
feePayer=user, point at Orquestra's buy builder — and declare ``fee_recipient`` (regular,
Global.fee_recipients[0]@162) and the appended buyback recipient (@741) as honest
resolver-style gaps (NOT guessed).

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

from gecko.pda_testkit import (
    start_failure_is_a_broken_gate,
    SurfpoolError,
    SurfpoolFork,
    verify_derivation,
)
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
# bonding_curve_v2: pure derivation from ["bonding-curve-v2", mint] (independently verified
# with solders). Lazily created on-chain — the ADDRESS, never existence, is the contract.
BONDING_CURVE_V2 = "5VHjhM7qJfaKd9skGBkU1nZCZ9r8XnzEeJ6wNGtwddyq"

# The named accounts of the post-Apr-2026 18-account `buy` (16 original + bonding_curve_v2;
# the 18th — the appended buyback recipient — is a declared read recipe, not a named
# account). Gecko supplies 16; fee_recipient is the honest gap.
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
    "bonding_curve_v2",
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
        # OptionBool wire shape Orquestra's /build requires (not a bare bool)
        "track_volume": {"field_0": True},
    }


def test_plan_buy_track_volume_is_option_bool() -> None:
    # a bare bool is rejected by Orquestra ("must be an object matching struct OptionBool");
    # plan_buy emits {"field_0": bool}, coercing string inputs an MCP tool passes.
    assert _plan()["args"]["track_volume"] == {"field_0": True}
    false_plan = plan_buy(
        {
            "mint": MINT,
            "user": USER,
            "amount": 1,
            "max_sol_cost": 1,
            "track_volume": "false",
        },
        rpc_call=_fake_rpc(),
    )
    assert false_plan["args"]["track_volume"] == {"field_0": False}


def test_plan_buy_flags_associated_user_ata_precondition() -> None:
    # honest first-call-correctness: the real cause of AnchorError 3012 is a missing buyer
    # ATA; plan_buy surfaces the precondition rather than emit a plan that reverts.
    plan = _plan()
    assert "associated_user" in plan["preconditions"]


def test_plan_buy_resolves_sixteen_accounts_flags_fee_recipient() -> None:
    plan = _plan()
    accounts = plan["accounts"]
    # 16 supplied (incl. bonding_curve_v2), fee_recipient is the flagged gap → the full
    # named set of the current program's 18-account buy.
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
    # bonding_curve_v2 (post-Apr-2026 account #17): PURE derivation, address-correct
    assert accounts["bonding_curve_v2"] == BONDING_CURVE_V2
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


# --- the post-Apr-2026 18-account truth is DECLARED, not orchestrator-only -----


def test_fee_recipient_note_carries_the_empirical_recipe_not_the_refuted_41() -> None:
    # The live Receipt established: `buy` accepts a REGULAR recipient — fee_recipients[0]
    # @ offset 162; the @41 field is a BUYBACK recipient (reverted 6062). The declared
    # note/recipe must carry the empirical truth, not the refuted "@41" guidance.
    entry = _plan()["unresolved"]["fee_recipient"]
    assert entry["resolve"] == {
        "read": "global",
        "field": "fee_recipients[0]",
        "field_offset": 162,
        "encoding": "pubkey",
    }
    assert "162" in entry["note"]
    assert "REFUTED" in entry["note"]  # the old @41 guidance, named and refuted
    assert "BUYBACK" in entry["note"]  # what @41 actually is


def test_plan_declares_buyback_recipient_as_resolver_style_read_recipe() -> None:
    # The appended buyback recipient (the 18th account) is declared the same honest way
    # as creator_vault: a read recipe (Global.buyback_fee_recipients@741), never a guess.
    entry = _plan()["unresolved"]["buyback_fee_recipient"]
    assert entry["resolve"] == {
        "read": "global",
        "field": "buyback_fee_recipients",
        "field_offset": 741,
        "count": 8,
        "encoding": "pubkey",
    }
    assert "bonding_curve_v2" in entry["note"]  # ordering: appended AFTER it


def test_plan_flags_bonding_curve_v2_as_address_not_existence() -> None:
    # bonding_curve_v2 is created lazily — for older mints the account may not exist yet.
    # The declared contract is the ADDRESS; the plan must say so, never imply existence.
    note = _plan()["preconditions"]["bonding_curve_v2"]
    assert "ADDRESS" in note
    assert "never existence" in note


def test_landing_plan_buy_step_declares_the_appended_remaining_accounts() -> None:
    # The declared buy step carries the concrete appended prefix (bonding_curve_v2) plus
    # the resolver recipe for the buyback recipients — the same set the orchestrator
    # assembles (see test_pump_buy_landing's no-drift guard).
    plan = _plan()
    buy = next(s for s in plan["landing_plan"] if s["kind"] == "buy")
    assert buy["accounts"] == plan["accounts"]
    assert buy["remaining_accounts"] == [
        {"pubkey": BONDING_CURVE_V2, "isWritable": True, "isSigner": False}
    ]
    recipe = buy["remaining_accounts_unresolved"]["buyback_fee_recipient"]["resolve"]
    assert (recipe["field_offset"], recipe["count"]) == (741, 8)


# --- Task 3 (Path B): the self-serve simulate recipe block -------------------


def test_plan_buy_carries_simulate_recipe() -> None:
    plan = _plan()
    sim = plan["simulate"]
    assert sim["rpc_method"] == "simulateTransaction"
    # the honest-order recipe: fill fee_recipient, POST build_url, then simulate
    assert "fee_recipient" in sim["after"]
    assert "replaceRecentBlockhash" in sim["params_note"]
    assert sim["gecko_tool"].startswith("simulate")


def test_plan_buy_simulate_block_does_not_disturb_account_contract() -> None:
    # the new key is purely additive: the 15-account set + fee_recipient gap are unchanged
    plan = _plan()
    assert set(plan["accounts"]) == _ALL_BUY_ACCOUNTS - {"fee_recipient"}
    assert "fee_recipient" in plan["unresolved"]
    assert "fee_recipient" not in plan["accounts"]


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
        # Installed and would not start = a BROKEN gate, not an absent one.
        if start_failure_is_a_broken_gate():
            pytest.fail(
                "surfpool IS installed and the fork did not start, so this "
                "gate is broken rather than absent — a skip here would claim "
                f"the environment cannot do what it demonstrably can: {exc}"
            )
        pytest.skip(f"surfpool fork unavailable: {exc}")

    assert bc.address == BONDING_CURVE and bc.exists and bc.owner_matches
    # ATAs are owned by their token program (Token-2022 here), not the ATA program
    assert abc.address == ASSOCIATED_BONDING_CURVE and abc.exists
    assert abc.owner == TOKEN_2022_PROGRAM
    # fee_config is a real PDA owned by the fee program
    assert fc.address == FEE_CONFIG and fc.exists
    assert fc.owner == FEE_PROGRAM_ID
    # (creator_vault existence as a System-owned SOL vault is proven in test_providers_pumpfun)
