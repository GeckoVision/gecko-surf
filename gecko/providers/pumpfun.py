"""Pump.fun — the second Orquestra program instance (buy against a bonding curve).

Where Meteora's star is deriving the helper-seeded root (``lb_pair``), Pump's is
assembling a WHOLE instruction's account set first-call-correct: the 16 accounts a
``buy`` needs, most of which an IDL/llms.txt cannot give you — the dotted-path
``creator_vault`` (reads ``bonding_curve.creator``), the token program (read from the
mint's owner, Token vs Token-2022), the two ATAs, the fee-program PDAs. Gecko derives
and resolves every one it honestly can, then hands back the Orquestra ``/build`` payload.

The honest gap: ``fee_recipient``. The on-chain ``Global`` account carries BOTH a single
``fee_recipient`` pubkey field AND a ``fee_recipients: [pubkey; 7]`` array, populated on
mainnet with *different* valid recipients. Which one ``buy`` validates against is not
determinable from the surface, so ``plan_buy`` flags ``fee_recipient`` as unresolved
(``UNRESOLVED``) rather than guess an offset — the caller / Orquestra ``/build`` supplies
it. Honesty over a fabricated account (the whole differentiator over "read the IDL and hope").

Run it (once its slug is served):
    claude mcp add orquestra-pumpfun -- \
        uvx --from "gecko-surf[serve,solana]" gecko-orquestra --program pumpfun --stdio
"""

from __future__ import annotations

from typing import Any, Mapping

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
    "PUMPFUN_INTENTS",
    "plan_buy",
    "build_pumpfun_surface",
]

# Display constants. The authoritative program id + PDA recipes live in the packaged
# config (gecko/providers/configs/orquestra/pumpfun.json) — this is data, not code.
PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
FEE_PROGRAM_ID = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
BUILD_URL = "https://api.orquestra.dev/api/6i6q26bmm46b89xlxo1kv/instructions/buy/build"

# Why fee_recipient is a flagged gap, not an auto-resolved account (see module docstring).
_FEE_RECIPIENT_NOTE = (
    "Global carries BOTH a single `fee_recipient` pubkey field (data offset 41) AND a "
    "`fee_recipients: [pubkey; 7]` array, populated on mainnet with different valid "
    "recipients; which one `buy` validates against is not determinable from the surface. "
    "Gecko will not guess — supply the authoritative fee_recipient to /build (a valid "
    "current pump fee recipient, e.g. global.fee_recipient @ offset 41)."
)


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

    ``fee_recipient`` is returned under ``unresolved`` — an honest gap, not a guess
    (see the module docstring). The ``accounts`` dict therefore carries the 15 accounts
    Gecko can supply first-call-correct; the caller/``/build`` adds ``fee_recipient``.

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
    }

    return {
        "instruction": "buy",
        "accounts": accounts,
        "unresolved": {"fee_recipient": _FEE_RECIPIENT_NOTE},
        # A real buy reverts with AnchorError 3012 (associated_user AccountNotInitialized)
        # if the buyer doesn't already hold the token's ATA — the tx must be preceded by a
        # create-associated-token-account-idempotent instruction (or the buyer must own it).
        # Gecko flags this honestly rather than emit a plan that lands in a revert.
        "preconditions": {
            "associated_user": (
                "must be an initialized ATA for (user, mint); if the buyer doesn't hold it, "
                "prepend a createAssociatedTokenAccountIdempotent instruction before `buy`"
            )
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
        "owner, derives every PDA/ATA a `buy` needs — including the dotted-path "
        "creator_vault (bonding_curve.creator) an IDL drops — and points at Orquestra's "
        "buy builder. One honest gap: fee_recipient (Global has both a single field and a "
        "7-slot array of recipients) is left for you/`/build` to supply — Gecko won't guess."
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
