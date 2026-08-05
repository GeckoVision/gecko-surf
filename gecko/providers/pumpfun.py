"""Pump.fun — the second Orquestra program instance (buy against a bonding curve).

Where Meteora's star is deriving the helper-seeded root (``lb_pair``), Pump's is
assembling a WHOLE instruction's account set first-call-correct: per the current
(post-Apr-2026, BREAKING_FEE_RECIPIENT.md) program a ``buy`` is **18 accounts** — the 16
original + ``bonding_curve_v2`` + a buyback fee recipient appended after it — most of
which an IDL/llms.txt cannot give you: the dotted-path ``creator_vault`` (reads
``bonding_curve.creator``), the token program (read from the mint's owner, Token vs
Token-2022), the two ATAs, the fee-program PDAs, the upgrade-added accounts the
pre-upgrade IDL drops entirely. Gecko derives and resolves every one it honestly can,
then hands back the Orquestra ``/build`` payload.

The honest gaps stay flagged, now with the recipes the live Receipt established
empirically: ``fee_recipient`` = a REGULAR recipient (``Global.fee_recipients[0]`` @ data
offset 162 — the old guidance pointing at the ``fee_recipient`` field @ offset 41 is
REFUTED: @41 is a *buyback* recipient and using it reverts 6062), and the appended
buyback recipient comes from ``Global.buyback_fee_recipients`` (``[pubkey; 8]`` @ offset
741). ``plan_buy`` declares both as resolver-style entries (a read recipe, same honest
pattern as ``creator_vault``) rather than hardcode pubkeys that can rotate. Honesty over
a fabricated account (the whole differentiator over "read the IDL and hope").

Run it (once its slug is served):
    claude mcp add orquestra-pumpfun -- \
        uvx --from "gecko-surf[serve,solana]" gecko-orquestra --program pumpfun --stdio
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..landing import ASSOCIATED_TOKEN_PROGRAM_ID, COMPUTE_BUDGET_PROGRAM_ID
from ..pda import derive_pda
from ..pda_resolve import read_account_owner, resolve_pda
from ..pda_testkit import LOCAL_RPC, RpcCall
from ..provider_config import load_packaged_provider
from .orquestra import Intent, OrquestraProgramSurface

__all__ = [
    "PUMPFUN_PROGRAM_ID",
    "FEE_PROGRAM_ID",
    "SYSTEM_PROGRAM_ID",
    "BUILD_URL",
    "FEE_RECIPIENTS_OFFSET",
    "BUYBACK_FEE_RECIPIENTS_OFFSET",
    "BUYBACK_FEE_RECIPIENTS_COUNT",
    "PUMPFUN_INTENTS",
    "buy_remaining_accounts",
    "plan_buy",
    "build_pumpfun_surface",
]

# Display constants. The authoritative program id + PDA recipes live in the packaged
# config (gecko/providers/configs/orquestra/pumpfun.json) — this is data, not code.
PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
FEE_PROGRAM_ID = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
BUILD_URL = "https://api.orquestra.dev/api/6i6q26bmm46b89xlxo1kv/instructions/buy/build"

# On-chain Global layout facts the live Receipt established empirically (the E2E landing
# run) — LAYOUT-FRAGILE by nature (exactly the drift the gap-map warns about; the
# env-gated E2E re-confirms them against the live program):
# - Global.fee_recipients[0] @ data offset 162 is a REGULAR fee recipient `buy` accepts.
# - Global.fee_recipient @ offset 41 is a BUYBACK recipient — passing it as the named
#   fee_recipient reverted 6062, refuting the earlier "@41" guidance.
# - Global.buyback_fee_recipients is a [pubkey; 8] array @ data offset 741 — computed from
#   the on-chain Global struct field order (8 disc + initialized + authority +
#   fee_recipient + 5×u64 + withdraw_authority + enable_migrate + 2×u64 +
#   fee_recipients[7] + set/admin creator authority + create_v2_enabled + whitelist_pda +
#   reserved_fee_recipient + mayhem_mode_enabled + reserved_fee_recipients[7] +
#   is_cashback_enabled). A post-Apr-2026 buy needs one of them appended after
#   bonding_curve_v2 (6062 BuybackFeeRecipientMissing otherwise).
FEE_RECIPIENTS_OFFSET = 162
BUYBACK_FEE_RECIPIENTS_OFFSET = 741
BUYBACK_FEE_RECIPIENTS_COUNT = 8

# Why fee_recipient is a flagged gap with a read recipe, never a hardcoded pubkey.
_FEE_RECIPIENT_NOTE = (
    "Global carries BOTH a single `fee_recipient` pubkey field (data offset 41) AND a "
    "`fee_recipients` array (data offset 162), populated on mainnet with different valid "
    "pubkeys. The live Receipt settled which one `buy` accepts empirically: a REGULAR "
    "recipient — Global.fee_recipients[0] @ offset 162. The @41 field is a BUYBACK "
    "recipient (passing it as fee_recipient reverted 6062), so earlier guidance pointing "
    "at @41 is REFUTED. Gecko still will not hardcode a pubkey that can rotate: resolve "
    "via the recipe here, or supply your own valid recipient to /build."
)

# The post-upgrade appended requirement, declared the same honest way as creator_vault:
# a read recipe, never a guessed pubkey.
_BUYBACK_RECIPIENT_NOTE = (
    "Post-Apr-2026 program (BREAKING_FEE_RECIPIENT.md): `buy` also requires a BUYBACK fee "
    "recipient appended AFTER bonding_curve_v2. The 8 valid pubkeys live in "
    "Global.buyback_fee_recipients ([pubkey; 8] @ data offset 741) — read them and append "
    "as remaining accounts (Gecko appends all 8; the live Receipt confirms that set "
    "passes). Same honest pattern as creator_vault: a read recipe, never a guess."
)


def buy_remaining_accounts(
    bonding_curve_v2: str, buyback_fee_recipients: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """The ordered ``remaining_accounts`` a post-Apr-2026 ``buy`` appends:
    ``bonding_curve_v2`` first, then the ``Global.buyback_fee_recipients`` — all writable,
    non-signer. The SINGLE source of truth for the appended set: ``plan_buy`` declares it
    (buyback pubkeys pending the Global read — see ``unresolved``) and
    ``simulate_buy_landing`` injects exactly it. Refactor here, never in two places.
    """
    return [
        {"pubkey": pubkey, "isWritable": True, "isSigner": False}
        for pubkey in (bonding_curve_v2, *buyback_fee_recipients)
    ]


def _option_bool(value: Any) -> dict[str, bool]:
    """Coerce a bool-ish value into Orquestra's ``OptionBool`` wire shape.

    Pump's ``track_volume`` arg is an Anchor ``Option<bool>``, which Orquestra models as a
    defined struct ``OptionBool { field_0: bool }`` — its ``/build`` rejects a bare ``true``
    with "must be an object matching struct OptionBool". Accepts real bools and the string
    forms an MCP tool passes ("true"/"false"/"1"/"0").
    """
    if isinstance(value, str):
        flag = value.strip().lower() in {"true", "1", "yes"}
    else:
        flag = bool(value)
    return {"field_0": flag}


def _landing_plan(accounts: Mapping[str, str]) -> list[dict[str, Any]]:
    """The DECLARED ordered instruction plan a real builder (Orquestra) assembles + 1claw
    signs — the "declare" half of the buy-that-passes. Pure structured data (no bytes, no
    signing): ``[SetComputeUnitLimit, SetComputeUnitPrice, createIdempotentATA, buy]``. The
    compute-budget values and fee_recipient are filled by the simulate/build step; this is
    the ordered contract, not a tx. See :func:`gecko.providers.pumpfun_landing.simulate_buy_landing`.
    """
    return [
        {
            "kind": "compute_budget",
            "program": COMPUTE_BUDGET_PROGRAM_ID,
            "instruction": "SetComputeUnitLimit",
            "note": "units from the Receipt's units_consumed × 1.2 (Gecko simulates to measure)",
        },
        {
            "kind": "compute_budget",
            "program": COMPUTE_BUDGET_PROGRAM_ID,
            "instruction": "SetComputeUnitPrice",
            "note": "micro-lamports from getRecentPrioritizationFees (operator's RPC)",
        },
        {
            "kind": "create_idempotent_ata",
            "program": ASSOCIATED_TOKEN_PROGRAM_ID,
            "accounts": {
                "payer": accounts["user"],
                "ata": accounts["associated_user"],
                "owner": accounts["user"],
                "mint": accounts["mint"],
                "system_program": SYSTEM_PROGRAM_ID,
                "token_program": accounts["token_program"],
            },
            "note": "removes AnchorError 3012 — initializes the buyer's associated_user ATA",
        },
        {
            "kind": "buy",
            "program": PUMPFUN_PROGRAM_ID,
            "accounts": dict(accounts),
            # The DECLARED appended set (single source of truth: buy_remaining_accounts).
            # bonding_curve_v2 is concrete (pure derivation); the buyback recipients need
            # the Global read — declared as a resolver-style recipe below, resolved (and
            # this list completed) by simulate_buy_landing.
            "remaining_accounts": buy_remaining_accounts(accounts["bonding_curve_v2"]),
            "remaining_accounts_unresolved": {
                "buyback_fee_recipient": {
                    "note": _BUYBACK_RECIPIENT_NOTE,
                    "resolve": {
                        "read": "global",
                        "field": "buyback_fee_recipients",
                        "field_offset": BUYBACK_FEE_RECIPIENTS_OFFSET,
                        "count": BUYBACK_FEE_RECIPIENTS_COUNT,
                        "encoding": "pubkey",
                    },
                }
            },
            "remaining_accounts_note": (
                "post-Apr-2026 program: append bonding_curve_v2 (lazily created — the "
                "ADDRESS is what the instruction needs, the account may not exist yet) "
                "then the Global.buyback_fee_recipients@741, all writable/non-signer"
            ),
            "note": (
                "Orquestra builds this instruction; fee_recipient is supplied at /build "
                "(the honest gap) and max_sol_cost is the curve-quoted arg"
            ),
        },
    ]


def _load_pumpfun_pdas() -> dict[str, Any]:
    _, apis = load_packaged_provider("orquestra")
    program = apis["pumpfun"].program
    if program is None:
        raise ValueError("pumpfun config carries no program spec")
    return dict(program.pdas)


def plan_buy(
    bindings: Mapping[str, Any],
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
) -> dict[str, Any]:
    """Assemble the full account set for a Pump.fun ``buy`` → an Orquestra ``/build`` payload.

    ``bindings`` needs ``mint``, ``user``, ``amount``, ``max_sol_cost``, ``track_volume``.
    Gecko resolves the token program (from the mint's owner), derives every PDA/ATA and
    the fee-program PDAs, fills the constants, and returns the ``buy`` payload. Two
    control-plane reads happen (both public metadata, never stored): the mint's owner
    (→ ``token_program``) and ``bonding_curve.creator`` (→ ``creator_vault``).

    The ``accounts`` dict carries the 16 accounts Gecko can supply first-call-correct —
    including ``bonding_curve_v2``, the account the post-Apr-2026 program added (a pure
    derivation from ``["bonding-curve-v2", mint]``; lazily created on-chain, so for older
    mints the account may not EXIST yet — the ADDRESS is what the instruction needs;
    assert address-correct, never existence). The two remaining requirements of the full
    18-account current-program set are declared under ``unresolved`` as resolver-style
    read recipes, not guesses: ``fee_recipient`` (``Global.fee_recipients[0]`` @ 162 —
    the Receipt-refuted @41 field is a buyback recipient) and the appended
    ``buyback_fee_recipient`` (``Global.buyback_fee_recipients`` @ 741).

    The returned ``simulate`` block closes the loop two ways: Path B (self-serve — a dev
    fills ``fee_recipient``, POSTs ``build_url``, runs ``simulateTransaction``) or Path A
    (hand the plan, with ``fee_recipient``, to the surface's generic ``simulate`` tool).
    """
    missing = [
        k
        for k in ("mint", "user", "amount", "max_sol_cost", "track_volume")
        if k not in bindings
    ]
    if missing:
        raise ValueError(f"plan_buy needs bindings {missing}")

    mint = str(bindings["mint"])
    user = str(bindings["user"])
    pdas = _load_pumpfun_pdas()

    # (1) resolve the token program from the mint's owner (Token vs Token-2022) — a read.
    token_program = read_account_owner(mint, rpc_url=rpc_url, rpc_call=rpc_call)

    # (2) derive/resolve every account Gecko can.
    global_addr = derive_pda(pdas["global"], {}).address
    bonding_curve = derive_pda(pdas["bonding_curve"], {"mint": mint}).address
    associated_bonding_curve = derive_pda(
        pdas["associated_bonding_curve"],
        {"owner": bonding_curve, "token_program": token_program, "mint": mint},
    ).address
    associated_user = derive_pda(
        pdas["associated_user"],
        {"owner": user, "token_program": token_program, "mint": mint},
    ).address
    # creator_vault's seed is bonding_curve.creator — a dotted-path read (#4057).
    creator_vault = resolve_pda(
        pdas, "creator_vault", {"mint": mint}, rpc_url=rpc_url, rpc_call=rpc_call
    ).address
    # bonding_curve_v2 (post-Apr-2026): PURE derivation from ["bonding-curve-v2", mint] —
    # no read; the account is lazily created, only its ADDRESS is required to be correct.
    bonding_curve_v2 = derive_pda(pdas["bonding_curve_v2"], {"mint": mint}).address
    event_authority = derive_pda(pdas["event_authority"], {}).address
    global_volume_accumulator = derive_pda(
        pdas["global_volume_accumulator"], {}
    ).address
    user_volume_accumulator = derive_pda(
        pdas["user_volume_accumulator"], {"user": user}
    ).address
    fee_config = derive_pda(pdas["fee_config"], {}).address

    accounts: dict[str, str] = {
        "global": global_addr,
        "fee_config": fee_config,
        "fee_program": FEE_PROGRAM_ID,
        "mint": mint,
        "bonding_curve": bonding_curve,
        "associated_bonding_curve": associated_bonding_curve,
        "associated_user": associated_user,
        "user": user,
        "system_program": SYSTEM_PROGRAM_ID,
        "token_program": token_program,
        "creator_vault": creator_vault,
        "event_authority": event_authority,
        "program": PUMPFUN_PROGRAM_ID,
        "global_volume_accumulator": global_volume_accumulator,
        "user_volume_accumulator": user_volume_accumulator,
        # Post-Apr-2026 account #17 — the pre-upgrade IDL (and Orquestra's /build) does
        # not know it; builders append it as the first remaining account (see landing_plan).
        "bonding_curve_v2": bonding_curve_v2,
    }

    return {
        "instruction": "buy",
        "accounts": accounts,
        # The two honest gaps of the 18-account current-program set, each with the
        # empirically-established read recipe (never a hardcoded, rotatable pubkey).
        "unresolved": {
            "fee_recipient": {
                "note": _FEE_RECIPIENT_NOTE,
                "resolve": {
                    "read": "global",
                    "field": "fee_recipients[0]",
                    "field_offset": FEE_RECIPIENTS_OFFSET,
                    "encoding": "pubkey",
                },
            },
            "buyback_fee_recipient": {
                "note": _BUYBACK_RECIPIENT_NOTE,
                "resolve": {
                    "read": "global",
                    "field": "buyback_fee_recipients",
                    "field_offset": BUYBACK_FEE_RECIPIENTS_OFFSET,
                    "count": BUYBACK_FEE_RECIPIENTS_COUNT,
                    "encoding": "pubkey",
                },
            },
        },
        # A real buy reverts with AnchorError 3012 (associated_user AccountNotInitialized)
        # if the buyer doesn't already hold the token's ATA — the tx must be preceded by a
        # create-associated-token-account-idempotent instruction (or the buyer must own it).
        # Gecko flags this honestly rather than emit a plan that lands in a revert.
        "preconditions": {
            "associated_user": (
                "must be an initialized ATA for (user, mint); if the buyer doesn't hold it, "
                "prepend a createAssociatedTokenAccountIdempotent instruction before `buy`"
            ),
            "bonding_curve_v2": (
                "created LAZILY by the program — for older mints the account may not "
                "EXIST yet; `buy` needs the correct ADDRESS (seed ['bonding-curve-v2', "
                "mint]), not an existing account. Assert address-correct, never existence."
            ),
        },
        "args": {
            "amount": bindings["amount"],
            "max_sol_cost": bindings["max_sol_cost"],
            # OptionBool wire shape Orquestra's /build requires (not a bare bool).
            "track_volume": _option_bool(bindings["track_volume"]),
        },
        "feePayer": user,
        "build_url": BUILD_URL,
        # The DECLARED ordered plan (compute-budget + ATA prelude + buy) a real builder
        # assembles — the "declare" half. Gecko emits the plan, never a signed/broadcast tx.
        "landing_plan": _landing_plan(accounts),
        # Path B (self-serve): a dev fills fee_recipient, POSTs build_url, then runs the
        # simulate loop themselves. Path A: hand this plan (fee_recipient merged) to
        # Gecko's `simulate` tool. Either way Gecko never signs or broadcasts.
        "simulate": {
            "after": "fill `fee_recipient` (see `unresolved`) then POST build_url to get the tx",
            "rpc_method": "simulateTransaction",
            "params_note": (
                "the tx in the encoding /build reports (Orquestra returns base58) + "
                "{sigVerify:false, replaceRecentBlockhash:true, commitment:'processed'}"
            ),
            "gecko_tool": (
                "simulate  # Path A: hand this plan (with fee_recipient) to Gecko's simulate tool"
            ),
        },
    }


def _buy_plan(
    surface: OrquestraProgramSurface, args: Mapping[str, Any]
) -> dict[str, str]:
    """Intent adapter for the MCP surface: run plan_buy and hand back the resolved
    accounts (the surface wraps them with the /build execute URL). fee_recipient stays
    an honest gap — surfaced in the intent description and the standalone plan_buy."""
    plan = plan_buy(args)
    return plan["accounts"]


_BUY = Intent(
    name="plan_buy",
    instruction="buy",
    description=(
        "Plan a Pump.fun buy against a token's bonding curve. Give mint, user, amount, "
        "max_sol_cost and track_volume; Gecko resolves the token program from the mint "
        "owner and derives every PDA/ATA of the current program's 18-account `buy` it "
        "honestly can — including the dotted-path creator_vault (bonding_curve.creator) "
        "an IDL drops and the post-Apr-2026 bonding_curve_v2. Two honest gaps are "
        "declared as read recipes, never guessed: fee_recipient (a REGULAR recipient, "
        "Global.fee_recipients[0] @ offset 162) and the appended buyback fee recipient "
        "(Global.buyback_fee_recipients @ offset 741)."
    ),
    inputs=("mint", "user", "amount", "max_sol_cost", "track_volume"),
    plan=_buy_plan,
)

# The code half of the config: the plan callables this program exposes, keyed by intent
# name. The config lists the intent NAMES; here we supply their derivation logic.
PUMPFUN_INTENTS: dict[str, Intent] = {_BUY.name: _BUY}


def build_pumpfun_surface() -> OrquestraProgramSurface:
    """Build the Pump.fun surface from packaged config (identity + PDA recipes) + the
    local intent registry (the plan callables)."""
    from .cli import build_surface_from_config

    return build_surface_from_config("orquestra", "pumpfun", PUMPFUN_INTENTS)
