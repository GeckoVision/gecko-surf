"""The unsigned bytes of one run's payment, and the policy that may sign exactly them.

Two shapes, both signed by the user's own wallet alone, each ONE instruction:

* ``USDC``: one ``TransferChecked`` of the quote from the payer's USDC account to the cold
  treasury's (``GECKO_FEE_WALLET``).
* ``SOL``: one System ``Transfer`` to Gecko's SETTLEMENT wallet of the quote converted at
  the pinned pool's price plus a stated margin (:class:`SolQuote`). No wrap, no swap, no
  account create in the user's transaction (option B, founder ruling 9 Oct): the user's
  charge is final when that SOL lands, and Gecko swaps its own revenue afterwards
  (:mod:`gecko.anyquery.settlement`), carrying the price risk.

The SOL price is read once, when the payment is built right after the user confirms the
quote, from the ONE pool configured as ``GECKO_SETTLEMENT_POOL``: the same pool the
settlement swaps on, so the price the user paid at and the price Gecko sells at come from
one place. Nothing here signs, sends or fetches a key.
"""

from __future__ import annotations

import base64
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..autonomous_purchase import USDC_DECIMALS, USDC_MINT, WHIRLPOOL_PROGRAM
from ..landing import NATIVE_SOL_MINT, SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID
from ..landing import assemble_unsigned_tx
from ..rpc import RpcCall
from ..simulate import BuiltTx
from ..spend_policy import AllowedInstruction, SpendPolicy, TokenCap, TokenCaps
from ..store_accounts import derive_ata
from .charge import BillingError
from .types import PayWith

__all__ = [
    "FEE_BUDGET_LAMPORTS",
    "IdlFetch",
    "PaymentBuild",
    "SolPricer",
    "SolQuote",
    "build_payment",
    "payment_policy",
    "system_transfer_ix",
    "transfer_checked_ix",
    "usdc_ata",
    "whirlpool_sol_pricer",
]

#: Base fee plus any priority bid. A ceiling for the gate, not a price.
FEE_BUDGET_LAMPORTS = 5_000_000

_TOKEN_TRANSFER_CHECKED = 12
_SYSTEM_TRANSFER = 2

IdlFetch = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True)
class SolQuote:
    """A USDC quote in SOL: what the user sends, and the price it was converted at."""

    lamports: int
    #: micro-USDC per SOL at the pool's spot price, before fee and margin.
    rate_micro: int
    pool: str
    margin_bps: int


#: ``usdc_micro -> SolQuote``. Injected so the tests need no pool on chain.
SolPricer = Callable[[int], SolQuote]


@dataclass(frozen=True)
class PaymentBuild:
    """The unsigned payment, and the facts the gate and the verifier need about it."""

    built: BuiltTx
    pay_with: PayWith
    payer: str
    #: The treasury's USDC account. A USDC payment lands here; a SOL payment does not.
    payee_usdc_ata: str
    amount_usdc_micro: int
    policy: SpendPolicy
    #: SOL only: where the lamports go (the settlement wallet), how many, at what price.
    settlement_wallet: str | None = None
    sol: SolQuote | None = None


def usdc_ata(owner: str) -> str:
    """``owner``'s USDC account (classic SPL), derived, never supplied."""
    return derive_ata(owner, USDC_MINT, token_program=TOKEN_PROGRAM_ID)


