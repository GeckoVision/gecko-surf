"""What the settlement wallet may sign to pay Orthogonal over x402, written twice.

Founder ruling, 9 Oct: the runner pays each Orthogonal call over x402 on Solana from
Gecko's SETTLEMENT wallet, out of the USDC float it keeps. One x402 ``exact`` payment is
one transaction (:mod:`gecko.x402_svm`): two compute-budget instructions, one USDC
``TransferChecked`` and a memo; the FACILITATOR is the fee payer, the wallet signs as the
transfer's authority only.

**Into Privy** (:func:`privy_x402_rules`, added to the settlement wallet's policy):

* one ``USDC to <provider>, capped`` per pinned ``payTo``: ``TransferChecked`` of USDC to
  that payTo's USDC account, at most ``max_x402_call_usdc_micro`` (CONFIRMED words).
  Measured 9 Oct: Orthogonal's x402 host names a DIFFERENT payTo per provider, so the
  set comes from the registry's probes (:func:`gecko.servicemap.pins.pinned_pay_tos`);
* the ComputeBudget program and the Memo program, program-wide (UNCONFIRMED words, the
  same ``programId`` rule the policy already carries for SPL Token and Whirlpool). Both
  move nothing of the wallet's: the compute price is paid by the facilitator, and a memo
  touches no account. Without them Privy's default-deny refuses the payment's other
  three instructions.

The SPL Token program is ALREADY allowed program-wide on this wallet (founder ruling,
9 Oct, for SyncNative), so Privy alone does not pin where USDC goes. The Orthogonal rule
says what we mean; the pin that binds is ours:

**Into our gate** (:class:`X402PaymentGate`, in our process, before Privy is asked): the
bytes are exactly the reference shape, the fee payer is the challenge's and not the
wallet, the transfer moves USDC from the wallet's own USDC account to the USDC account of
the payTo PINNED for this operation (the registry's last probe, or the env override), signed by the wallet, for exactly the challenge's amount, and that
amount is within the per-call cap. Then the ordinary spend gate runs over a policy for
THIS payment only.
"""

from __future__ import annotations

import base64
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..autonomous_purchase import USDC_DECIMALS, USDC_MINT
from ..landing import TOKEN_PROGRAM_ID
from ..simulate import Receipt
from ..spend_policy import (
    AllowedInstruction,
    SpendPolicy,
    SpendPolicyGate,
    SpendVerdict,
    TokenCap,
    TokenCaps,
)
from ..txbind import DISCRIMINATOR_CEILING
from ..x402_svm import COMPUTE_BUDGET_PROGRAM, MEMO_PROGRAM
from .config import AnyQueryConfig, ConfigError, is_pubkey
from .payment_tx import usdc_ata
from .privy_policy import (
    PolicyRefusal,
    decode_instructions,
    program_rule,
    usdc_transfer_rule,
)

__all__ = [
    "MAX_COMPUTE_UNIT_PRICE_MICROLAMPORTS",
    "X402Expectation",
    "X402PaymentGate",
    "X402PayPolicy",
    "check_x402_payment",
    "privy_x402_rules",
    "x402_rule_name",
    "x402_pay_policy_from_config",
    "x402_spend_policy",
]

#: The reference facilitator refuses a compute price above 5 lamports per unit.
MAX_COMPUTE_UNIT_PRICE_MICROLAMPORTS = 5_000_000
_SET_CU_LIMIT = 2
_SET_CU_PRICE = 3
_TRANSFER_CHECKED = 12


@dataclass(frozen=True)
class X402PayPolicy:
    """The one restriction an x402 payment from the settlement wallet is under.

    ``pay_to`` is ``None`` until a payment is pinned to one: per operation from the
    registry (:meth:`pinned_to`), or for every call by the env override. The gate
    refuses a payment whose policy names no payTo."""

    wallet: str
    wallet_usdc_account: str
    pay_to: str | None
    pay_to_usdc_account: str | None
    max_call_usdc_micro: int

    def pinned_to(self, pay_to: str) -> X402PayPolicy:
        """This policy for ONE payee. Refuses an address that is not one, or that is
        the paying wallet itself."""
        if not is_pubkey(pay_to):
            raise ConfigError("the pinned payTo is not a valid Solana address")
        if pay_to == self.wallet:
            raise ConfigError("the pinned payTo must not be the settlement wallet")
        return replace(self, pay_to=pay_to, pay_to_usdc_account=usdc_ata(pay_to))


