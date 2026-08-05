"""The Meteora DLMM "swap-that-passes" orchestrator — the second program that RUNS.

A derive-only ``swap`` (correct named accounts, correct built instruction) cannot land:
the bin_array remaining accounts the IDL never names are absent, the user's token ATAs
may not exist, ``min_amount_out`` is blind, and a SOL leg needs wrap/unwrap. This module
closes those gaps for the $0 SIMULATION ONLY, mirroring
:mod:`gecko.providers.pumpfun_landing`:

  1. :func:`gecko.providers.meteora.plan_swap` reads the pool state ONCE and declares
     everything — the account set, the bitmap-selected bin_arrays, the state-quoted
     ``min_amount_out``, the ordered landing plan;
  2. Orquestra ``/build`` builds the ``swap`` instruction (its lane — the IDL-hard
     part). Wire fact (discovered live): omitted OPTIONAL accounts come back as
     EMPTY-string pubkeys in the built instruction; per Anchor's optional-account
     convention those slots are filled with the PROGRAM id, never dropped (the account
     list is positional) and never fabricated;
  3. the bin_array PDAs are appended as remaining accounts — the Class-1 requirement
     no IDL-driven client can supply;
  4. the STANDARD preludes/postludes are assembled AROUND it — createIdempotentATA for
     both legs, the wSOL wrap (transfer + SyncNative) before and CloseAccount after
     when a leg is native SOL, ComputeBudget — into an UNSIGNED bundle that is
     ``simulateTransaction``-ed (:mod:`gecko.landing`);
  5. the side-by-side Receipt comes back: derive-only → revert; Gecko-complete
     landing bundle → pass.

The compose boundary holds exactly: **Gecko assembles the standard instructions only to
prove the bundle lands ($0, sigVerify:false, replaceRecentBlockhash:true), and DECLARES
the ordered plan. It never signs, never sends, never broadcasts.** Orquestra builds the
real signable tx; 1claw signs the bound receipt.

Control-plane invariant #1: the pool-state read + the built instruction are public
metadata held in memory; the assembled bundle and the Receipt are returned, never stored.
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..landing import (
    NATIVE_SOL_MINT,
    close_account_ix,
    create_idempotent_ata_ix,
    orquestra_instruction_to_solders,
    with_remaining_accounts,
    wrap_sol_ixs,
)
from ..landing import simulate_landing_bundle as _simulate_landing_bundle
from ..meteora_math import DEFAULT_SLIPPAGE_BPS
from ..pda_testkit import LOCAL_RPC, RpcCall
from ..rpc import _http_post_json, validate_rpc_url
from ..simulate import Receipt
from .landing_record import record_landing_outcome
from .meteora import METEORA_PROGRAM_ID, _load_meteora_pdas, plan_swap

__all__ = [
    "FetchSwapInstruction",
    "SwapLandingError",
    "SwapLandingResult",
    "simulate_swap_landing",
]

# (accounts, args, feePayer) -> Orquestra's built `swap` instruction object. Injectable
# so the orchestrator is falsifiable offline with a canned instruction (no network).
FetchSwapInstruction = Callable[
    [Mapping[str, str], Mapping[str, Any], str], Mapping[str, Any]
]


class SwapLandingError(Exception):
    """A swap-landing orchestration failure — bad bindings or a build response with no
    ``instruction``. Messages carry only public data, never a secret or a raw body."""


@dataclass(frozen=True)
class SwapLandingResult:
    """The side-by-side deliverable: the Gecko-complete landing Receipt next to the
    derive-only Receipt, plus the snapshot quote, the recovered bin_array set, the
    chosen CU limit, and the DECLARED ordered plan Orquestra/1claw assemble."""

    landing_receipt: Receipt
    derive_only_receipt: Receipt | None
    expected_out_snapshot: int
    min_amount_out: int
    bin_array_indexes: list[int]
    bin_arrays: list[str]
    unit_limit: int
    landing_plan: list[dict[str, Any]]


def _fetch_swap_instruction_default(
    accounts: Mapping[str, str],
    args: Mapping[str, Any],
    fee_payer: str,
    *,
    build_url: str,
) -> Mapping[str, Any]:
    """POST the plan to Orquestra ``/build`` and return its ``instruction`` object.

    ``/build`` is single-instruction: it returns the built ``swap`` as an
    ``instruction`` object and ignores prelude/remaining-account params — exactly why
    Gecko assembles those itself for the sim. Raises :class:`SwapLandingError` on a
    transport failure or a missing ``instruction`` (never echoes the body).
    """
    validate_rpc_url(build_url)
    body = json.dumps(
        {"accounts": dict(accounts), "args": dict(args), "feePayer": fee_payer}
    ).encode()
    try:
        resp = _http_post_json(build_url, body)
    except urllib.error.HTTPError as exc:
        raise SwapLandingError(
            f"build POST to {build_url} failed: HTTP {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SwapLandingError(
            f"build POST to {build_url} failed: {exc.reason}"
        ) from exc
    instruction = resp.get("instruction")
    if not isinstance(instruction, Mapping):
        raise SwapLandingError(
            f"build response from {build_url} carried no `instruction` object"
        )
    return instruction


def _fill_optional_account_slots(
    instruction: Mapping[str, Any], program_id: str
) -> dict[str, Any]:
    """Replace EMPTY-pubkey account slots with the program id (read-only, non-signer).

    Wire finding: when an optional account (``bin_array_bitmap_extension``,
    ``host_fee_in``) is omitted from ``/build``, Orquestra returns its positional slot
    with ``"pubkey": ""``. Anchor's convention for an omitted optional account is the
    PROGRAM'S OWN id in that slot — so the slot is filled with a verifiable public
    constant, never dropped (the list is positional) and never guessed.
    """
    accounts = instruction.get("accounts")
    if not isinstance(accounts, list):
        return dict(instruction)
    filled = [
        {"pubkey": program_id, "isSigner": False, "isWritable": False}
        if isinstance(entry, Mapping) and not entry.get("pubkey")
        else entry
        for entry in accounts
    ]
    return {**instruction, "accounts": filled}


def simulate_swap_landing(
    bindings: Mapping[str, Any],
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    unit_price_microlamports: int = 0,
    include_derive_only: bool = True,
    fetch_swap_instruction: FetchSwapInstruction | None = None,
    network_label: str | None = None,
    record_to: str | Path | None = None,
) -> SwapLandingResult:
    """Assemble the Meteora swap landing bundle and simulate it → a
    :class:`SwapLandingResult`.

    ``bindings`` needs ``input_mint``, ``output_mint``, ``bin_step``, ``base_factor``,
    ``user``, ``amount_in`` (``min_amount_out`` is quoted from the pool state, NOT
    taken as input). Reads are control-plane only; the unsigned bundle is simulated,
    never sent. Both the RPC and the Orquestra build are injectable, so the whole path
    is falsifiable offline.

    ``record_to`` is the D2 corpus opt-in — OFF by default (None = today's behavior,
    nothing persisted). When set, ONE categorical ``SimulatedOutcome`` row for the
    landing Receipt is appended to the path's segregated ``simulated.jsonl`` sibling
    (:func:`gecko.providers.landing_record.record_landing_outcome`): status / revert
    family / units / slot / network category + a values-free structural ``recipe_hash``
    — never a pubkey, amount, or log line.
    """
    required = (
        "input_mint",
        "output_mint",
        "bin_step",
        "base_factor",
        "user",
        "amount_in",
    )
    missing = [k for k in required if k not in bindings]
    if missing:
        raise SwapLandingError(f"simulate_swap_landing needs bindings {missing}")

    input_mint = str(bindings["input_mint"])
    output_mint = str(bindings["output_mint"])
    user = str(bindings["user"])
    amount_in = int(bindings["amount_in"])

    # (1) the full declared plan: ONE pool-state read → accounts, bin_arrays, quote.
    plan = plan_swap(
        bindings, rpc_url=rpc_url, rpc_call=rpc_call, slippage_bps=slippage_bps
    )
    accounts: dict[str, str] = dict(plan["accounts"])
    bin_arrays = [entry["pubkey"] for entry in plan["remaining_accounts"]]
    bin_array_indexes = [entry["index"] for entry in plan["remaining_accounts"]]

    # (2) Orquestra builds the `swap` instruction (its lane); empty OPTIONAL slots are
    # filled with the program id per Anchor convention (see _fill_optional_account_slots).
    fetch = fetch_swap_instruction or (
        lambda a, ar, fp: _fetch_swap_instruction_default(
            a, ar, fp, build_url=plan["build_url"]
        )
    )
    built = fetch(accounts, plan["args"], user)
    swap_ix = orquestra_instruction_to_solders(
        _fill_optional_account_slots(built, accounts["program"])
    )

    # (3) append the Class-1 bin_array remaining accounts the IDL never names.
    swap_ix_complete = with_remaining_accounts(
        swap_ix, [(addr, True) for addr in bin_arrays]
    )

    # (4) the STANDARD preludes/postludes around it, in the declared order: both user
    # ATAs idempotent, the wSOL wrap when the input is native SOL, the CloseAccount
    # unwrap when either leg is (mirrors landing_plan exactly — the no-drift guard in
    # the tests compares the two).
    token_program_of = {
        accounts["token_x_mint"]: accounts["token_x_program"],
        accounts["token_y_mint"]: accounts["token_y_program"],
    }
    prelude_ixs = [
        create_idempotent_ata_ix(
            payer=user,
            owner=user,
            mint=mint,
            ata=accounts[ata_key],
            token_program=token_program_of[mint],
        )
        for ata_key, mint in (
            ("user_token_in", input_mint),
            ("user_token_out", output_mint),
        )
    ]
    if input_mint == NATIVE_SOL_MINT:
        prelude_ixs.extend(wrap_sol_ixs(user, accounts["user_token_in"], amount_in))
    postlude_ixs = []
    if NATIVE_SOL_MINT in (input_mint, output_mint):
        wsol_ata = accounts[
            "user_token_in" if input_mint == NATIVE_SOL_MINT else "user_token_out"
        ]
        postlude_ixs.append(close_account_ix(wsol_ata, user, user))

    landing_receipt, unit_limit = _simulate_landing_bundle(
        swap_ix_complete,
        prelude_ixs,
        user,
        rpc_url=rpc_url,
        rpc_call=rpc_call,
        unit_price_microlamports=unit_price_microlamports,
        track=[user],
        network_label=network_label,
        postlude_ixs=postlude_ixs,
    )

    derive_only_receipt: Receipt | None = None
    if include_derive_only:
        # The RAW Orquestra swap — what a coding agent gets from the IDL: no bin_arrays,
        # no ATAs, no wrap. It reverts at the first gap.
        derive_only_receipt, _ = _simulate_landing_bundle(
            swap_ix,
            [],
            user,
            rpc_url=rpc_url,
            rpc_call=rpc_call,
            unit_price_microlamports=unit_price_microlamports,
            track=[user],
            network_label=network_label,
        )

    # (5) the D2 corpus opt-in: categorical outcome → segregated simulated.jsonl. The
    # fingerprint reads the plan's NAMES + the packaged seed-KIND graph, never the
    # resolved accounts above. Explicit opt-in only — record_to=None persists nothing.
    if record_to is not None:
        record_landing_outcome(
            landing_receipt,
            program_id=METEORA_PROGRAM_ID,
            instruction="swap",
            account_names=accounts,
            arg_names=plan["args"],
            pdas=_load_meteora_pdas(),
            network_label=network_label,
            rpc_url=rpc_url,
            rpc_call=rpc_call,
            record_to=record_to,
        )

    return SwapLandingResult(
        landing_receipt=landing_receipt,
        derive_only_receipt=derive_only_receipt,
        expected_out_snapshot=plan["quote"]["expected_out_snapshot"],
        min_amount_out=plan["args"]["min_amount_out"],
        bin_array_indexes=bin_array_indexes,
        bin_arrays=bin_arrays,
        unit_limit=unit_limit,
        landing_plan=plan["landing_plan"],
    )
