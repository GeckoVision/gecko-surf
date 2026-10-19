"""The unsigned bytes of one settlement: Gecko's own SOL to USDC to the cold treasury.

One transaction, signed by the SETTLEMENT wallet alone (option B, founder ruling 9 Oct),
four instructions and nothing else:

1. System ``Transfer`` of the swap input from the settlement wallet into its OWN wSOL
   account (the wrap);
2. SPL-Token ``SyncNative`` on that wSOL account;
3. Whirlpool ``swap_v2`` on the ONE configured pool, exact input, wSOL in from its own
   account, USDC out to its own account, guarded by a floor (``other_amount_threshold``);
4. SPL-Token ``TransferChecked`` to the treasury's USDC account of what the wallet will
   hold ABOVE ITS FLOAT: the floor plus whatever USDC it already held, less
   ``float_usdc_micro`` (founder ruling, 9 Oct: the float pays Orthogonal per call over
   x402). When that is nothing, the transfer is left out and the USDC stays as float.

No ``CloseAccount`` (it would send the wSOL account's lamports to an address Privy cannot
check), no account create (both token accounts are made once, by hand, before the first
settlement), no compute-budget bid (a price instruction can spend the wallet on fees).
The exact-input swap leaves the wSOL account at its rent, so nothing is left wrapped.

The input is the SOL the user's charge delivered, less one signature's fee: the payment
carries its own settlement cost, so the wallet's float does not drift. The swap leg is
built from the IDL's own ``swap_v2`` by :func:`gecko.instruction_build.make_instruction`;
its accounts come from :func:`gecko.providers.whirlpool.plan_swap` pinned to the pool.
Nothing here signs, sends or fetches a key.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..autonomous_purchase import (
    SWAP_V2_DISCRIMINATOR,
    USDC_DECIMALS,
    USDC_MINT,
    WHIRLPOOL_PROGRAM,
)
from ..landing import (
    NATIVE_SOL_MINT,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    assemble_unsigned_tx,
    wrap_sol_ixs,
)
from ..rpc import RpcCall
from ..simulate import BuiltTx
from ..spend_policy import AllowedInstruction, SpendPolicy, TokenCap, TokenCaps
from ..store_accounts import derive_ata
from .charge import BillingError
from .payment_tx import FEE_BUDGET_LAMPORTS, IdlFetch, transfer_checked_ix, usdc_ata

__all__ = [
    "SETTLEMENT_FEE_LAMPORTS",
    "SettlementBuild",
    "SwapLeg",
    "SwapPlanner",
    "build_settlement",
    "leg_from_values",
    "settlement_spend_policy",
    "swap_v2_instruction",
    "whirlpool_swap_leg",
    "wsol_ata",
]

#: One signature's base fee: what a settlement costs, taken from the SOL it settles.
SETTLEMENT_FEE_LAMPORTS = 5_000

_TOKEN_TRANSFER_CHECKED = 12
_TOKEN_SYNC_NATIVE = 17
_SYSTEM_TRANSFER = 2


@dataclass(frozen=True)
class SwapLeg:
    """One sized, exact-input ``swap_v2`` from the owner's wSOL to the owner's USDC."""

    pool: str
    vault_a: str
    vault_b: str
    amount_in_lamports: int
    min_out_usdc_micro: int
    instruction: Any  # a solders Instruction
    #: Every account the swap writes besides the owner: the gate's destination list.
    writes: frozenset[str]


#: ``(owner, lamports_in) -> SwapLeg``. Injected so the tests need no pool on chain.
SwapPlanner = Callable[[str, int], SwapLeg]


@dataclass(frozen=True)
class SettlementBuild:
    """The unsigned settlement, and the facts the gate and the verifier need about it."""

    built: BuiltTx
    owner: str
    treasury_usdc_ata: str
    lamports_in: int
    transfer_usdc_micro: int
    swap: SwapLeg
    policy: SpendPolicy


def wsol_ata(owner: str) -> str:
    """``owner``'s wrapped-SOL account (classic SPL), derived, never supplied."""
    return derive_ata(owner, NATIVE_SOL_MINT, token_program=TOKEN_PROGRAM_ID)