def x402_pay_policy_from_config(config: AnyQueryConfig) -> X402PayPolicy:
    """From config alone. The payer is the settlement wallet; the payee is the env
    override when set, else pinned per call from the registry, never the address a
    challenge names."""
    wallet = config.settlement_wallet
    if not wallet:
        raise ConfigError("GECKO_SETTLEMENT_WALLET is required to pay over x402")
    if config.max_x402_call_usdc_micro <= 0:
        raise ConfigError("GECKO_ORTHOGONAL_X402_MAX_CALL_USDC_MICRO must be positive")
    policy = X402PayPolicy(
        wallet=wallet,
        wallet_usdc_account=usdc_ata(wallet),
        pay_to=None,
        pay_to_usdc_account=None,
        max_call_usdc_micro=config.max_x402_call_usdc_micro,
    )
    override = config.orthogonal_x402_pay_to
    return policy if override is None else policy.pinned_to(override)


#: Privy refuses a rule name of 50 characters or more (live, 9 Oct).
_MAX_RULE_NAME = 49


def x402_rule_name(services: Sequence[str], pay_to: str) -> str:
    """``USDC to <provider> (<payTo prefix>), capped``, under 50 characters. The prefix
    keeps two rules apart when a long provider list is cut."""
    tail = f" ({pay_to[:4]}), capped"
    room = _MAX_RULE_NAME - len("USDC to ") - len(tail)
    label = "+".join(services) or "x402 payee"
    if len(label) > room:
        label = label[: room - 3] + "..."
    return f"USDC to {label}{tail}"


def privy_x402_rules(
    *, pay_tos: Mapping[str, Sequence[str]], max_call_usdc_micro: int
) -> list[dict[str, Any]]:
    """The ALLOW rules the settlement policy gains: one capped USDC transfer per pinned
    ``payTo`` (``pay_tos``: payTo -> the services that use it), then ComputeBudget and
    Memo once. No payTo, no rules: an x402 payment then has nowhere to go."""
    if not pay_tos:
        return []
    if max_call_usdc_micro <= 0:
        raise ConfigError("the x402 per-call cap must be positive")
    rules = [
        usdc_transfer_rule(
            x402_rule_name(tuple(services), pay_to),
            destination=usdc_ata(pay_to),
            mint=USDC_MINT,
            cap=max_call_usdc_micro,
        )
        for pay_to, services in sorted(pay_tos.items())
    ]
    return [
        *rules,
        program_rule(
            "ComputeBudget for x402 (pinned by our check)", COMPUTE_BUDGET_PROGRAM
        ),
        program_rule("Memo for x402 (pinned by our check)", MEMO_PROGRAM),
    ]


@dataclass(frozen=True)
class X402Expectation:
    """What THIS payment must be: the policy, plus the two facts the challenge set."""

    policy: X402PayPolicy
    amount_usdc_micro: int
    fee_payer: str


def _fee_payer(transaction: str | bytes) -> str:
    from solders.transaction import VersionedTransaction

    raw = (
        transaction
        if isinstance(transaction, bytes)
        else base64.b64decode(transaction, validate=True)
    )
    return str(VersionedTransaction.from_bytes(raw).message.account_keys[0])


def _evaluate(transaction: str | bytes, want: X402Expectation) -> None:  # noqa: C901 - one flat shape
    p = want.policy
    if p.pay_to_usdc_account is None:
        raise PolicyRefusal(
            "destination-not-allowlisted",
            "no payTo is pinned for this payment; the wallet signs nothing",
        )
    if want.amount_usdc_micro <= 0:
        raise PolicyRefusal(
            "over-per-transaction-cap", "the payment amount is not positive"
        )
    if want.amount_usdc_micro > p.max_call_usdc_micro:
        raise PolicyRefusal(
            "over-per-transaction-cap",
            f"the payment is {want.amount_usdc_micro} micro-USDC; one x402 call is "
            f"capped at {p.max_call_usdc_micro}",
        )
    payer = _fee_payer(transaction)
    if payer == p.wallet:
        raise PolicyRefusal(
            "instruction-not-allowlisted",
            "the settlement wallet is the fee payer; an x402 payment's fee is the "
            "facilitator's",
        )
    if payer != want.fee_payer:
        raise PolicyRefusal(
            "instruction-not-allowlisted",
            "the fee payer is not the one the challenge named",
        )
    ixs = decode_instructions(transaction)
    if len(ixs) not in (3, 4):
        raise PolicyRefusal(
            "instruction-not-allowlisted",
            f"an x402 payment has 3 or 4 instructions, these bytes carry {len(ixs)}",
        )
    limit, price, transfer = ixs[0], ixs[1], ixs[2]
    if (
        limit.program != COMPUTE_BUDGET_PROGRAM
        or len(limit.data) != 5
        or limit.data[0] != _SET_CU_LIMIT
    ):
        raise PolicyRefusal(
            "instruction-not-allowlisted",
            "instruction 0 is not SetComputeUnitLimit",
        )
    if (
        price.program != COMPUTE_BUDGET_PROGRAM
        or len(price.data) != 9
        or price.data[0] != _SET_CU_PRICE
    ):
        raise PolicyRefusal(
            "instruction-not-allowlisted",
            "instruction 1 is not SetComputeUnitPrice",
        )
    if (
        struct.unpack_from("<Q", price.data, 1)[0]
        > MAX_COMPUTE_UNIT_PRICE_MICROLAMPORTS
    ):
        raise PolicyRefusal(
            "over-per-transaction-cap",
            "the compute price is above what the facilitator accepts",
        )
    if (
        transfer.program != TOKEN_PROGRAM_ID
        or len(transfer.data) != 10
        or transfer.data[0] != _TRANSFER_CHECKED
        or len(transfer.accounts) != 4
    ):
        raise PolicyRefusal(
            "instruction-not-allowlisted",
            "instruction 2 is not an SPL Token TransferChecked",
        )
    amount, decimals = struct.unpack_from("<QB", transfer.data, 1)
    source, mint, destination, owner = transfer.accounts
    if mint != USDC_MINT or decimals != USDC_DECIMALS:
        raise PolicyRefusal(
            "instruction-not-allowlisted",
            "the payment transfers a token that is not USDC",
        )
    if destination != p.pay_to_usdc_account:
        raise PolicyRefusal(
            "destination-not-allowlisted",
            f"the payment sends USDC to {destination}, which is not the USDC account "
            f"of the payTo pinned for this operation",
        )
    if source != p.wallet_usdc_account or owner != p.wallet:
        raise PolicyRefusal(
            "instruction-not-allowlisted",
            "the payment moves USDC out of an account that is not the wallet's own",
        )
    if amount != want.amount_usdc_micro:
        raise PolicyRefusal(
            "over-per-transaction-cap",
            f"the payment moves {amount} micro-USDC; the challenge asked for "
            f"{want.amount_usdc_micro}",
        )
    if len(ixs) == 4:
        memo = ixs[3]
        if memo.program != MEMO_PROGRAM or memo.accounts:
            raise PolicyRefusal(
                "program-not-allowlisted",
                "instruction 3 is not a plain memo",
            )


