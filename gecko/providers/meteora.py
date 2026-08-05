"""Meteora DLMM — the first Orquestra provider instance (the touchable Berkay demo).

Wires the generic Orquestra surface (:mod:`gecko.providers.orquestra`) to Meteora DLMM:
its program id, the recovered PDA recipes, the Orquestra project's build base, and a
``swap`` intent. The star is ``lb_pair`` — the helper-seeded root Orquestra returns
``"has no PDA seeds"`` for; here it's derivable, and the whole account set falls out.

The PDA recipes below are the program's **public on-chain seed facts** for Meteora DLMM
(`LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo`) — authored as data (not copied source), so
this ships license-clean. `lb_pair = [min(x,y), max(x,y), bin_step, base_factor]`,
`reserve = [lb_pair, token_mint]`, `oracle = [b"oracle", lb_pair]`,
`bin_array = [b"bin_array", lb_pair, index(i64 LE)]`.

The `lb_pair` recipe carries a 4th seed — `base_factor:u16` (LE) — since the dlmm-sdk
deprecated the 3-seed scheme (`derive_lb_pair_pda`) for `derive_lb_pair_pda2` in PR #49
(merged 2024-05-09). base_factor selects among fee-tier pools that share the same
(mint-pair, bin_step), so it is NOT derivable from the mints — the caller MUST supply the
fee tier. Dropping it silently derives the WRONG pool for every pool created after that
upgrade (source: MeteoraAg/dlmm-sdk `commons/src/pda.rs::derive_lb_pair_pda2`).

``plan_swap`` is the full DECLARED plan (mirrors pump's ``plan_buy``): the complete
named-account set the ``swap`` instruction takes (Orquestra-verified), the bin_array
remaining accounts the IDL never names (a live bitmap-walk state read —
:mod:`gecko.meteora_math`), the state-quoted ``min_amount_out``, and the ordered
``landing_plan`` including the wSOL wrap/close legs when a side is native SOL.

Run it (published 0.9.3+):
    # stdio, add straight into Claude Code:
    claude mcp add orquestra-meteora -- \
        uvx --from "gecko-surf[serve,solana]" gecko-orquestra --program meteora --stdio
    # or HTTP:
    uvx --from "gecko-surf[serve,solana]" gecko-orquestra --program meteora
"""

from __future__ import annotations

import sys
from typing import Any, Mapping

from ..find_start import PreludeSpec, StartSpec
from ..landing import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    COMPUTE_BUDGET_PROGRAM_ID,
    NATIVE_SOL_MINT,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from ..meteora_math import (
    DEFAULT_SLIPPAGE_BPS,
    LbPairState,
    quote_min_amount_out,
    read_lb_pair_state,
    swap_bin_array_indexes,
    swap_for_y,
)
from ..pda import PdaNode, derive_pda
from ..pda_resolve import read_account_owner
from ..pda_testkit import LOCAL_RPC, RpcCall
from ..provider_config import load_packaged_provider
from .orquestra import Intent, OrquestraProgramSurface

__all__ = [
    "METEORA_PROGRAM_ID",
    "METEORA_INTENTS",
    "METEORA_STARTS",
    "plan_swap",
    "build_meteora_surface",
    "main",
]

# Display constant, kept for consumers. The authoritative program id + PDA recipes
# now live in the packaged config (gecko/providers/configs/orquestra/meteora.json),
# loaded by build_meteora_surface — this is data, not code.
METEORA_PROGRAM_ID = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"

# How many bin arrays the plan declares, nearest-first in the swap direction — the
# dlmm-sdk's own swap-quote default. Fewer risks CrossedMaxBinId on a large swap;
# the sim Receipt is the check.
_BIN_ARRAY_TAKE_COUNT = 3


def _load_meteora_pdas() -> dict[str, PdaNode]:
    _, apis = load_packaged_provider("orquestra")
    program = apis["meteora"].program
    if program is None:
        raise ValueError("meteora config carries no program spec")
    return dict(program.pdas)


