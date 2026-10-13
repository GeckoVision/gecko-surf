"""What the user's Privy wallet may sign, written twice: into the wallet, and into our gate.

Review finding 2: the wallet was created app-owned with no restriction, so anybody holding
the app secret could have it sign anything. Now it is restricted by one policy, authored
here from config and nothing else:

* SPL USDC ``TransferChecked`` to the cold treasury's USDC account (``GECKO_FEE_WALLET``),
  up to ``max_run_usdc_micro``;
* System ``Transfer`` to Gecko's SETTLEMENT wallet (``GECKO_SETTLEMENT_WALLET``), up to
  ``max_run_sol_lamports`` (option B, founder ruling 9 Oct).

Every other instruction matches no ALLOW rule and is denied: no swap, no wrap, no
compute-budget bid, no account create. A SOL payment is one plain transfer; the swap to
USDC happens later, in a transaction the SETTLEMENT wallet signs
(:mod:`gecko.anyquery.settlement`), so the user's wallet never needs a rule that names a
whole program. The first live wallet (9 Oct) was born under the older rule "SOL to the
treasury"; :func:`gecko.anyquery.privy_admin.update_user_policy` replaces its rules.

With no settlement wallet configured, the SOL rule is left out entirely: the wallet can
then pay in USDC only, which is the honest answer rather than a rule to the treasury that
nothing settles.

**Defence in depth.** :func:`check_wallet_policy` evaluates the same rules over the exact
bytes the signer is about to sign, in our process, before Privy is asked
(:class:`WalletPolicyGate`). It is stricter than Privy where it can see more: amounts are
summed per transaction, and the transfer's source and authority must be the payer.
The words are in :mod:`gecko.anyquery.privy_policy` (confirmed by Privy's echo, 9 Oct).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from ..autonomous_purchase import USDC_DECIMALS, USDC_MINT
from ..landing import SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID
from ..simulate import Receipt
from ..spend_policy import SpendPolicy, SpendPolicyGate, SpendVerdict
from .config import AnyQueryConfig, ConfigError
from .payment_tx import usdc_ata
from .privy_policy import (
    PolicyRefusal,
    decode_instructions,
    policy_body,
    sol_transfer_rule,
    usdc_transfer_rule,
)

__all__ = [
    "USER_POLICY_NAME",
    "WalletPolicy",
    "WalletPolicyGate",
    "check_wallet_policy",
    "privy_policy_body",
    "privy_rules",
    "wallet_policy_from_config",
]

#: The name every user wallet's policy carries in Privy.
USER_POLICY_NAME = "gecko-run-payments"

_SYSTEM_TRANSFER = 2
_TOKEN_TRANSFER_CHECKED = 12


@dataclass(frozen=True)
class WalletPolicy:
    """The one restriction every user wallet is under."""

    treasury: str
    treasury_usdc_account: str
    #: Where SOL may go. ``None``: no settlement wallet is configured, so no SOL at all.
    settlement: str | None
    max_usdc_micro: int
    max_lamports: int


def wallet_policy_from_config(config: AnyQueryConfig) -> WalletPolicy:
    """The policy from config alone. No treasury, no policy, so no wallet."""
    if not config.fee_wallet:
        raise ConfigError("GECKO_FEE_WALLET is required to restrict a user's wallet")
    if config.max_run_usdc_micro <= 0 or config.max_run_sol_lamports <= 0:
        raise ConfigError("the per-transaction ceilings must be positive")
    if config.settlement_wallet == config.fee_wallet:
        raise ConfigError(
            "GECKO_SETTLEMENT_WALLET must not be the cold treasury: the treasury is "
            "receive-only and cannot sign a settlement"
        )
    return WalletPolicy(
        treasury=config.fee_wallet,
        treasury_usdc_account=usdc_ata(config.fee_wallet),
        settlement=config.settlement_wallet,
        max_usdc_micro=config.max_run_usdc_micro,
        max_lamports=config.max_run_sol_lamports,
    )


def privy_rules(policy: WalletPolicy) -> list[dict[str, Any]]:
    """The ALLOW rules, in order: USDC to the treasury; SOL to the settlement wallet."""
    rules = [
        usdc_transfer_rule(
            "USDC to the treasury, capped",
            destination=policy.treasury_usdc_account,
            mint=USDC_MINT,
            cap=policy.max_usdc_micro,
        )
    ]
    if policy.settlement is not None:
        rules.append(
            sol_transfer_rule(
                "SOL to the settlement wallet, capped",
                to=policy.settlement,
                cap=policy.max_lamports,
            )
        )
    return rules


def privy_policy_body(policy: WalletPolicy, *, name: str) -> dict[str, Any]:
    """The Privy policy body: ALLOW rules only (unmatched is denied), no program rule."""
    return policy_body(name, privy_rules(policy))


# --------------------------------------------------------------------------------------
# The local check
# --------------------------------------------------------------------------------------


def _evaluate(transaction: str | bytes, policy: WalletPolicy, payer: str) -> None:
    usdc_out = 0
    lamports_out = 0
    own_usdc = usdc_ata(payer)
    for position, ix in enumerate(decode_instructions(transaction)):
        where = f"instruction {position}"
        if ix.program == SYSTEM_PROGRAM_ID:
            if (
                len(ix.data) != 12
                or struct.unpack_from("<I", ix.data)[0] != _SYSTEM_TRANSFER
            ):
                raise PolicyRefusal(
                    "instruction-not-allowlisted",
                    f"{where} is a System instruction other than Transfer",
                )
            source, to = ix.accounts[0], ix.accounts[1]
            if policy.settlement is None or to != policy.settlement:
                raise PolicyRefusal(
                    "destination-not-allowlisted",
                    f"{where} sends SOL to {to}, which is not the settlement wallet",
                )
            if source != payer:
                raise PolicyRefusal(
                    "instruction-not-allowlisted",
                    f"{where} moves SOL out of {source}, not the payer",
                )
            lamports_out += struct.unpack_from("<Q", ix.data, 4)[0]
        elif ix.program == TOKEN_PROGRAM_ID:
            if ix.data[:1] != bytes([_TOKEN_TRANSFER_CHECKED]) or len(ix.data) != 10:
                raise PolicyRefusal(
                    "instruction-not-allowlisted",
                    f"{where} is a token instruction the wallet policy does not allow",
                )
            amount, decimals = struct.unpack_from("<QB", ix.data, 1)
            source, mint, destination, owner = ix.accounts[:4]
            if mint != USDC_MINT or decimals != USDC_DECIMALS:
                raise PolicyRefusal(
                    "instruction-not-allowlisted",
                    f"{where} transfers a token that is not USDC",
                )
            if destination != policy.treasury_usdc_account:
                raise PolicyRefusal(
                    "destination-not-allowlisted",
                    f"{where} sends USDC to {destination}, which is not the treasury's "
                    f"USDC account",
                )
            if source != own_usdc or owner != payer:
                raise PolicyRefusal(
                    "instruction-not-allowlisted",
                    f"{where} moves USDC out of an account that is not the payer's own",
                )
            usdc_out += amount
        else:
            raise PolicyRefusal(
                "program-not-allowlisted",
                f"{where} calls {ix.program}, which the wallet policy does not allow",
            )
    if usdc_out > policy.max_usdc_micro:
        raise PolicyRefusal(
            "over-per-transaction-cap",
            f"the transaction sends {usdc_out} micro-USDC; the ceiling is "
            f"{policy.max_usdc_micro}",
        )
    if lamports_out > policy.max_lamports:
        raise PolicyRefusal(
            "over-per-transaction-cap",
            f"the transaction moves {lamports_out} lamports; the ceiling is "
            f"{policy.max_lamports}",
        )


def check_wallet_policy(
    transaction: str | bytes, policy: WalletPolicy, *, payer: str
) -> SpendVerdict | None:
    """``None`` when every instruction is allowed; else the refusal. Never raises: a
    check that could not read the bytes has refused them."""
    try:
        _evaluate(transaction, policy, payer)
    except PolicyRefusal as refused:
        return SpendVerdict(authorized=False, reason=refused.reason, code=refused.code)
    except Exception as exc:  # noqa: BLE001 - an unreadable transaction is a refusal
        return SpendVerdict(
            authorized=False,
            reason=f"the wallet policy could not be evaluated ({type(exc).__name__})",
            code="undecodable-transaction",
        )
    return None


@dataclass(frozen=True)
class WalletPolicyGate(SpendPolicyGate):
    """The spend gate, with the wallet policy checked FIRST over the same bytes.

    A refusal here never reaches the payment's own spend policy, so it spends no velocity
    budget. With no wallet policy or no payer, it refuses: the restriction is not optional.
    """

    wallet_policy: WalletPolicy | None = None
    payer: str = ""

    def authorize(
        self,
        transaction_base64: str | bytes,
        receipt: Receipt,
        *,
        now: float | None = None,
        agent_supplied_policy: SpendPolicy | None = None,
    ) -> SpendVerdict:
        if self.wallet_policy is None or not self.payer:
            return SpendVerdict(
                authorized=False,
                reason="no wallet policy is configured; the user's wallet signs nothing",
                code="no-policy",
            )
        refused = check_wallet_policy(
            transaction_base64, self.wallet_policy, payer=self.payer
        )
        if refused is not None:
            return refused
        return super().authorize(
            transaction_base64,
            receipt,
            now=now,
            agent_supplied_policy=agent_supplied_policy,
        )
