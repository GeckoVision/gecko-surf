"""Orca Whirlpool — the `plan_swap` intent, so a HOSTED agent can convert a token pair.

WHY THIS MODULE EXISTS. The whole swap path already ran on mainnet — four Gecko-built
`swap_v2` transactions landed with exact CU predictions — but every one was assembled by
``scripts/prepare_whirlpool_swap.py`` reading a keypair on this machine. A hosted agent
(Claude web + a signer connector) cannot run scripts. What it CAN call is
``prepare_instruction`` — the same tool the script itself uses — and the only thing it
was missing is the PLANNER: the code that turns "convert this into that for this user"
into the exact values `swap_v2` needs. That is this module, and it is the same porting
move `meteora.plan_swap` made for the DLMM.

Everything load-bearing was already in the package this week: venue selection that makes
a pool re-derive its own address (`whirlpool_venue.find_venues`), tick-array selection
with the ASCII-decimal seed trap handled (`whirlpool_venue.tick_arrays`), and sizing
under the ONE slippage bound (`whirlpool_math`, `SWAP_SLIPPAGE_BPS`). This module only
assembles.

THE TRAPS IT CARRIES (from the packaged config's measured notes, none of them in the
IDL): `swap_v2`, never `swap` — v1 cannot handle a Token-2022 mint, and this pair is
mixed (USDG under Token-2022, USDC under classic SPL). Each user ATA derives under the
token program read from ITS OWN mint's owner — the wrong one yields a valid, empty,
never-initialized account. `swap_v2` creates NO token accounts: both ATAs must already
exist, including the receiving one. And the output floor is never defaulted to zero —
"accept any output" was the state of five real mainnet swaps before the rule existed.
"""

from __future__ import annotations

from ..tools import tool_annotations

from typing import Any, Mapping

from ..find_start import StartSpec
from ..pda_resolve import read_account_owner
from ..provider_config import load_packaged_provider
from ..rpc import RpcCall, default_rpc_call
from ..store_accounts import derive_ata
from ..whirlpool_math import quote_min_amount_out
from ..whirlpool_venue import (
    WHIRLPOOL_PROGRAM,
    find_venues,
    tick_arrays,
    whirlpool_layout,
)
from .orquestra import Intent, OrquestraProgramSurface

__all__ = [
    "PLAN_SWAP_TOOL",
    "WHIRLPOOL_INTENTS",
    "WHIRLPOOL_STARTS",
    "plan_swap",
    "plan_swap_result",
]

#: The sizing/build bound. ONE number on purpose — sizing at 50 while building at 100 is
#: the mainnet bug `pay_route.SWAP_SLIPPAGE_BPS` documents, so this defaults to the same
#: constant rather than declaring a second one.
from ..pay_route import SWAP_SLIPPAGE_BPS  # noqa: E402  (single source of truth)


class WhirlpoolPlanError(Exception):
    """The plan cannot be assembled — a refusal with the reason, never a guess."""


def _mint_owner(rpc_url: str, mint: str, rpc_call: RpcCall) -> str:
    return read_account_owner(mint, rpc_url=rpc_url, rpc_call=rpc_call)


