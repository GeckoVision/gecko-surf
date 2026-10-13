"""What Gecko's settlement wallet may sign, written twice: into Privy, and into our gate.

The settlement wallet is a Privy server wallet Gecko owns (option B, founder ruling
9 Oct). Users who pay in SOL send it a plain transfer; it then signs ONE kind of
transaction (:mod:`gecko.anyquery.settlement_tx`): wrap, sync, swap on the one pool,
forward USDC to the cold treasury. Its Privy policy allows:

* System ``Transfer`` to its OWN wSOL account only, up to ``max_run_sol_lamports``
  (CONFIRMED words);
* every instruction of the SPL Token program (PROGRAM-WIDE): Privy's token vocabulary
  has no ``SyncNative`` (its docs list Transfer, TransferChecked, Burn, MintTo,
  CloseAccount, InitializeAccount3), and a live create 400'd on it, 9 Oct;
* every instruction of the Whirlpool program (UNCONFIRMED words, and PROGRAM-WIDE);
* SPL USDC ``TransferChecked`` to the treasury's USDC account only, up to
  ``max_settlement_usdc_micro`` (CONFIRMED words).

**The Whirlpool and SPL Token rules are program-wide, accepted here, for this wallet only.**
Privy's vocabulary has no field that reads a Whirlpool instruction's accounts, so it
cannot say "this pool" or "out to this account": whoever holds the Privy app secret could
have the enclave sign a ``swap_v2`` of this wallet's wSOL or USDC through a pool of their
own, or any other Whirlpool instruction. The user's wallet carries no such rule for that
reason. This one may, because it only ever holds Gecko's own revenue, briefly and in
small amounts (one run is at most ``max_run_usdc_micro``), never a user's balance; the
app secret is Gecko's own. :func:`check_settlement_policy` closes what Privy cannot, in
our process, before Privy is asked: the swap must be ``swap_v2`` on the PINNED pool,
signed by this wallet, wSOL in from its own account and USDC out to its own account.

**The address problem.** The wrap rule names the wallet's own wSOL account, which does not
exist until Privy answers the create. So the founder's create is three calls: store the
policy without the wrap rule, create the wallet under it, then replace the policy's rules
with the full set naming the new address (:func:`privy_settlement_rules`). Until that
third call lands, the wallet can wrap nothing and so settle nothing: the safe direction.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..autonomous_purchase import (
    SWAP_V2_DISCRIMINATOR,
    USDC_DECIMALS,
    USDC_MINT,
    WHIRLPOOL_PROGRAM,
)
from ..landing import NATIVE_SOL_MINT, SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID
from ..simulate import Receipt
from ..spend_policy import SpendPolicy, SpendPolicyGate, SpendVerdict
from .config import AnyQueryConfig, ConfigError
from .payment_tx import usdc_ata
from .privy_policy import (
    PolicyRefusal,
    decode_instructions,
    policy_body,
    program_rule,
    sol_transfer_rule,
    usdc_transfer_rule,
)
from .settlement_tx import wsol_ata
from .x402_policy import privy_x402_rules

__all__ = [
    "SETTLEMENT_POLICY_NAME",
    "SettlementPolicy",
    "SettlementPolicyGate",
    "check_settlement_policy",
    "privy_settlement_body",
    "privy_settlement_rules",
    "settlement_policy_from_config",
]

#: The name the settlement wallet's policy carries in Privy.
SETTLEMENT_POLICY_NAME = "gecko-settlement"

_SYSTEM_TRANSFER = 2
_TOKEN_TRANSFER_CHECKED = 12
_TOKEN_SYNC_NATIVE = 17
#: ``swap_v2`` account positions (Whirlpool IDL order) and the ``a_to_b`` flag's byte:
#: discriminator 8 + amount 8 + threshold 8 + sqrt_price_limit 16 + specified_is_input 1.
_SWAP_AUTHORITY = 3
_SWAP_POOL = 4
_SWAP_MINT_A, _SWAP_MINT_B = 5, 6
_SWAP_OWNER_A, _SWAP_OWNER_B = 7, 9
_SWAP_SPECIFIED_IS_INPUT_OFFSET = 40
_SWAP_A_TO_B_OFFSET = 41


@dataclass(frozen=True)
class SettlementPolicy:
    """The one restriction the settlement wallet is under."""

    wallet: str
    wsol_account: str
    usdc_account: str
    pool: str
    treasury_usdc_account: str
    max_lamports: int
    max_usdc_micro: int


def settlement_policy_from_config(
    config: AnyQueryConfig, *, wallet: str | None = None
) -> SettlementPolicy:
    """The policy from config alone. ``wallet`` overrides the configured address only
    for the founder's create, where the address is Privy's answer, read a moment ago."""
    address = wallet or config.settlement_wallet
    if not config.fee_wallet:
        raise ConfigError("GECKO_FEE_WALLET is required to restrict the settlement")
    if not address:
        raise ConfigError("GECKO_SETTLEMENT_WALLET is required to settle")
    if address == config.fee_wallet:
        raise ConfigError("the settlement wallet must not be the cold treasury")
    if not config.settlement_pool:
        raise ConfigError("GECKO_SETTLEMENT_POOL is required to settle")
    if config.max_run_sol_lamports <= 0 or config.max_settlement_usdc_micro <= 0:
        raise ConfigError("the settlement ceilings must be positive")
    return SettlementPolicy(
        wallet=address,
        wsol_account=wsol_ata(address),
        usdc_account=usdc_ata(address),
        pool=config.settlement_pool,
        treasury_usdc_account=usdc_ata(config.fee_wallet),
        max_lamports=config.max_run_sol_lamports,
        max_usdc_micro=config.max_settlement_usdc_micro,
    )


def privy_settlement_rules(
    *,
    treasury_usdc_account: str,
    max_usdc_micro: int,
    wallet: str | None,
    max_lamports: int,
    x402_pay_tos: Mapping[str, Sequence[str]] | None = None,
    max_x402_call_usdc_micro: int = 0,
) -> list[dict[str, Any]]:
    """The ALLOW rules. ``wallet=None`` is the pre-create set: no wrap rule, because the
    wSOL account it would name does not exist yet. With ``x402_pay_tos`` (payTo -> the
    services that use it) the wallet may also pay per call over x402
    (:mod:`gecko.anyquery.x402_policy`, founder ruling 9 Oct): USDC to those payTos'
    accounts only, each at most ``max_x402_call_usdc_micro``, plus the compute-budget and
    memo instructions an x402 payment carries."""
    rules: list[dict[str, Any]] = []
    if wallet is not None:
        rules.append(
            sol_transfer_rule(
                "wrap SOL into own wSOL account, capped",
                to=wsol_ata(wallet),
                cap=max_lamports,
            )
        )
    rules += [
        # Privy's token vocabulary has no SyncNative (docs, 9 Oct; the first live create
        # 400'd on it), so the SPL Token program is allowed program-wide, for this wallet
        # only and for the same reason as Whirlpool below (founder ruling, 9 Oct).
        program_rule(
            "SPL Token program (pinned by our check)",
            TOKEN_PROGRAM_ID,
        ),
        program_rule(
            "Whirlpool program (pinned by our check)",
            WHIRLPOOL_PROGRAM,
        ),
        usdc_transfer_rule(
            "USDC to the treasury, capped",
            destination=treasury_usdc_account,
            mint=USDC_MINT,
            cap=max_usdc_micro,
        ),
    ]
    if x402_pay_tos:
        rules += privy_x402_rules(
            pay_tos=x402_pay_tos, max_call_usdc_micro=max_x402_call_usdc_micro
        )
    return rules


def privy_settlement_body(
    policy: SettlementPolicy,
    *,
    name: str,
    x402_pay_tos: Mapping[str, Sequence[str]] | None = None,
    max_x402_call_usdc_micro: int = 0,
) -> dict[str, Any]:
    """The full policy body, naming the wallet's own wSOL account."""
    return policy_body(
        name,
        privy_settlement_rules(
            treasury_usdc_account=policy.treasury_usdc_account,
            max_usdc_micro=policy.max_usdc_micro,
            wallet=policy.wallet,
            max_lamports=policy.max_lamports,
            x402_pay_tos=x402_pay_tos,
            max_x402_call_usdc_micro=max_x402_call_usdc_micro,
        ),
    )