def transfer_checked_ix(
    *, source: str, mint: str, destination: str, owner: str, amount: int, decimals: int
) -> Any:
    """SPL-Token ``TransferChecked``: the mint and decimals ride in the instruction, so
    a wrong mint or a wrong scale reverts instead of moving a different token."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    if amount <= 0:
        raise BillingError("a payment transfer must move a positive amount")
    data = bytes([_TOKEN_TRANSFER_CHECKED]) + struct.pack("<QB", amount, decimals)
    return Instruction(
        Pubkey.from_string(TOKEN_PROGRAM_ID),
        data,
        [
            AccountMeta(Pubkey.from_string(source), False, True),
            AccountMeta(Pubkey.from_string(mint), False, False),
            AccountMeta(Pubkey.from_string(destination), False, True),
            AccountMeta(Pubkey.from_string(owner), True, False),
        ],
    )


def system_transfer_ix(*, source: str, destination: str, lamports: int) -> Any:
    """System ``Transfer``: ``lamports`` from ``source`` (the signer) to ``destination``."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    if lamports <= 0:
        raise BillingError("a SOL transfer must move a positive amount")
    return Instruction(
        Pubkey.from_string(SYSTEM_PROGRAM_ID),
        struct.pack("<IQ", _SYSTEM_TRANSFER, lamports),
        [
            AccountMeta(Pubkey.from_string(source), True, True),
            AccountMeta(Pubkey.from_string(destination), False, True),
        ],
    )


def payment_policy(
    *,
    amount_usdc_micro: int,
    destinations: frozenset[str],
    lamports: int | None = None,
) -> SpendPolicy:
    """The policy for THIS payment and nothing wider.

    Authored here because the user's confirmation of the quote is the decision it
    encodes. USDC: one ``TransferChecked`` of at most the confirmed amount, to the
    treasury's account. SOL (``lamports`` set): one System ``Transfer`` of at most
    ``lamports`` to the settlement wallet, and no token may move at all.
    """
    if lamports is None:
        return SpendPolicy(
            authorized=True,
            per_transaction_cap_lamports=FEE_BUDGET_LAMPORTS,
            hourly_cap_lamports=FEE_BUDGET_LAMPORTS,
            daily_cap_lamports=FEE_BUDGET_LAMPORTS,
            max_transactions_per_day=1,
            allowed_instructions=frozenset(
                {AllowedInstruction(TOKEN_PROGRAM_ID, bytes([_TOKEN_TRANSFER_CHECKED]))}
            ),
            allowed_destinations=destinations,
            token_caps=TokenCaps.of(
                [
                    TokenCap(
                        mint=USDC_MINT,
                        decimals=USDC_DECIMALS,
                        per_transaction_raw=amount_usdc_micro,
                        hourly_raw=amount_usdc_micro,
                        daily_raw=amount_usdc_micro,
                    )
                ]
            ),
        )
    budget = lamports + FEE_BUDGET_LAMPORTS
    return SpendPolicy(
        authorized=True,
        per_transaction_cap_lamports=budget,
        hourly_cap_lamports=budget,
        daily_cap_lamports=budget,
        max_transactions_per_day=1,
        allowed_instructions=frozenset(
            {AllowedInstruction(SYSTEM_PROGRAM_ID, struct.pack("<I", _SYSTEM_TRANSFER))}
        ),
        allowed_destinations=destinations,
        token_caps=TokenCaps.none(),
    )