def plan_swap(
    args: Mapping[str, Any],
    *,
    rpc_url: str,
    rpc_call: RpcCall | None = None,
    idl_fetch: Any = None,
) -> dict[str, Any]:
    """Plan one `swap_v2`: derive the pool and every account, size the floor, refuse gaps.

    ``args``: ``input_mint``, ``output_mint``, ``user``, ``amount_in`` (base units of the
    input mint), optional ``slippage_bps`` (defaults to the ONE shared bound) and
    optional ``pool`` (else derived — and a pool that cannot reproduce its own address
    from its own configuration is dropped, not ranked lower).

    Returns the full plan: the exact ``values`` for ``prepare_instruction`` /
    Orquestra's ``/build``, the quote with its honest limits, and ``missing_atas`` —
    `swap_v2` creates no token accounts, so a missing one is named here with the command
    that fixes it, rather than discovered as a 3012 in simulation.
    """
    call: RpcCall = rpc_call or default_rpc_call
    input_mint = str(args["input_mint"])
    output_mint = str(args["output_mint"])
    user = str(args["user"])
    amount_in = int(args["amount_in"])
    slippage_bps = int(args.get("slippage_bps", SWAP_SLIPPAGE_BPS))
    if amount_in <= 0:
        raise WhirlpoolPlanError(f"amount_in must be positive, got {amount_in}")
    if not 0 <= slippage_bps < 10_000:
        raise WhirlpoolPlanError(
            f"slippage_bps {slippage_bps} is not in [0, 10000) — 10000 or more accepts "
            "any price, which is no bound at all"
        )

    if idl_fetch is None:
        from .catalog_surface import orquestra_seams

        idl_fetch, _build = orquestra_seams()
    idl = idl_fetch(WHIRLPOOL_PROGRAM)
    layout = whirlpool_layout(idl)
    _, apis = load_packaged_provider("orquestra")
    program = apis["whirlpool"].program
    if program is None:  # pragma: no cover - the packaged config always carries it
        raise WhirlpoolPlanError("the packaged whirlpool config declares no program")
    pool_recipe = dict(program.pdas)["whirlpool"]
    tick_recipe = dict(program.pdas)["tick_array"]

    venues = find_venues(
        rpc_url,
        input_mint,
        output_mint,
        layout=layout,
        recipe=pool_recipe,
        rpc_call=call,
    )
    wanted = str(args["pool"]) if args.get("pool") else None
    venue = next((v for v in venues if wanted is None or v.pool == wanted), None)
    if venue is None:
        raise WhirlpoolPlanError(
            "no pool for this pair survived re-derivation"
            + (f" (asked for {wanted})" if wanted else "")
            + " — the memcmp proposes and the seed recipe disposes, and nothing disposed"
        )

    # Re-read the pool ACCOUNT for the swap-only fields (tick_current, vaults). The
    # venue proved the address; this read supplies what a swap needs beyond ranking.
    import base64 as _b64

    value = (
        call(rpc_url, "getAccountInfo", [venue.pool, {"encoding": "base64"}]).get(
            "result"
        )
        or {}
    ).get("value")
    if not value:
        raise WhirlpoolPlanError(f"pool {venue.pool} vanished between ranking and read")
    from ..whirlpool_venue import decode_whirlpool

    account = decode_whirlpool(_b64.b64decode(value["data"][0]), layout)
    if account.tick_current_index is None or account.token_vault_a is None:
        raise WhirlpoolPlanError(
            "this IDL does not declare tick_current_index/token_vault fields — the plan "
            "cannot be assembled from it, and guessing offsets is how a well-formed "
            "transaction addresses the wrong region of the curve"
        )

    a_to_b = venue.direction == "a_to_b"
    mint_a, mint_b = account.token_mint_a, account.token_mint_b
    program_a = _mint_owner(rpc_url, mint_a, call)
    program_b = _mint_owner(rpc_url, mint_b, call)

    live = account.sqrt_price
    limit = (
        live * (10_000 - slippage_bps) // 10_000
        if a_to_b
        else live * (10_000 + slippage_bps) // 10_000
    )
    spot, min_out = quote_min_amount_out(
        amount_in, live, account.fee_rate, a_to_b=a_to_b, slippage_bps=slippage_bps
    )
    if min_out <= 0:
        raise WhirlpoolPlanError(
            "the derived output floor is zero — 'accept any output', which is exactly "
            "what five real mainnet swaps ran with before this refusal existed"
        )

    ticks = tick_arrays(
        venue.pool,
        tick_current=account.tick_current_index,
        tick_spacing=account.tick_spacing,
        upward=not a_to_b,
        recipe=tick_recipe,
    )

    ata_a = derive_ata(user, mint_a, token_program=program_a)
    ata_b = derive_ata(user, mint_b, token_program=program_b)
    missing: list[dict[str, str]] = []
    for label, ata, mint, token_program in (
        ("a", ata_a, mint_a, program_a),
        ("b", ata_b, mint_b, program_b),
    ):
        exists = (
            call(rpc_url, "getAccountInfo", [ata, {"encoding": "base64"}]).get("result")
            or {}
        ).get("value") is not None
        if not exists:
            missing.append(
                {
                    "side": label,
                    "ata": ata,
                    "mint": mint,
                    "fix": (
                        f"spl-token create-account {mint} --program-id "
                        f"{token_program} --owner {user}"
                    ),
                }
            )

    values: dict[str, Any] = {
        "whirlpool": venue.pool,
        "token_mint_a": mint_a,
        "token_mint_b": mint_b,
        "token_program_a": program_a,
        "token_program_b": program_b,
        "token_owner_account_a": ata_a,
        "token_owner_account_b": ata_b,
        "token_vault_a": account.token_vault_a,
        "token_vault_b": account.token_vault_b,
        "tick_array_0": ticks[0],
        "tick_array_1": ticks[1],
        "tick_array_2": ticks[2],
        "amount": amount_in,
        "other_amount_threshold": min_out,
        "sqrt_price_limit": limit,
        "amount_specified_is_input": True,
        "a_to_b": a_to_b,
        "remaining_accounts_info": None,
    }
    return {
        "instruction": "swap_v2",
        "program_id": WHIRLPOOL_PROGRAM,
        "pool": venue.pool,
        "direction": venue.direction,
        "values": values,
        "feePayer": user,
        "next_tool": "prepare_instruction",
        "missing_atas": missing,
        "quote": {
            "amount_in": amount_in,
            "expected_out_spot": spot,
            "min_amount_out": min_out,
            "slippage_bps": slippage_bps,
            "sqrt_price": str(live),
            "sqrt_price_limit": str(limit),
            "note": (
                "spot quote — price impact is not modelled, so the floor is optimistic "
                "and fails CLOSED: the program reverts rather than filling below it. "
                "min_amount_out and sqrt_price_limit are the protection, not this number."
            ),
        },
    }