# --------------------------------------------------------------------------------------
# The local check
# --------------------------------------------------------------------------------------


def _check_swap(
    where: str, data: bytes, accounts: list[str], p: SettlementPolicy
) -> None:
    """``swap_v2``, exact input, on the pinned pool: own wSOL in, own USDC out."""
    if data[: len(SWAP_V2_DISCRIMINATOR)] != SWAP_V2_DISCRIMINATOR:
        raise PolicyRefusal(
            "instruction-not-allowlisted",
            f"{where} calls the Whirlpool program, but not swap_v2",
        )
    if len(data) <= _SWAP_A_TO_B_OFFSET or len(accounts) <= _SWAP_OWNER_B:
        raise PolicyRefusal(
            "undecodable-transaction", f"{where} is a truncated swap_v2"
        )
    if accounts[_SWAP_POOL] != p.pool:
        raise PolicyRefusal(
            "destination-not-allowlisted",
            f"{where} swaps on {accounts[_SWAP_POOL]}, not the pinned pool",
        )
    if accounts[_SWAP_AUTHORITY] != p.wallet:
        raise PolicyRefusal(
            "instruction-not-allowlisted",
            f"{where} swaps on behalf of {accounts[_SWAP_AUTHORITY]}, not the wallet",
        )
    if data[_SWAP_SPECIFIED_IS_INPUT_OFFSET] != 1:
        raise PolicyRefusal(
            "instruction-not-allowlisted",
            f"{where} is an exact-output swap; settlements sell an exact input",
        )
    mint_a, mint_b = accounts[_SWAP_MINT_A], accounts[_SWAP_MINT_B]
    if {mint_a, mint_b} != {NATIVE_SOL_MINT, USDC_MINT}:
        raise PolicyRefusal(
            "instruction-not-allowlisted",
            f"{where} swaps on a pool that is not SOL/USDC",
        )
    a_to_b = data[_SWAP_A_TO_B_OFFSET] == 1
    if (mint_a if a_to_b else mint_b) != NATIVE_SOL_MINT:
        raise PolicyRefusal(
            "instruction-not-allowlisted",
            f"{where} sells USDC; a settlement only sells SOL",
        )
    own = {NATIVE_SOL_MINT: p.wsol_account, USDC_MINT: p.usdc_account}
    for mint, account in (
        (mint_a, accounts[_SWAP_OWNER_A]),
        (mint_b, accounts[_SWAP_OWNER_B]),
    ):
        if account != own[mint]:
            raise PolicyRefusal(
                "destination-not-allowlisted",
                f"{where} swaps through {account}, which is not the wallet's own "
                f"account for that token",
            )