def settlement_spend_policy(
    build_swap: SwapLeg, *, owner: str, transfer: int, treasury_usdc: str
) -> SpendPolicy:
    """The policy for THIS settlement and nothing wider: the four instructions above,
    the owner's two token accounts, the pool's writes and the treasury as destinations,
    the wrap plus a fee as the lamport ceiling, and the transfer as the USDC ceiling."""
    lamports = build_swap.amount_in_lamports + FEE_BUDGET_LAMPORTS
    # No transfer (all of it kept as float): the treasury is not a destination either.
    forward = frozenset({treasury_usdc}) if transfer > 0 else frozenset()
    destinations = (
        frozenset({wsol_ata(owner), usdc_ata(owner)}) | forward | build_swap.writes
    )
    return SpendPolicy(
        authorized=True,
        per_transaction_cap_lamports=lamports,
        hourly_cap_lamports=lamports,
        daily_cap_lamports=lamports,
        max_transactions_per_day=1,
        allowed_instructions=frozenset(
            {
                AllowedInstruction(
                    SYSTEM_PROGRAM_ID, struct.pack("<I", _SYSTEM_TRANSFER)
                ),
                AllowedInstruction(TOKEN_PROGRAM_ID, bytes([_TOKEN_SYNC_NATIVE])),
                AllowedInstruction(TOKEN_PROGRAM_ID, bytes([_TOKEN_TRANSFER_CHECKED])),
                AllowedInstruction(WHIRLPOOL_PROGRAM, SWAP_V2_DISCRIMINATOR),
            }
        ),
        allowed_destinations=destinations,
        token_caps=TokenCaps.of(
            [
                TokenCap(
                    mint=USDC_MINT,
                    decimals=USDC_DECIMALS,
                    per_transaction_raw=transfer,
                    hourly_raw=transfer,
                    daily_raw=transfer,
                ),
                TokenCap(
                    mint=NATIVE_SOL_MINT,
                    decimals=9,
                    per_transaction_raw=build_swap.amount_in_lamports,
                    hourly_raw=build_swap.amount_in_lamports,
                    daily_raw=build_swap.amount_in_lamports,
                ),
            ]
        ),
    )


def build_settlement(
    *,
    owner: str,
    fee_wallet: str,
    lamports_in: int,
    held_usdc_micro: int,
    swap_planner: SwapPlanner,
    max_usdc_micro: int,
    float_usdc_micro: int = 0,
) -> SettlementBuild:
    """The unsigned settlement of ``lamports_in`` from ``owner`` (the settlement wallet).

    The USDC forwarded is what the wallet is sure to hold above ``float_usdc_micro``
    after the swap: the floor plus ``held_usdc_micro`` (what it held before) less the
    float, never negative, capped at ``max_usdc_micro``. It is reckoned on the FLOOR, so
    the float is kept even when the swap pays its worst; what the swap pays above its
    floor stays in the wallet and goes with the next settlement.
    """
    if lamports_in <= 0:
        raise BillingError("there is no SOL to settle")
    if owner == fee_wallet:
        raise BillingError("the settlement wallet cannot be the treasury")
    if held_usdc_micro < 0:
        raise BillingError("a token balance is never negative")
    if float_usdc_micro < 0:
        raise BillingError("the settlement float is never negative")
    leg = swap_planner(owner, lamports_in)
    if leg.amount_in_lamports != lamports_in:
        raise BillingError("the swap was sized for a different amount of SOL")
    if leg.min_out_usdc_micro <= 0:
        raise BillingError(
            "the swap has no output floor; refusing to sell at any price"
        )
    if leg.min_out_usdc_micro > max_usdc_micro:
        raise BillingError(
            "the swap's floor alone is above the settlement ceiling "
            "(GECKO_SETTLEMENT_MAX_USDC_MICRO)"
        )
    above_float = held_usdc_micro + leg.min_out_usdc_micro - float_usdc_micro
    transfer = min(max(above_float, 0), max_usdc_micro)
    treasury = usdc_ata(fee_wallet)
    wsol = wsol_ata(owner)
    instructions = [*wrap_sol_ixs(owner, wsol, lamports_in), leg.instruction]
    if transfer > 0:
        instructions.append(
            transfer_checked_ix(
                source=usdc_ata(owner),
                mint=USDC_MINT,
                destination=treasury,
                owner=owner,
                amount=transfer,
                decimals=USDC_DECIMALS,
            )
        )
    return SettlementBuild(
        built=assemble_unsigned_tx(instructions, owner),
        owner=owner,
        treasury_usdc_ata=treasury,
        lamports_in=lamports_in,
        transfer_usdc_micro=transfer,
        swap=leg,
        policy=settlement_spend_policy(
            leg, owner=owner, transfer=transfer, treasury_usdc=treasury
        ),
    )