def check_x402_payment(
    transaction: str | bytes, expectation: X402Expectation
) -> SpendVerdict | None:
    """``None`` when the bytes are exactly the expected payment; else the refusal.
    Never raises."""
    try:
        _evaluate(transaction, expectation)
    except PolicyRefusal as refused:
        return SpendVerdict(authorized=False, reason=refused.reason, code=refused.code)
    except Exception as exc:  # noqa: BLE001 - an unreadable transaction is a refusal
        return SpendVerdict(
            authorized=False,
            reason=f"the x402 payment could not be evaluated ({type(exc).__name__})",
            code="undecodable-transaction",
        )
    return None


def x402_spend_policy(expectation: X402Expectation, *, memo: bytes) -> SpendPolicy:
    """The spend policy for THIS payment and nothing wider: its four instructions, its
    two token accounts, no lamports out of the wallet, and the challenge's USDC."""
    p = expectation.policy
    amount = expectation.amount_usdc_micro
    allowed = {
        AllowedInstruction(COMPUTE_BUDGET_PROGRAM, bytes([_SET_CU_LIMIT])),
        AllowedInstruction(COMPUTE_BUDGET_PROGRAM, bytes([_SET_CU_PRICE])),
        AllowedInstruction(TOKEN_PROGRAM_ID, bytes([_TRANSFER_CHECKED])),
    }
    if memo:
        allowed.add(AllowedInstruction(MEMO_PROGRAM, memo[:DISCRIMINATOR_CEILING]))
    return SpendPolicy(
        authorized=True,
        per_transaction_cap_lamports=0,
        hourly_cap_lamports=0,
        daily_cap_lamports=0,
        max_transactions_per_day=1,
        allowed_instructions=frozenset(allowed),
        allowed_destinations=frozenset(
            a for a in (p.wallet_usdc_account, p.pay_to_usdc_account) if a is not None
        ),
        token_caps=TokenCaps.of(
            [
                TokenCap(
                    mint=USDC_MINT,
                    decimals=USDC_DECIMALS,
                    per_transaction_raw=amount,
                    hourly_raw=amount,
                    daily_raw=amount,
                )
            ]
        ),
    )


@dataclass(frozen=True)
class X402PaymentGate(SpendPolicyGate):
    """The spend gate, with the x402 payment's exact shape checked FIRST."""

    expectation: X402Expectation | None = None

    def authorize(
        self,
        transaction_base64: str | bytes,
        receipt: Receipt,
        *,
        now: float | None = None,
        agent_supplied_policy: SpendPolicy | None = None,
    ) -> SpendVerdict:
        if self.expectation is None:
            return SpendVerdict(
                authorized=False,
                reason="no x402 expectation is configured; the wallet signs nothing",
                code="no-policy",
            )
        refused = check_x402_payment(transaction_base64, self.expectation)
        if refused is not None:
            return refused
        return super().authorize(
            transaction_base64,
            receipt,
            now=now,
            agent_supplied_policy=agent_supplied_policy,
        )