def _landing_plan(
    accounts: Mapping[str, str],
    bindings: Mapping[str, str],
    bin_arrays: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The DECLARED ordered instruction plan a real builder (Orquestra) assembles +
    1claw signs — pure structured data (no bytes, no signing):
    ``[SetComputeUnitLimit, SetComputeUnitPrice, createIdempotentATA(in),
    createIdempotentATA(out), (wrap wSOL: transfer + syncNative)?, swap,
    (closeAccount wSOL)?]``. The wSOL legs appear only when a side's mint is native
    SOL. The compute-budget values are filled by the simulate step; this is the
    ordered contract, not a tx. See
    :func:`gecko.providers.meteora_landing.simulate_swap_landing`.
    """
    user = bindings["user"]
    input_mint, output_mint = bindings["input_mint"], bindings["output_mint"]
    plan: list[dict[str, Any]] = [
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
    ]
    for leg, mint in (("in", input_mint), ("out", output_mint)):
        ata_key = f"user_token_{leg}"
        plan.append(
            {
                "kind": "create_idempotent_ata",
                "program": ASSOCIATED_TOKEN_PROGRAM_ID,
                "accounts": {
                    "payer": user,
                    "ata": accounts[ata_key],
                    "owner": user,
                    "mint": mint,
                    "system_program": SYSTEM_PROGRAM_ID,
                    "token_program": accounts[
                        "token_x_program"
                        if mint == accounts["token_x_mint"]
                        else "token_y_program"
                    ],
                },
                "note": f"initializes the user's {ata_key} ATA (no-op if it exists)",
            }
        )
    if input_mint == NATIVE_SOL_MINT:
        wsol_ata = accounts["user_token_in"]
        plan.append(
            {
                "kind": "wrap_sol",
                "program": SYSTEM_PROGRAM_ID,
                "accounts": {"from": user, "wsol_ata": wsol_ata},
                "note": (
                    "System transfer of amount_in lamports into the wSOL ATA, then "
                    "SPL-Token SyncNative — the input side moves as wrapped SOL"
                ),
            }
        )
    plan.append(
        {
            "kind": "swap",
            "program": METEORA_PROGRAM_ID,
            "accounts": dict(accounts),
            "remaining_accounts": bin_arrays,
            "remaining_accounts_note": (
                "Class-1 recovered: the bin_array PDAs the swap traverses — "
                "remaining_accounts the IDL never names, selected by the pool's live "
                "liquidity bitmap from active_id (see gecko.meteora_math)"
            ),
            "note": (
                "Orquestra builds this instruction; min_amount_out is the "
                "state-quoted slippage guard (snapshot price − slippage_bps)"
            ),
        }
    )
    if NATIVE_SOL_MINT in (input_mint, output_mint):
        wsol_leg = (
            "user_token_in" if input_mint == NATIVE_SOL_MINT else "user_token_out"
        )
        plan.append(
            {
                "kind": "close_wsol_ata",
                "program": TOKEN_PROGRAM_ID,
                "accounts": {
                    "account": accounts[wsol_leg],
                    "destination": user,
                    "owner": user,
                },
                "note": "CloseAccount unwraps the wSOL leg back to SOL (and recovers rent)",
            }
        )
    return plan


def plan_swap(
    bindings: Mapping[str, Any],
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
) -> dict[str, Any]:
    """Assemble the full DECLARED plan for a Meteora DLMM ``swap`` → an Orquestra
    ``/build`` payload plus the ordered landing plan.

    ``bindings`` needs ``input_mint``, ``output_mint``, ``bin_step``, ``base_factor``,
    ``user``, ``amount_in`` (``min_amount_out`` is quoted from the pool state, NOT
    taken as input). Gecko derives the pool root (``lb_pair`` — the helper-seeded PDA
    Orquestra's IDL drops), every leaf, and the user ATAs; then makes ONE control-plane
    read of the pool account (public metadata, never stored) to (a) confirm the pool
    exists and the mints match, (b) resolve the token programs, (c) select the
    bin_array remaining accounts via the SDK's liquidity-bitmap walk, and (d) quote
    ``min_amount_out`` at the active-bin snapshot price minus ``slippage_bps``.

    The quote is a SNAPSHOT (fees and bin traversal not modelled — see
    :mod:`gecko.meteora_math`); the slippage guard + the simulate Receipt are the
    protection, not the prediction. Raises :class:`ValueError` on missing bindings and
    :class:`gecko.meteora_math.MeteoraMathError` on a dead/unswappable pool.
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
        raise ValueError(f"plan_swap needs bindings {missing}")

    input_mint = str(bindings["input_mint"])
    output_mint = str(bindings["output_mint"])
    user = str(bindings["user"])
    amount_in = int(bindings["amount_in"])
    pdas = _load_meteora_pdas()

    # (1) derive the root — the address Orquestra can't derive (#4057) — and confirm
    # it holds a real pool whose mints match the intent (the state read doubles as the
    # wrong-pool guard: a bad bin_step/base_factor derives an address with no account).
    lb_pair = derive_pda(
        pdas["lb_pair"],
        {
            "token_x_mint": input_mint,
            "token_y_mint": output_mint,
            "bin_step": int(bindings["bin_step"]),
            "base_factor": int(bindings["base_factor"]),
        },
    ).address
    state: LbPairState = read_lb_pair_state(lb_pair, rpc_url=rpc_url, rpc_call=rpc_call)
    selling_x = swap_for_y(state, input_mint)  # also validates the mint is in the pool

    # (2) the token programs, from each mint's OWNER (Token vs Token-2022) — the same
    # read pump's plan_buy makes; native SOL's sentinel mint is owned by Token.
    token_x_program = read_account_owner(
        state.token_x_mint, rpc_url=rpc_url, rpc_call=rpc_call
    )
    token_y_program = read_account_owner(
        state.token_y_mint, rpc_url=rpc_url, rpc_call=rpc_call
    )
    in_program, out_program = (
        (token_x_program, token_y_program)
        if selling_x
        else (token_y_program, token_x_program)
    )

    # (3) derive the leaves + the user ATAs.
    accounts: dict[str, str] = {
        "lb_pair": lb_pair,
        "reserve_x": derive_pda(
            pdas["reserve"], {"lb_pair": lb_pair, "token_mint": state.token_x_mint}
        ).address,
        "reserve_y": derive_pda(
            pdas["reserve"], {"lb_pair": lb_pair, "token_mint": state.token_y_mint}
        ).address,
        "user_token_in": derive_pda(
            pdas["user_token"],
            {"owner": user, "token_program": in_program, "mint": input_mint},
        ).address,
        "user_token_out": derive_pda(
            pdas["user_token"],
            {"owner": user, "token_program": out_program, "mint": output_mint},
        ).address,
        "token_x_mint": state.token_x_mint,
        "token_y_mint": state.token_y_mint,
        "oracle": derive_pda(pdas["oracle"], {"lb_pair": lb_pair}).address,
        "user": user,
        "token_x_program": token_x_program,
        "token_y_program": token_y_program,
        "event_authority": derive_pda(pdas["event_authority"], {}).address,
        "program": METEORA_PROGRAM_ID,
    }

    # (4) the Class-1 remaining accounts: bin_arrays from the live bitmap walk.
    indexes = swap_bin_array_indexes(state, input_mint, _BIN_ARRAY_TAKE_COUNT)
    bin_arrays = [
        {
            "pubkey": derive_pda(
                pdas["bin_array"], {"lb_pair": lb_pair, "index": idx}
            ).address,
            "index": idx,
            "isWritable": True,
            "isSigner": False,
        }
        for idx in indexes
    ]

    # (5) the state-quoted slippage guard.
    expected_out, min_amount_out = quote_min_amount_out(
        amount_in, state, input_mint, slippage_bps
    )

    str_bindings = {"input_mint": input_mint, "output_mint": output_mint, "user": user}
    return {
        "instruction": "swap",
        "accounts": accounts,
        # bin_array_bitmap_extension and host_fee_in are OPTIONAL swap accounts,
        # honestly omitted: no host fee is charged, and the extension only matters for
        # liquidity beyond array index ±512 (the bitmap walk notes the same limit).
        "optional_accounts_omitted": ["bin_array_bitmap_extension", "host_fee_in"],
        "remaining_accounts": bin_arrays,
        "args": {"amount_in": amount_in, "min_amount_out": min_amount_out},
        "feePayer": user,
        "build_url": (
            "https://api.orquestra.dev/api/v48gsz901w84zriqe0elsl"
            "/instructions/swap/build"
        ),
        "quote": {
            "active_id": state.active_id,
            "bin_step": state.bin_step,
            "expected_out_snapshot": expected_out,
            "min_amount_out": min_amount_out,
            "slippage_bps": slippage_bps,
            "note": (
                "SNAPSHOT quote at the active-bin price — fees and bin traversal not "
                "modelled, the active bin moves; the on-chain min_amount_out guard + "
                "the simulate Receipt are the protection, not this prediction"
            ),
        },
        # The DECLARED ordered plan a real builder assembles — the "declare" half.
        # Gecko emits the plan, never a signed/broadcast tx.
        "landing_plan": _landing_plan(accounts, str_bindings, bin_arrays),
    }


def _swap_plan(
    surface: OrquestraProgramSurface, args: Mapping[str, Any]
) -> dict[str, str]:
    """Intent adapter for the MCP surface: run plan_swap and hand back the named
    accounts (the surface wraps them with the /build execute URL). The full plan —
    bin_arrays, quote, landing_plan — comes from the standalone :func:`plan_swap`."""
    plan = plan_swap(args)
    return plan["accounts"]


_SWAP = Intent(
    name="plan_swap",
    instruction="swap",
    description=(
        "Plan a Meteora DLMM swap. Give input_mint, output_mint, bin_step, base_factor, "
        "user and amount_in; Gecko derives the pool (lb_pair) and every account the "
        "swap instruction needs — including the bin_array remaining accounts the IDL "
        "never names (selected from the pool's LIVE liquidity bitmap) — and quotes "
        "min_amount_out from the pool's active-bin price. base_factor is the pool's fee "
        "tier: multiple pools share the same (mint-pair, bin_step) at different "
        "base_factors, so it is NOT inferable from the mints — you must pick the fee "
        "tier (read it off the target pool's on-chain parameters, or the Meteora pair "
        "list). Omit it and Gecko would derive a DIFFERENT, wrong pool for any pool "
        "created after the 2024-05 SDK upgrade."
    ),
    inputs=(
        "input_mint",
        "output_mint",
        "bin_step",
        "base_factor",
        "user",
        "amount_in",
    ),
    plan=_swap_plan,
)


# The code half of the config: the multi-step plan callables this program exposes,
# keyed by intent name. The config lists the intent NAMES; here we supply their
# derivation logic. (Making plans declarative — data too — is a follow-on PR.)
METEORA_INTENTS: dict[str, Intent] = {_SWAP.name: _SWAP}

# What find_start declares about plan_swap: the derive set + the source-recovered
# facts the packaged overlay does not carry (the overlay only hand-models
# user_token; lb_pair/bin_array were recovered from program SOURCE) + the
# DECLARED landing preludes. Pure data.
METEORA_STARTS: dict[str, StartSpec] = {
    "plan_swap": StartSpec(
        accounts=(
            "lb_pair",
            "reserve",
            "oracle",
            "bin_array",
            "event_authority",
            "user_token",
        ),
        recovered={
            "lb_pair": (
                "the helper-seeded ROOT the IDL drops (#4057), recovered from source "
                "(derive_lb_pair_pda2): 4-seed recipe [min(x,y), max(x,y), bin_step, "
                "base_factor(u16 LE)]. base_factor is the pool's FEE TIER — "
                "caller-supplied, not inferable from the mints; omitting it silently "
                "derives the WRONG pool for any post-2024-05 pool"
            ),
            "bin_array": (
                "remaining_accounts the IDL never names: which bin_array PDAs the "
                "swap traverses is selected from the pool's LIVE liquidity bitmap at "
                "active_id (gecko.meteora_math) — declared in the plan, filled at "
                "plan time"
            ),
        },
        preludes=(
            PreludeSpec(
                kind="compute_budget",
                program=COMPUTE_BUDGET_PROGRAM_ID,
                note=(
                    "SetComputeUnitLimit (units from the simulate Receipt) + "
                    "SetComputeUnitPrice (getRecentPrioritizationFees)"
                ),
            ),
            PreludeSpec(
                kind="create_idempotent_ata",
                program=ASSOCIATED_TOKEN_PROGRAM_ID,
                note=(
                    "createAssociatedTokenAccountIdempotent for BOTH user token "
                    "accounts (in/out legs). token_program note: each ATA derives "
                    "under the token program read from its mint's OWNER (Token vs "
                    "Token-2022), never a hardcoded SPL Token"
                ),
            ),
            PreludeSpec(
                kind="wrap_unwrap_wsol",
                program=SYSTEM_PROGRAM_ID,
                note=(
                    "CONDITIONAL: when a side's mint is native SOL, the plan adds "
                    "the wSOL wrap (transfer + SyncNative) before the swap and a "
                    "CloseAccount unwrap after it"
                ),
            ),
        ),
    )
}


def build_meteora_surface() -> OrquestraProgramSurface:
    """Build the Meteora surface from packaged config (identity + PDA recipes) +
    the local intent registry (the plan callables)."""
    from .cli import build_surface_from_config

    return build_surface_from_config("orquestra", "meteora", METEORA_INTENTS)


def main(argv: list[str] | None = None) -> int:
    """DEPRECATED alias — use ``gecko-orquestra --program meteora``.

    Kept so the 0.9.2 command keeps working; delegates to the provider CLI with
    ``--program meteora`` prepended.
    """
    from .cli import main as _cli_main

    print(
        "meteora-demo is deprecated — use: gecko-orquestra --program meteora",
        file=sys.stderr,
    )
    passthrough = list(argv) if argv is not None else sys.argv[1:]
    return _cli_main(["--program", "meteora", *passthrough])
