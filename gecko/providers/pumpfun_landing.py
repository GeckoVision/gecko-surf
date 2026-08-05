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

:func:`simulate_sell_landing` is the same loop for the other direction, and its gap is
sharper because there is no prelude to blame. A ``sell`` needs NO ATA step (the seller
already holds the token) and no wSOL wrap (the curve pays native lamports), so the naive
bundle is the builder's instruction with nothing missing *that the surface names*. It
still fails — on the appended accounts the IDL mentions only in an English sentence
("For cashback coins, pass as remaining_accounts: [0] user_volume_accumulator,
[1] bonding_curve_v2") and on the buyback recipient the April-2026 upgrade added. Gecko
reads ``BondingCurve.is_cashback_coin`` to pick between the 16- and 17-account shapes,
quotes ``min_sol_output`` off the sell-side curve formula (NOT the buy formula reversed —
see :mod:`gecko.pump_curve`), and injects the recovered set.

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
from pathlib import Path
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
    quote_min_sol_output,
    read_bonding_curve_reserves,
)
from ..rpc import _http_post_json, default_rpc_call, validate_rpc_url
from ..simulate import Receipt
from .landing_record import record_landing_outcome
from .pumpfun import (
    PUMPFUN_PROGRAM_ID,
    # The offsets + layout provenance live with the DECLARED surface (pumpfun.py), so the
    # plan a consumer reads and the set this orchestrator assembles share one definition.
    BUYBACK_FEE_RECIPIENTS_COUNT,
    BUYBACK_FEE_RECIPIENTS_OFFSET,
    _load_pumpfun_pdas,
    buy_remaining_accounts,
    plan_buy,
    plan_sell,
    sell_remaining_accounts,
)

__all__ = [
    "BuyLandingError",
    "BuyLandingResult",
    "FetchBuyInstruction",
    "SellLandingError",
    "SellLandingResult",
    "FetchSellInstruction",
    "simulate_buy_landing",
    "simulate_sell_landing",
]

# (accounts, args, feePayer) -> Orquestra's built `buy` instruction object. Injectable so
# the orchestrator is falsifiable offline with a canned instruction (no network).
FetchBuyInstruction = Callable[
    [Mapping[str, str], Mapping[str, Any], str], Mapping[str, Any]
]
# The same seam for `sell` — one shape, two instructions.
FetchSellInstruction = FetchBuyInstruction


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