def build_payment(
    *,
    payer: str,
    fee_wallet: str,
    amount_usdc_micro: int,
    pay_with: PayWith,
    settlement_wallet: str | None = None,
    sol_pricer: SolPricer | None = None,
) -> PaymentBuild:
    """The unsigned payment of ``amount_usdc_micro`` (in USDC, or its SOL equivalent).

    The blockhash is the zero placeholder: :func:`gecko.autonomous_purchase.run_purchase`
    stamps a fresh one from the node it simulates against and sends to.
    """
    if amount_usdc_micro <= 0:
        raise BillingError("there is nothing to build for a zero payment")
    if payer == fee_wallet:
        raise BillingError("the payer cannot be the fee wallet")
    payee = usdc_ata(fee_wallet)
    if pay_with == "USDC":
        source = usdc_ata(payer)
        transfer = transfer_checked_ix(
            source=source,
            mint=USDC_MINT,
            destination=payee,
            owner=payer,
            amount=amount_usdc_micro,
            decimals=USDC_DECIMALS,
        )
        return PaymentBuild(
            built=assemble_unsigned_tx([transfer], payer),
            pay_with="USDC",
            payer=payer,
            payee_usdc_ata=payee,
            amount_usdc_micro=amount_usdc_micro,
            policy=payment_policy(
                amount_usdc_micro=amount_usdc_micro,
                destinations=frozenset({source, payee}),
            ),
        )
    if pay_with != "SOL":
        raise BillingError("pay_with must be USDC or SOL")
    if settlement_wallet is None:
        raise BillingError(
            "paying in SOL needs the settlement wallet (GECKO_SETTLEMENT_WALLET)"
        )
    if settlement_wallet in (payer, fee_wallet):
        raise BillingError(
            "the settlement wallet is neither the payer nor the treasury"
        )
    if sol_pricer is None:
        raise BillingError("paying in SOL needs a pool price (GECKO_SETTLEMENT_POOL)")
    sol = sol_pricer(amount_usdc_micro)
    if sol.lamports <= 0 or sol.rate_micro <= 0:
        raise BillingError("the pool priced this quote at nothing; refusing to build")
    transfer = system_transfer_ix(
        source=payer, destination=settlement_wallet, lamports=sol.lamports
    )
    return PaymentBuild(
        built=assemble_unsigned_tx([transfer], payer),
        pay_with="SOL",
        payer=payer,
        payee_usdc_ata=payee,
        amount_usdc_micro=amount_usdc_micro,
        policy=payment_policy(
            amount_usdc_micro=amount_usdc_micro,
            destinations=frozenset({settlement_wallet}),
            lamports=sol.lamports,
        ),
        settlement_wallet=settlement_wallet,
        sol=sol,
    )


def whirlpool_sol_pricer(
    *,
    rpc_url: str,
    rpc_call: RpcCall,
    idl_fetch: IdlFetch,
    pool: str,
    margin_bps: int,
) -> SolPricer:
    """The live :data:`SolPricer`: one read of the PINNED pool, priced in integers.

    The lamports are the SMALLEST input whose floor, after the pool's fee and
    ``margin_bps``, clears the quote (:func:`gecko.whirlpool_math.size_input_for_output`):
    the margin is what lets the later settlement swap, priced the same way, still return
    the quote. A pool that is not owned by the Whirlpool program, or not SOL/USDC, is
    refused rather than priced.
    """
    from ..whirlpool_math import WhirlpoolMathError, size_input_for_output, spot_out
    from ..whirlpool_venue import (
        WhirlpoolIdlIncomplete,
        decode_whirlpool,
        whirlpool_layout,
    )

    def price(usdc_micro: int) -> SolQuote:
        answer = rpc_call(rpc_url, "getAccountInfo", [pool, {"encoding": "base64"}])
        value = (answer.get("result") or {}).get("value")
        if not value:
            raise BillingError("the settlement pool does not exist on this network")
        if value.get("owner") != WHIRLPOOL_PROGRAM:
            raise BillingError("the settlement pool is not a Whirlpool account")
        try:
            state = decode_whirlpool(
                base64.b64decode(value["data"][0]),
                whirlpool_layout(idl_fetch(WHIRLPOOL_PROGRAM)),
            )
            if {state.token_mint_a, state.token_mint_b} != {NATIVE_SOL_MINT, USDC_MINT}:
                raise BillingError("the settlement pool is not a SOL/USDC pool")
            a_to_b = state.token_mint_a == NATIVE_SOL_MINT
            lamports = size_input_for_output(
                usdc_micro,
                state.sqrt_price,
                state.fee_rate,
                a_to_b=a_to_b,
                slippage_bps=margin_bps,
            )
            rate = spot_out(1_000_000_000, state.sqrt_price, a_to_b=a_to_b)
        except (WhirlpoolMathError, WhirlpoolIdlIncomplete) as exc:
            raise BillingError(f"the SOL price could not be read: {exc}") from None
        return SolQuote(
            lamports=lamports, rate_micro=rate, pool=pool, margin_bps=margin_bps
        )

    return price