def _swap_plan(
    surface: OrquestraProgramSurface, args: Mapping[str, Any]
) -> dict[str, str]:
    """Intent adapter: run the full planner and hand back the named values.

    The surface's contract wants a flat name→value map for `/build`; the standalone
    :func:`plan_swap` carries the quote and the honest limits for callers that want them.
    """
    from ..rpc import LOCAL_RPC
    import os

    rpc_url = os.environ.get("GECKO_MAINNET_RPC", LOCAL_RPC)
    plan = plan_swap(args, rpc_url=rpc_url)
    return {k: str(v) for k, v in plan["values"].items()}


_PLAN_SWAP = Intent(
    name="plan_swap",
    instruction="swap_v2",
    # PAIR-AGNOSTIC ON PURPOSE. The first draft named the flagship pair's token symbols
    # and immediately stole every query mentioning them from the sibling venues — the
    # exact overreach the meteora card's history warns about: a card that serves every
    # pair must not name any pair's symbols. Concrete mints belong in the packaged
    # config's NOTES (operator-facing), never in ranking vocabulary.
    description=(
        "Swap, exchange or convert one token for another on an Orca Whirlpool "
        "concentrated-liquidity pool. Give input_mint, output_mint, user and "
        "amount_in; Gecko derives the pool (and DROPS any candidate that cannot "
        "reproduce its own address from its own configuration), reads each mint's "
        "token program from the mint itself, selects the three tick arrays in the "
        "direction of travel, and sizes min_amount_out and sqrt_price_limit under one "
        "shared slippage bound. Uses swap_v2, never swap — v1 cannot carry a "
        "Token-2022 mint. swap_v2 creates NO token accounts: missing ATAs are named "
        "in the plan with the command that fixes them, not discovered as a 3012."
    ),
    inputs=("input_mint", "output_mint", "user", "amount_in"),
    plan=_swap_plan,
)

WHIRLPOOL_INTENTS: dict[str, Intent] = {_PLAN_SWAP.name: _PLAN_SWAP}