def _fetch_built_instruction(
    accounts: Mapping[str, str],
    args: Mapping[str, Any],
    fee_payer: str,
    *,
    build_url: str,
    error: type[Exception],
) -> Mapping[str, Any]:
    """POST the plan to Orquestra ``/build`` and return its ``instruction`` object.

    Orquestra ``/build`` is single-instruction: it returns the built instruction object
    (and ignores any prelude/compute-budget params) — which is exactly why Gecko assembles
    the preludes itself for the sim. Raises ``error`` on a transport failure or a missing
    ``instruction`` (never echoes the request/response body).
    """
    validate_rpc_url(build_url)
    body = json.dumps(
        {"accounts": dict(accounts), "args": dict(args), "feePayer": fee_payer}
    ).encode()
    try:
        resp = _http_post_json(build_url, body)
    except urllib.error.HTTPError as exc:
        raise error(
            f"build POST to {build_url} failed: HTTP {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise error(f"build POST to {build_url} failed: {exc.reason}") from exc
    instruction = resp.get("instruction")
    if not isinstance(instruction, Mapping):
        raise error(f"build response from {build_url} carried no `instruction` object")
    return instruction


def _fetch_buy_instruction_default(
    accounts: Mapping[str, str],
    args: Mapping[str, Any],
    fee_payer: str,
    *,
    build_url: str,
) -> Mapping[str, Any]:
    """The ``buy`` /build fetch — see :func:`_fetch_built_instruction`."""
    return _fetch_built_instruction(
        accounts, args, fee_payer, build_url=build_url, error=BuyLandingError
    )


def _read_buyback_fee_recipients(
    global_addr: str,
    *,
    rpc_url: str,
    rpc_call: RpcCall | None,
    error: type[Exception] = BuyLandingError,
) -> list[str]:
    """The 8 ``buyback_fee_recipients`` from ``Global`` — a control-plane read of public
    metadata (never stored). These travel as ``remaining_accounts`` the IDL drops."""
    call = rpc_call or default_rpc_call
    resp = call(rpc_url, "getAccountInfo", [global_addr, {"encoding": "base64"}])
    value = (resp.get("result") or {}).get("value")
    if not (isinstance(value, dict) and isinstance(value.get("data"), list)):
        raise error(
            f"Global {global_addr} not found — cannot recover buyback recipients"
        )
    from solders.pubkey import Pubkey

    raw = base64.b64decode(value["data"][0])
    end = BUYBACK_FEE_RECIPIENTS_OFFSET + 32 * BUYBACK_FEE_RECIPIENTS_COUNT
    if len(raw) < end:
        raise error(
            f"Global data is {len(raw)} bytes — too short for buyback_fee_recipients @{BUYBACK_FEE_RECIPIENTS_OFFSET}"
        )
    return [
        str(Pubkey.from_bytes(raw[off : off + 32]))
        for off in range(BUYBACK_FEE_RECIPIENTS_OFFSET, end, 32)
    ]


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
    record_to: str | Path | None = None,
) -> BuyLandingResult:
    """Assemble the Pump.fun landing bundle and simulate it → a :class:`BuyLandingResult`.

    ``bindings`` needs ``mint``, ``user``, ``amount``, ``fee_recipient``, ``track_volume``
    (``max_sol_cost`` is quoted from the curve, NOT taken as input). Reads are control-plane
    only; the unsigned bundle is simulated, never sent. Both the RPC and the Orquestra build
    are injectable, so the whole path is falsifiable offline.

    ``record_to`` is the D2 corpus opt-in — OFF by default (None = today's behavior,
    nothing persisted). When set, ONE categorical ``SimulatedOutcome`` row for the
    landing Receipt is appended to the path's segregated ``simulated.jsonl`` sibling
    (:func:`gecko.providers.landing_record.record_landing_outcome`): status / revert
    family / units / slot / network category + a values-free structural ``recipe_hash``
    — never a pubkey, amount, or log line.
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
    # bonding_curve_v2 is stripped from the /build payload: /build is driven by the
    # pre-upgrade IDL, which has no such named account — it travels as the first APPENDED
    # remaining account instead (same 18-account truth, expressed the way the old builder
    # consumes it, and exactly the shape the live Receipt passed with).
    build_accounts = {k: v for k, v in accounts.items() if k != "bonding_curve_v2"}
    build_accounts["fee_recipient"] = fee_recipient
    fetch = fetch_buy_instruction or (
        lambda a, ar, fp: _fetch_buy_instruction_default(
            a, ar, fp, build_url=plan["build_url"]
        )
    )
    buy_instruction = fetch(build_accounts, plan["args"], user)
    buy_ix = orquestra_instruction_to_solders(buy_instruction)

    # (4) resolve + inject the Class-1 HIDDEN remaining accounts the IDL drops
    # (bonding_curve_v2 — already derived by plan_buy — + the 8 buyback_fee_recipients read
    # from Global) — WITHOUT them a post-upgrade buy reverts (6074 InvalidBondingCurveV2 /
    # 6062 BuybackFeeRecipientMissing), a gap no coding agent finds from the surface. The
    # set comes from buy_remaining_accounts, the SAME function plan_buy declares with — no
    # drift between the declared plan and what the sim verifies.
    buyback = _read_buyback_fee_recipients(
        accounts["global"], rpc_url=rpc_url, rpc_call=rpc_call
    )
    remaining = buy_remaining_accounts(accounts["bonding_curve_v2"], buyback)
    buy_ix_complete = with_remaining_accounts(
        buy_ix, [(entry["pubkey"], entry["isWritable"]) for entry in remaining]
    )

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
        program="pumpfun",
        instruction="buy",
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

    # (6) the D2 corpus opt-in: categorical outcome → segregated simulated.jsonl. The
    # fingerprint reads the plan's NAMES + the packaged seed-KIND graph, never the
    # resolved accounts above. Explicit opt-in only — record_to=None persists nothing.
    if record_to is not None:
        record_landing_outcome(
            landing_receipt,
            program_id=PUMPFUN_PROGRAM_ID,
            instruction="buy",
            account_names=accounts,
            arg_names=plan["args"],
            pdas=pdas,
            network_label=network_label,
            rpc_url=rpc_url,
            rpc_call=rpc_call,
            record_to=record_to,
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


class SellLandingError(Exception):
    """A sell-landing orchestration failure — bad bindings or a build response with no
    ``instruction``. Messages carry only public data, never a secret or a raw body."""


@dataclass(frozen=True)
class SellLandingResult:
    """The side-by-side deliverable for a ``sell``: the Gecko-complete landing Receipt
    next to the naive derive-only Receipt, the curve-quoted proceeds + floor, the shape
    verdict read from the curve, the chosen CU limit, and the DECLARED ordered plan."""

    landing_receipt: Receipt
    derive_only_receipt: Receipt | None
    base_sol_output: int
    min_sol_output: int
    is_cashback_coin: bool
    is_mayhem_mode: bool
    account_count: int
    unit_limit: int
    landing_plan: list[dict[str, Any]]


def _fetch_sell_instruction_default(
    accounts: Mapping[str, str],
    args: Mapping[str, Any],
    fee_payer: str,
    *,
    build_url: str,
) -> Mapping[str, Any]:
    """The ``sell`` /build fetch — see :func:`_fetch_built_instruction`."""
    return _fetch_built_instruction(
        accounts, args, fee_payer, build_url=build_url, error=SellLandingError
    )


def simulate_sell_landing(
    bindings: Mapping[str, Any],
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    unit_price_microlamports: int = 0,
    include_derive_only: bool = True,
    fetch_sell_instruction: FetchSellInstruction | None = None,
    network_label: str | None = None,
    record_to: str | Path | None = None,
) -> SellLandingResult:
    """Assemble the Pump.fun ``sell`` landing bundle and simulate it → a
    :class:`SellLandingResult`.

    The mirror of :func:`simulate_buy_landing`, with two things swapped and one dropped:

    * the quoted arg is ``min_sol_output`` (a FLOOR, discounted by slippage) rather than
      ``max_sol_cost`` (a CEILING, padded by it) — different formula, not a sign flip;
    * the recovered appended set is shape-dependent —
      ``[user_volume_accumulator?, bonding_curve_v2, buyback_fee_recipient]`` — where the
      leading entry appears only for a cashback coin, a fact read from the curve;
    * there is NO ATA prelude. The seller holds the token by definition, so
      ``associated_user`` already exists and the whole prelude is ComputeBudget.

    ``bindings`` needs ``mint``, ``user``, ``amount``, ``fee_recipient``
    (``min_sol_output`` is quoted from the curve, NOT taken as input). Reads are
    control-plane only; the unsigned bundle is simulated, never sent. Both the RPC and the
    Orquestra build are injectable, so the whole path is falsifiable offline.

    ``record_to`` is the D2 corpus opt-in — OFF by default (None = today's behavior,
    nothing persisted). When set, ONE categorical ``SimulatedOutcome`` row for the landing
    Receipt is appended to the path's segregated ``simulated.jsonl`` sibling
    (:func:`gecko.providers.landing_record.record_landing_outcome`): status / revert
    family / units / slot / network category + a values-free structural ``recipe_hash`` —
    never a pubkey, amount, or log line.
    """
    required = ("mint", "user", "amount", "fee_recipient")
    missing = [k for k in required if k not in bindings]
    if missing:
        raise SellLandingError(f"simulate_sell_landing needs bindings {missing}")

    mint = str(bindings["mint"])
    user = str(bindings["user"])
    amount = int(bindings["amount"])
    fee_recipient = str(bindings["fee_recipient"])

    # (1) quote min_sol_output from the curve reserves (the blind slippage arg). The same
    # read also carries the is_cashback_coin flag that decides the account SHAPE.
    pdas = _load_pumpfun_pdas()
    bonding_curve = derive_pda(pdas["bonding_curve"], {"mint": mint}).address
    reserves = read_bonding_curve_reserves(
        bonding_curve, rpc_url=rpc_url, rpc_call=rpc_call
    )
    base_sol_output, min_sol_output = quote_min_sol_output(
        amount, reserves, slippage_bps
    )

    # (2) derive the full account set (+ the declared landing plan) with the quoted arg.
    plan = plan_sell(
        {
            "mint": mint,
            "user": user,
            "amount": amount,
            "min_sol_output": min_sol_output,
        },
        rpc_url=rpc_url,
        rpc_call=rpc_call,
    )
    accounts = dict(plan["accounts"])

    # (3) Orquestra builds the `sell` instruction (its lane), fee_recipient filled. The
    # appended accounts are stripped from the /build payload: /build is driven by the
    # pre-upgrade IDL, whose `sell` names 14 accounts and knows neither bonding_curve_v2
    # nor (for sell) user_volume_accumulator — they travel as remaining accounts instead.
    appended_names = {"bonding_curve_v2", "user_volume_accumulator"}
    build_accounts = {k: v for k, v in accounts.items() if k not in appended_names}
    build_accounts["fee_recipient"] = fee_recipient
    fetch = fetch_sell_instruction or (
        lambda a, ar, fp: _fetch_sell_instruction_default(
            a, ar, fp, build_url=plan["build_url"]
        )
    )
    sell_instruction = fetch(build_accounts, plan["args"], user)
    sell_ix = orquestra_instruction_to_solders(sell_instruction)

    # (4) resolve + inject the Class-1 HIDDEN remaining accounts the surface mentions only
    # in a doc-comment sentence. The set comes from sell_remaining_accounts, the SAME
    # function plan_sell declares with — no drift between the declared plan and the sim.
    buyback = _read_buyback_fee_recipients(
        accounts["global"],
        rpc_url=rpc_url,
        rpc_call=rpc_call,
        error=SellLandingError,
    )
    remaining = sell_remaining_accounts(
        accounts["bonding_curve_v2"],
        buyback[0],
        accounts.get("user_volume_accumulator"),
    )
    sell_ix_complete = with_remaining_accounts(
        sell_ix, [(entry["pubkey"], entry["isWritable"]) for entry in remaining]
    )

    # (5) simulate. The prelude is ComputeBudget only (added by the assembler) — a sell
    # needs no ATA step, which is the honest per-program answer, not an omission.
    landing_receipt, unit_limit = _simulate_landing_bundle(
        sell_ix_complete,
        [],
        user,
        rpc_url=rpc_url,
        rpc_call=rpc_call,
        unit_price_microlamports=unit_price_microlamports,
        track=[user],
        network_label=network_label,
        program="pumpfun",
        instruction="sell",
    )

    derive_only_receipt: Receipt | None = None
    if include_derive_only:
        # The NAIVE bundle: the builder's 14-account `sell` VERBATIM — what an agent gets
        # from the IDL. Nothing about it looks wrong; it reverts on the accounts that only
        # exist in a doc-comment sentence.
        derive_only_receipt, _ = _simulate_landing_bundle(
            sell_ix,
            [],
            user,
            rpc_url=rpc_url,
            rpc_call=rpc_call,
            unit_price_microlamports=unit_price_microlamports,
            track=[user],
            network_label=network_label,
        )

    # (6) the D2 corpus opt-in: categorical outcome → segregated simulated.jsonl. The
    # fingerprint reads the plan's NAMES + the packaged seed-KIND graph, never the
    # resolved accounts above. Explicit opt-in only — record_to=None persists nothing.
    if record_to is not None:
        record_landing_outcome(
            landing_receipt,
            program_id=PUMPFUN_PROGRAM_ID,
            instruction="sell",
            account_names=accounts,
            arg_names=plan["args"],
            pdas=pdas,
            network_label=network_label,
            rpc_url=rpc_url,
            rpc_call=rpc_call,
            record_to=record_to,
        )

    return SellLandingResult(
        landing_receipt=landing_receipt,
        derive_only_receipt=derive_only_receipt,
        base_sol_output=base_sol_output,
        min_sol_output=min_sol_output,
        is_cashback_coin=bool(plan["cashback"]["is_cashback_coin"]),
        is_mayhem_mode=bool(plan["cashback"]["is_mayhem_mode"]),
        account_count=int(plan["cashback"]["account_count"]),
        landing_plan=_declare_remaining_in_plan(
            plan["landing_plan"], remaining, kind="sell"
        ),
        unit_limit=unit_limit,
    )


def _declare_remaining_in_plan(
    landing_plan: list[dict[str, Any]],
    remaining: list[dict[str, Any]],
    *,
    kind: str = "buy",
) -> list[dict[str, Any]]:
    """Complete the declared ``kind`` step with the RESOLVED remaining-accounts set (the
    exact list the simulated bundle injected — same ``*_remaining_accounts`` output), so
    the real builder (Orquestra) and signer (1claw) include them in the signable tx. The
    plan's resolver-style ``remaining_accounts_unresolved`` recipe is dropped: resolved."""
    notes = {
        "buy": (
            "RESOLVED: bonding_curve_v2 (seed ['bonding-curve-v2', mint]) then the 8 "
            "Global.buyback_fee_recipients@741 — exactly the set the simulated bundle "
            "injected (no drift between declare and verify)"
        ),
        "sell": (
            "RESOLVED: (cashback coins only) user_volume_accumulator, then "
            "bonding_curve_v2 read-only (seed ['bonding-curve-v2', mint]), then ONE "
            "Global.buyback_fee_recipients@741 entry — exactly the set the simulated "
            "bundle injected (no drift between declare and verify)"
        ),
    }
    declared = [dict(step) for step in landing_plan]
    for step in declared:
        if step.get("kind") == kind:
            step["remaining_accounts"] = [dict(entry) for entry in remaining]
            step.pop("remaining_accounts_unresolved", None)
            step["remaining_accounts_note"] = notes[kind]
    return declared