def _evaluate(transaction: str | bytes, p: SettlementPolicy) -> None:  # noqa: C901 - one flat rule table
    lamports_out = 0
    usdc_out = 0
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
            if ix.accounts[0] != p.wallet or ix.accounts[1] != p.wsol_account:
                raise PolicyRefusal(
                    "destination-not-allowlisted",
                    f"{where} sends SOL to {ix.accounts[1]}; only the wallet's own wSOL "
                    f"account is allowed",
                )
            lamports_out += struct.unpack_from("<Q", ix.data, 4)[0]
        elif ix.program == TOKEN_PROGRAM_ID:
            kind = ix.data[0] if ix.data else -1
            if kind == _TOKEN_SYNC_NATIVE and len(ix.data) == 1:
                if ix.accounts[0] != p.wsol_account:
                    raise PolicyRefusal(
                        "destination-not-allowlisted",
                        f"{where} syncs an account that is not the wallet's wSOL",
                    )
            elif kind == _TOKEN_TRANSFER_CHECKED and len(ix.data) == 10:
                amount, decimals = struct.unpack_from("<QB", ix.data, 1)
                source, mint, destination, owner = ix.accounts[:4]
                if mint != USDC_MINT or decimals != USDC_DECIMALS:
                    raise PolicyRefusal(
                        "instruction-not-allowlisted",
                        f"{where} transfers a token that is not USDC",
                    )
                if destination != p.treasury_usdc_account:
                    raise PolicyRefusal(
                        "destination-not-allowlisted",
                        f"{where} sends USDC to {destination}, which is not the "
                        f"treasury's USDC account",
                    )
                if source != p.usdc_account or owner != p.wallet:
                    raise PolicyRefusal(
                        "instruction-not-allowlisted",
                        f"{where} moves USDC out of an account that is not the "
                        f"wallet's own",
                    )
                usdc_out += amount
            else:
                raise PolicyRefusal(
                    "instruction-not-allowlisted",
                    f"{where} is a token instruction the settlement does not use",
                )
        elif ix.program == WHIRLPOOL_PROGRAM:
            _check_swap(where, ix.data, ix.accounts, p)
        else:
            raise PolicyRefusal(
                "program-not-allowlisted",
                f"{where} calls {ix.program}, which the settlement does not use",
            )
    if lamports_out > p.max_lamports:
        raise PolicyRefusal(
            "over-per-transaction-cap",
            f"the settlement wraps {lamports_out} lamports; the ceiling is "
            f"{p.max_lamports}",
        )
    if usdc_out > p.max_usdc_micro:
        raise PolicyRefusal(
            "over-per-transaction-cap",
            f"the settlement forwards {usdc_out} micro-USDC; the ceiling is "
            f"{p.max_usdc_micro}",
        )


def check_settlement_policy(
    transaction: str | bytes, policy: SettlementPolicy
) -> SpendVerdict | None:
    """``None`` when every instruction is allowed; else the refusal. Never raises."""
    try:
        _evaluate(transaction, policy)
    except PolicyRefusal as refused:
        return SpendVerdict(authorized=False, reason=refused.reason, code=refused.code)
    except Exception as exc:  # noqa: BLE001 - an unreadable transaction is a refusal
        return SpendVerdict(
            authorized=False,
            reason=f"the settlement policy could not be evaluated ({type(exc).__name__})",
            code="undecodable-transaction",
        )
    return None


@dataclass(frozen=True)
class SettlementPolicyGate(SpendPolicyGate):
    """The spend gate, with the settlement policy checked FIRST over the same bytes."""

    settlement_policy: SettlementPolicy | None = None

    def authorize(
        self,
        transaction_base64: str | bytes,
        receipt: Receipt,
        *,
        now: float | None = None,
        agent_supplied_policy: SpendPolicy | None = None,
    ) -> SpendVerdict:
        if self.settlement_policy is None:
            return SpendVerdict(
                authorized=False,
                reason="no settlement policy is configured; the wallet signs nothing",
                code="no-policy",
            )
        refused = check_settlement_policy(transaction_base64, self.settlement_policy)
        if refused is not None:
            return refused
        return super().authorize(
            transaction_base64,
            receipt,
            now=now,
            agent_supplied_policy=agent_supplied_policy,
        )