WHIRLPOOL_STARTS: dict[str, StartSpec] = {
    "plan_swap": StartSpec(
        accounts=("whirlpool", "tick_array", "oracle", "token_badge"),
        recovered={
            "whirlpool": (
                "PDA [whirlpools_config, mint_a, mint_b, tick_spacing(u16 LE)] — "
                "tick_spacing selects the pool among fee tiers and is NOT derivable "
                "from the mints; every candidate must re-derive its own address or be "
                "dropped (a wrong pool is real, funded, and accepts the money)"
            ),
            "tick_array": (
                "seeded with the ASCII DECIMAL string of the start index, not LE "
                "bytes — the IDL declares the arg i32 and a type does not determine a "
                "seed encoding; span is 88 * tick_spacing and the start index floors "
                "toward negative infinity"
            ),
            "oracle": (
                "['oracle', whirlpool] — may not exist on chain and must still be "
                "passed WRITABLE; whether it needs initializing depends on the fee "
                "tier being adaptive, a live read, never an IDL fact"
            ),
        },
    )
}


# --- the MCP tool ---------------------------------------------------------------------

PLAN_SWAP_TOOL: dict[str, Any] = {
    "name": "plan_swap",
    "annotations": tool_annotations(
        read_only=True, open_world=True, title="Plan a Whirlpool swap"
    ),
    "description": (
        "Plan a token conversion on Orca Whirlpool and hand back the exact values "
        "`prepare_instruction` needs to build it — Gecko's CHECKED venue, from the "
        "wallet you name, without signing anything or starting any clock. The pool is "
        "derived, never looked up: every candidate must reproduce its own address from "
        "its own on-chain configuration, and one that cannot is DROPPED — the "
        "second-best answer is a real, funded pool that would take the money. Each "
        "mint's token program is read from the mint itself, the three tick arrays are "
        "selected in the direction of travel, and min_amount_out + sqrt_price_limit "
        "are sized under ONE shared slippage bound, so the floor you are guaranteed is "
        "the floor the swap is built with. Uses swap_v2 — v1 cannot carry a Token-2022 "
        "mint. `missing_atas` names any token account the wallet lacks WITH the command "
        "that creates it: swap_v2 creates no accounts, and finding that out as a 3012 "
        "mid-swap is the expensive way. "
        "WHAT THIS DELIBERATELY DOES NOT DO: consult the peg oracle. This tool plans "
        "the conversion a caller explicitly asked for; `plan_payment` is the tool that "
        "DECIDES whether converting is wise, and its peg gate can refuse. Calling this "
        "directly is the operator saying 'I want this swap' — the quote's own floor and "
        "price-limit are the protection that remains. "
        "NEXT STEP: pass `values` to prepare_instruction (program_id, "
        "instruction=swap_v2, payer=your wallet), sign the returned bytes, verify the "
        "binding, submit. Read-only; nothing here holds a key."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "input_mint": {"type": "string", "description": "the mint being SOLD"},
            "output_mint": {"type": "string", "description": "the mint being BOUGHT"},
            "user": {
                "type": "string",
                "description": "the wallet that signs and pays — its ATAs are derived and checked",
            },
            "amount_in": {
                "type": "integer",
                "minimum": 1,
                "description": "base units of input_mint to sell",
            },
            "pool": {
                "type": "string",
                "description": (
                    "optional: pin the pool a prior plan_payment route named "
                    "(route.quote.pool), so the venue that was checked is the venue "
                    "that executes. Still re-derived, and DROPPED if it cannot "
                    "reproduce its own address. Omit to derive fresh."
                ),
            },
            "slippage_bps": {
                "type": "integer",
                "description": "optional; defaults to the ONE shared bound the swap is built with",
            },
            "network": {
                "type": "string",
                "description": "mainnet (default) or a fork you name with rpc_url",
            },
            "rpc_url": {
                "type": "string",
                "description": "your own node; requires `network` so the two cannot disagree",
            },
        },
        "required": ["input_mint", "output_mint", "user", "amount_in"],
    },
}

_B58_CHARS = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _is_pubkey(value: Any) -> bool:
    return (
        isinstance(value, str) and 32 <= len(value) <= 44 and set(value) <= _B58_CHARS
    )


