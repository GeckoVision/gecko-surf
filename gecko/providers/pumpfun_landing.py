"""The Pump.fun "buy-that-passes" orchestrator — the Berkay a-ha.

Today a derive-only ``buy`` (correct accounts, correct program instruction) simulates to a
REVERT: AnchorError 3012, the buyer's ``associated_user`` ATA is not initialized. Deriving
accounts is table stakes; making the call LAND is the differentiator. This module closes
that last gap for the $0 SIMULATION ONLY:

  1. reads the ``bonding_curve`` reserves and quotes ``max_sol_cost`` (the blind slippage
     arg the IDL cannot give) — :mod:`gecko.pump_curve`;
  2. has Orquestra ``/build`` build the ``buy`` instruction (Orquestra's lane — the
     IDL-hard part), fee_recipient filled;
  3. RECOVERS the Class-1 hidden ``remaining_accounts`` the IDL drops and injects them —
     ``bonding_curve_v2`` (seed ``["bonding-curve-v2", mint]``) then the 8
     ``Global.buyback_fee_recipients`` — without which a post-upgrade buy reverts 6074 /
     6062 (the live Receipt discovered this; both were flagged in the gap-map);
  4. assembles the STANDARD preludes AROUND it — ``createAssociatedTokenAccountIdempotent``
     + ``ComputeBudget`` — into an UNSIGNED bundle and ``simulateTransaction``s it
     (:mod:`gecko.landing`);
  5. returns the side-by-side Receipt: derive-only → ``account_error`` (3012);
     Gecko-complete landing bundle → ``pass``.

The compose boundary holds exactly: **Gecko assembles the standard preludes only to prove
the bundle lands ($0, sigVerify:false, replaceRecentBlockhash:true), and DECLARES the
ordered plan. It never signs, never sends, never broadcasts.** Orquestra builds the real
signable tx; 1claw signs the bound receipt.

Control-plane invariant #1: the reserve read + the built instruction are public metadata
held in memory; the assembled bundle and the Receipt are returned, never stored.
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import base64

from ..landing import (
    create_idempotent_ata_ix,
    orquestra_instruction_to_solders,
    with_remaining_accounts,
)
from ..landing import simulate_landing_bundle as _simulate_landing_bundle
from ..pda import derive_pda
from ..pda_testkit import LOCAL_RPC, RpcCall
from ..pump_curve import (
    DEFAULT_SLIPPAGE_BPS,
    quote_max_sol_cost,
    read_bonding_curve_reserves,
)
from ..rpc import _http_post_json, default_rpc_call, validate_rpc_url
from ..simulate import Receipt
from .pumpfun import _load_pumpfun_pdas, plan_buy

# Global.buyback_fee_recipients is an [pubkey; 8] array at data offset 741 — computed from
# the on-chain Global struct field order (8 disc + initialized + authority + fee_recipient +
# 5×u64 + withdraw_authority + enable_migrate + 2×u64 + fee_recipients[7] + set/admin
# creator authority + create_v2_enabled + whitelist_pda + reserved_fee_recipient +
# mayhem_mode_enabled + reserved_fee_recipients[7] + is_cashback_enabled). LAYOUT-FRAGILE
# by nature (this is exactly the drift the gap-map warns about) — the Receipt confirms it.
_BUYBACK_RECIPIENTS_OFFSET = 741
_BUYBACK_RECIPIENTS_COUNT = 8

__all__ = [
    "BuyLandingError",
    "BuyLandingResult",
    "FetchBuyInstruction",
    "simulate_buy_landing",
]

# (accounts, args, feePayer) -> Orquestra's built `buy` instruction object. Injectable so
# the orchestrator is falsifiable offline with a canned instruction (no network).
FetchBuyInstruction = Callable[
    [Mapping[str, str], Mapping[str, Any], str], Mapping[str, Any]
]


class BuyLandingError(Exception):
    """A buy-landing orchestration failure — bad bindings or a build response with no
    ``instruction``. Messages carry only public data, never a secret or a raw body."""


@dataclass(frozen=True)
class BuyLandingResult:
    """The side-by-side deliverable: the Gecko-complete landing Receipt (should ``pass``)
    next to the derive-only Receipt (should be ``account_error`` / 3012), plus the quoted
    cost, the chosen CU limit, and the DECLARED ordered plan Orquestra/1claw assemble."""

    landing_receipt: Receipt
    derive_only_receipt: Receipt | None
    base_sol_cost: int
    max_sol_cost: int
    unit_limit: int
    landing_plan: list[dict[str, Any]]


def _fetch_buy_instruction_default(
    accounts: Mapping[str, str],
    args: Mapping[str, Any],
    fee_payer: str,
    *,
    build_url: str,
) -> Mapping[str, Any]:
    """POST the plan to Orquestra ``/build`` and return its ``instruction`` object.

    Orquestra ``/build`` is single-instruction: it returns the built ``buy`` as an
    ``instruction`` object (and ignores any prelude/compute-budget params) — which is
    exactly why Gecko assembles the preludes itself for the sim. Raises
    :class:`BuyLandingError` on a transport failure or a missing ``instruction`` (never
    echoes the request/response body).
    """
    validate_rpc_url(build_url)
    body = json.dumps(
        {"accounts": dict(accounts), "args": dict(args), "feePayer": fee_payer}
    ).encode()
    try:
        resp = _http_post_json(build_url, body)
    except urllib.error.HTTPError as exc:
        raise BuyLandingError(
            f"build POST to {build_url} failed: HTTP {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise BuyLandingError(
            f"build POST to {build_url} failed: {exc.reason}"
        ) from exc
    instruction = resp.get("instruction")
    if not isinstance(instruction, Mapping):
        raise BuyLandingError(
            f"build response from {build_url} carried no `instruction` object"
        )
    return instruction


def _read_buyback_fee_recipients(
    global_addr: str, *, rpc_url: str, rpc_call: RpcCall | None
) -> list[str]:
    """The 8 ``buyback_fee_recipients`` from ``Global`` — a control-plane read of public
    metadata (never stored). These travel as ``remaining_accounts`` the IDL drops."""
    call = rpc_call or default_rpc_call
    resp = call(rpc_url, "getAccountInfo", [global_addr, {"encoding": "base64"}])
    value = (resp.get("result") or {}).get("value")
    if not (isinstance(value, dict) and isinstance(value.get("data"), list)):
        raise BuyLandingError(
            f"Global {global_addr} not found — cannot recover buyback recipients"
        )
    from solders.pubkey import Pubkey

    raw = base64.b64decode(value["data"][0])
    end = _BUYBACK_RECIPIENTS_OFFSET + 32 * _BUYBACK_RECIPIENTS_COUNT
    if len(raw) < end:
        raise BuyLandingError(
            f"Global data is {len(raw)} bytes — too short for buyback_fee_recipients @{_BUYBACK_RECIPIENTS_OFFSET}"
        )
    return [
        str(Pubkey.from_bytes(raw[off : off + 32]))
        for off in range(_BUYBACK_RECIPIENTS_OFFSET, end, 32)
    ]


def _recover_hidden_remaining_accounts(
    mint: str,
    global_addr: str,
    pdas: Mapping[str, Any],
    *,
    rpc_url: str,
    rpc_call: RpcCall | None,
) -> list[tuple[str, bool]]:
    """Recover the two Class-1 hidden requirements a post-upgrade ``buy`` needs but the IDL
    (and Orquestra's IDL-driven ``/build``) omit, in on-chain order: ``bonding_curve_v2``
    (derived from ``["bonding-curve-v2", mint]``) then the 8 ``buyback_fee_recipients``
    (read from ``Global``). Each ``(pubkey, is_writable)``, all writable / non-signer.
    """
    bonding_curve_v2 = derive_pda(pdas["bonding_curve_v2"], {"mint": mint}).address
    buyback = _read_buyback_fee_recipients(
        global_addr, rpc_url=rpc_url, rpc_call=rpc_call
    )
    return [(bonding_curve_v2, True)] + [(addr, True) for addr in buyback]


def simulate_buy_landing(
    bindings: Mapping[str, Any],
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    unit_price_microlamports: int = 0,
    include_derive_only: bool = True,
    fetch_buy_instruction: FetchBuyInstruction | None = None,
    network_label: str | None = None,
) -> BuyLandingResult:
    """Assemble the Pump.fun landing bundle and simulate it → a :class:`BuyLandingResult`.

    ``bindings`` needs ``mint``, ``user``, ``amount``, ``fee_recipient``, ``track_volume``
    (``max_sol_cost`` is quoted from the curve, NOT taken as input). Reads are control-plane
    only; the unsigned bundle is simulated, never sent. Both the RPC and the Orquestra build
    are injectable, so the whole path is falsifiable offline.
    """
    required = ("mint", "user", "amount", "fee_recipient", "track_volume")
    missing = [k for k in required if k not in bindings]
    if missing:
        raise BuyLandingError(f"simulate_buy_landing needs bindings {missing}")

    mint = str(bindings["mint"])
    user = str(bindings["user"])
    amount = int(bindings["amount"])
    fee_recipient = str(bindings["fee_recipient"])

    # (1) quote max_sol_cost from the curve reserves (the blind slippage arg). The
    # bonding_curve address is a pure derivation from the mint; reading it is the same
    # control-plane read plan_buy already makes for creator_vault.
    pdas = _load_pumpfun_pdas()
    bonding_curve = derive_pda(pdas["bonding_curve"], {"mint": mint}).address
    reserves = read_bonding_curve_reserves(
        bonding_curve, rpc_url=rpc_url, rpc_call=rpc_call
    )
    base_sol_cost, max_sol_cost = quote_max_sol_cost(amount, reserves, slippage_bps)

    # (2) derive the full account set (+ the declared landing plan) with the quoted arg.
    plan = plan_buy(
        {
            "mint": mint,
            "user": user,
            "amount": amount,
            "max_sol_cost": max_sol_cost,
            "track_volume": bindings["track_volume"],
        },
        rpc_url=rpc_url,
        rpc_call=rpc_call,
    )
    accounts = dict(plan["accounts"])
    token_program = accounts["token_program"]
    associated_user = accounts["associated_user"]

    # (3) Orquestra builds the `buy` instruction (its lane), fee_recipient filled.
    build_accounts = {**accounts, "fee_recipient": fee_recipient}
    fetch = fetch_buy_instruction or (
        lambda a, ar, fp: _fetch_buy_instruction_default(
            a, ar, fp, build_url=plan["build_url"]
        )
    )
    buy_instruction = fetch(build_accounts, plan["args"], user)
    buy_ix = orquestra_instruction_to_solders(buy_instruction)

    # (4) recover the Class-1 HIDDEN remaining accounts the IDL drops (bonding_curve_v2 +
    # the 8 buyback_fee_recipients) and inject them into the buy — WITHOUT them a
    # post-upgrade buy reverts (6074 InvalidBondingCurveV2 / 6062 BuybackFeeRecipientMissing),
    # a gap no coding agent finds from the surface. These are DECLARED in landing_plan too.
    remaining = _recover_hidden_remaining_accounts(
        mint, accounts["global"], pdas, rpc_url=rpc_url, rpc_call=rpc_call
    )
    buy_ix_complete = with_remaining_accounts(buy_ix, remaining)

    # (5) assemble the STANDARD ATA prelude around the completed buy and simulate → the a-ha.
    ata_ix = create_idempotent_ata_ix(
        payer=user,
        owner=user,
        mint=mint,
        ata=associated_user,
        token_program=token_program,
    )
    landing_receipt, unit_limit = _simulate_landing_bundle(
        buy_ix_complete,
        [ata_ix],
        user,
        rpc_url=rpc_url,
        rpc_call=rpc_call,
        unit_price_microlamports=unit_price_microlamports,
        track=[user],
        network_label=network_label,
    )

    derive_only_receipt: Receipt | None = None
    if include_derive_only:
        # The RAW Orquestra buy — what a coding agent gets from the IDL: no ATA prelude, no
        # recovered remaining accounts. It reverts at the first gap (3012, the buyer's ATA).
        derive_only_receipt, _ = _simulate_landing_bundle(
            buy_ix,
            [],
            user,
            rpc_url=rpc_url,
            rpc_call=rpc_call,
            unit_price_microlamports=unit_price_microlamports,
            track=[user],
            network_label=network_label,
        )

    landing_plan = _declare_remaining_in_plan(plan["landing_plan"], remaining)
    return BuyLandingResult(
        landing_receipt=landing_receipt,
        derive_only_receipt=derive_only_receipt,
        base_sol_cost=base_sol_cost,
        max_sol_cost=max_sol_cost,
        unit_limit=unit_limit,
        landing_plan=landing_plan,
    )


def _declare_remaining_in_plan(
    landing_plan: list[dict[str, Any]], remaining: list[tuple[str, bool]]
) -> list[dict[str, Any]]:
    """Attach the recovered hidden remaining accounts to the declared ``buy`` step, so the
    real builder (Orquestra) and signer (1claw) include them in the signable tx."""
    declared = [dict(step) for step in landing_plan]
    for step in declared:
        if step.get("kind") == "buy":
            step["remaining_accounts"] = [
                {"pubkey": pubkey, "isWritable": writable, "isSigner": False}
                for pubkey, writable in remaining
            ]
            step["remaining_accounts_note"] = (
                "Class-1 recovered: bonding_curve_v2 (seed ['bonding-curve-v2', mint]) then "
                "the 8 Global.buyback_fee_recipients — remaining_accounts the IDL drops"
            )
    return declared