def _idl_instruction(idl: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    for instruction in idl.get("instructions") or ():
        if isinstance(instruction, Mapping) and instruction.get("name") == name:
            return instruction
    raise BillingError(f"the Whirlpool IDL declares no `{name}` instruction")


def swap_v2_instruction(
    idl: Mapping[str, Any], values: Mapping[str, Any], *, payer: str
) -> Any:
    """``swap_v2`` from ``plan_swap``'s values, ``payer`` as ``token_authority`` and the
    pool's oracle PDA (``["oracle", whirlpool]``). The memo program is the IDL's own
    pinned address; nothing is guessed."""
    from solders.pubkey import Pubkey

    from ..instruction_build import InstructionEncodeError, make_instruction

    pool = str(values["whirlpool"])
    oracle, _bump = Pubkey.find_program_address(
        [b"oracle", bytes(Pubkey.from_string(pool))],
        Pubkey.from_string(WHIRLPOOL_PROGRAM),
    )
    accounts = {
        name: str(value)
        for name, value in values.items()
        if isinstance(value, str) and name.startswith(("token_", "tick_array_"))
    }
    accounts.update(
        {"whirlpool": pool, "token_authority": payer, "oracle": str(oracle)}
    )
    args = {
        "amount": int(values["amount"]),
        "other_amount_threshold": int(values["other_amount_threshold"]),
        "sqrt_price_limit": int(values["sqrt_price_limit"]),
        "amount_specified_is_input": bool(values["amount_specified_is_input"]),
        "a_to_b": bool(values["a_to_b"]),
        "remaining_accounts_info": None,
    }
    try:
        return make_instruction(
            _idl_instruction(idl, "swap_v2"),
            program_id=WHIRLPOOL_PROGRAM,
            accounts=accounts,
            args=args,
        )
    except InstructionEncodeError as exc:
        raise BillingError(f"swap_v2 could not be built: {exc}") from None


def leg_from_values(
    idl: Mapping[str, Any], values: Mapping[str, Any], *, owner: str, pool: str
) -> SwapLeg:
    """A :class:`SwapLeg` from a plan's ``values``: built, and its writes listed."""
    instruction = swap_v2_instruction(idl, values, payer=owner)
    writes = frozenset(
        str(meta.pubkey)
        for meta in instruction.accounts
        if meta.is_writable and str(meta.pubkey) != owner
    )
    return SwapLeg(
        pool=pool,
        vault_a=str(values["token_vault_a"]),
        vault_b=str(values["token_vault_b"]),
        amount_in_lamports=int(values["amount"]),
        min_out_usdc_micro=int(values["other_amount_threshold"]),
        instruction=instruction,
        writes=writes,
    )


def whirlpool_swap_leg(
    *,
    rpc_url: str,
    rpc_call: RpcCall,
    idl_fetch: IdlFetch,
    slippage_bps: int,
    pool: str,
) -> SwapPlanner:
    """The live :data:`SwapPlanner`: an exact-input SOL->USDC plan, PINNED to ``pool``.

    ``plan_swap`` re-derives the pinned pool from its own configuration and refuses one
    that cannot; the floor is its integer quote less ``slippage_bps``.
    """
    from ..providers.whirlpool import WhirlpoolPlanError, plan_swap

    if not pool:
        raise BillingError("settling needs the pinned pool (GECKO_SETTLEMENT_POOL)")

    def plan(owner: str, lamports_in: int) -> SwapLeg:
        idl = idl_fetch(WHIRLPOOL_PROGRAM)
        try:
            sized = plan_swap(
                {
                    "input_mint": NATIVE_SOL_MINT,
                    "output_mint": USDC_MINT,
                    "user": owner,
                    "slippage_bps": slippage_bps,
                    "amount_in": lamports_in,
                    "pool": pool,
                },
                rpc_url=rpc_url,
                rpc_call=rpc_call,
                idl_fetch=lambda _p: idl,
            )
        except WhirlpoolPlanError as exc:
            raise BillingError(
                f"the settlement swap could not be planned: {exc}"
            ) from None
        if str(sized.get("pool")) != pool:
            raise BillingError(
                "the swap planner chose a pool other than the pinned one"
            )
        return leg_from_values(idl, sized["values"], owner=owner, pool=pool)

    return plan