def plan_swap_result(
    arguments: Any,
    *,
    rpc_call: RpcCall | None = None,
    idl_fetch: Any = None,
    url_guard: Any = None,
) -> dict[str, Any]:
    """The surface-facing entry: validate, resolve the RPC, plan. Never raises.

    Same rules as every sibling on this unauthenticated door: the NETWORK is asserted by
    the caller and never inferred from a URL, a caller-supplied ``rpc_url`` goes through
    the SSRF guard (``url_guard`` is the injected rehearsal seam, exactly as on
    ``plan_payment``), and a transport failure comes back redacted to its class.
    """
    from ..networks import network_for_browse
    from ..prepare_purchase import SIGNERS_KNOWN_TO_WORK, _resolve_rpc_url

    args = arguments or {}
    for name in ("input_mint", "output_mint", "user"):
        if not _is_pubkey(args.get(name)):
            return {
                "error": f"`{name}` must be a base58 account address — got something else."
            }
    raw_amount = args.get("amount_in")
    if raw_amount is None:
        return {"error": "`amount_in` is required — base units of the input mint."}
    try:
        amount_in = int(raw_amount)
    except (TypeError, ValueError):
        return {"error": "`amount_in` must be an integer count of base units."}
    if amount_in <= 0:
        return {"error": f"`amount_in` must be positive, got {amount_in}."}

    network, net_error = network_for_browse(args)
    if net_error or network is None:
        return {"error": net_error or "no network"}
    rpc_url, refusal = _resolve_rpc_url(args.get("rpc_url"), network, url_guard)
    if refusal or rpc_url is None:
        return {"error": refusal or "no RPC url"}

    plan_args: dict[str, Any] = {
        "input_mint": args["input_mint"],
        "output_mint": args["output_mint"],
        "user": args["user"],
        "amount_in": amount_in,
    }
    if args.get("slippage_bps") is not None:
        plan_args["slippage_bps"] = args["slippage_bps"]
    if args.get("pool"):
        plan_args["pool"] = args["pool"]
    try:
        plan = plan_swap(
            plan_args, rpc_url=rpc_url, rpc_call=rpc_call, idl_fetch=idl_fetch
        )
    except WhirlpoolPlanError as exc:
        # A refusal with its reason — the plan could not be assembled honestly.
        return {"error": str(exc), "refused": True}
    except Exception as exc:  # noqa: BLE001 - redacted to a class at the transport edge
        return {"error": f"{type(exc).__name__}: {exc}"}
    plan["network"] = network
    # The execution breadcrumb. The first Claude web session had the plan and still
    # routed execution through the wallet's own aggregator, because at the moment of
    # "how do I run this" the only path carrying instructions was that one.
    # `prepare_purchase` embeds its signer sequence and the web agent followed it
    # exactly; a swap deserves the same rail.
    plan["next_steps"] = [
        {
            "step": "wallet_and_funding",
            "note": (
                "no wallet credential in list_credentials? PayBox creates the wallet "
                "at enrolment — the user signs in to the connector (a passkey step "
                "only they can do); an agent cannot create it and never asks the user "
                "to paste key material. FUND before building: the user needs amount_in "
                "of the input mint plus a little SOL for the fee (PayBox's "
                "get_buy_link funds an empty wallet), and check the wallet's "
                "approval_mode BEFORE preparing — always_approve waits for a "
                "passkey INSIDE the ~60s window."
            ),
        },
        {
            "step": "build",
            "tool": "prepare_instruction",
            "arguments": {
                "program_id": plan["program_id"],
                "instruction": "swap_v2",
                "payer": str(args["user"]),
                "values": "<the `values` object above, verbatim>",
            },
            "note": "returns the UNSIGNED transaction and its binding",
        },
        {
            "step": "sign",
            "note": (
                "hand the unsigned base64 to your wallet connector — PayBox: "
                "request_wallet_sign {op: 'solanaTransaction', address, "
                "transactionBase64}, then poll get_request for the signed artifact. "
                "Load the signer's tools BEFORE calling prepare_instruction: the "
                "blockhash budget is ~60s and cold tool-loading is what spends it."
            ),
            # ONE signer directory for every money path — the purchase refusal's
            # entries verbatim, so the two rails cannot drift apart.
            "signers": [dict(signer) for signer in SIGNERS_KNOWN_TO_WORK],
        },
        {
            "step": "verify",
            "tool": "verify_signed_transaction",
            "note": "prove the signed bytes are the built bytes BEFORE submitting",
        },
        {
            "step": "submit",
            "note": "sendTransaction with preflightCommitment: confirmed",
        },
    ]
    return plan
