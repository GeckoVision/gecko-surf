"""SolanaBilling, offline: every RPC and the signer are injected fakes.

Option B (founder ruling, 9 Oct): a SOL payment is ONE System transfer to Gecko's
settlement wallet and is final when it lands; the swap is the settlement's
(``tests/test_anyquery_settlement.py``), and rebates are matched to the settlement's slot.

The transaction bytes are REAL (assembled with solders and decoded by the same code the
spend gate uses); the node's answers are mainnet-SHAPED and synthesized, so these tests
prove what the billing does GIVEN such answers, not that mainnet gives them. The live
check is founder-run, never part of this suite.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

pytest.importorskip("solders")

from solders.keypair import Keypair  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

from gecko.anyquery.charge import BillingError, rebate_credit_micro  # noqa: E402
from gecko.anyquery.config import AnyQueryConfig  # noqa: E402
from gecko.anyquery.rebates import MemoryRebateIndex, RebateRow  # noqa: E402
from gecko.anyquery.payment_tx import (  # noqa: E402
    SolQuote,
    build_payment,
    usdc_ata,
)
from gecko.anyquery.settlement_tx import (  # noqa: E402
    SwapLeg,
    leg_from_values,
    wsol_ata,
)
from gecko.anyquery.solana_billing import SolanaBilling  # noqa: E402
from gecko.anyquery.store import DuplicateChargeError, MemoryStore  # noqa: E402
from gecko.anyquery.types import (  # noqa: E402
    LedgerEntry,
    Payment,
    PlannedField,
    PlannedSource,
    QueryPlan,
    Quote,
)
from gecko.autonomous_purchase import SWAP_V2_DISCRIMINATOR, USDC_MINT  # noqa: E402
from gecko.rpc import RpcError  # noqa: E402
from gecko.landing import (  # noqa: E402
    NATIVE_SOL_MINT,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from gecko.signer import SigningAttestation  # noqa: E402
from gecko.txbind import decode_message  # noqa: E402
from gecko.wallet_binding import InMemoryWalletDirectory, WalletBinding  # noqa: E402


def _key(seed: int) -> Keypair:
    return Keypair.from_seed(bytes([seed]) * 32)


PAYER_KEY = _key(1)
PAYER = str(PAYER_KEY.pubkey())
FEE_WALLET = str(_key(2).pubkey())
POOL = str(_key(3).pubkey())
VAULT_A = str(_key(4).pubkey())
VAULT_B = str(_key(5).pubkey())
TICKS = [str(_key(6 + i).pubkey()) for i in range(3)]
STRANGER = str(_key(20).pubkey())
#: The Kora relay's fee payer: the only rebate address (founder ruling, 8 Oct).
RELAY = str(_key(30).pubkey())
OTHER_INFLOW_SIG = "3" + "X" * 86
MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
BLOCKHASH = "6xCk4Xgb64QofLjfh5Q5sy47W5dURHagdcDWWhhoAqgo"
PAY_SIG = "5" + "P" * 86
REBATE_SIG = "4" + "R" * 86
SLOT = 400_000_123
FAKE_KEY = "hk-SENTINEL-billing-DO-NOT-LEAK"
CUSTOMER = "acct_alice"
AMOUNT = 20_000  # 0.02 USDC
#: Gecko's settlement wallet (option B): where a SOL payment goes, and who swaps it.
SETTLEMENT = str(_key(40).pubkey())
SETTLEMENT_ID = "settlewallet0001"
RATE_MICRO = 150_000_000  # 150 USDC per SOL at the pool
MARGIN_BPS = 200
SOL_IN = 140_000  # lamports the user pays for AMOUNT at RATE_MICRO plus the margin
USDC_OUT = 21_000  # what a settlement swap of SOL_IN pays out
SETTLE_SIG = "6" + "S" * 86
REBATE_LAMPORTS = 100_000  # 0.0001 SOL observed in the settlement's slot

SWAP_V2_IDL: dict[str, Any] = {
    "instructions": [
        {
            "name": "swap_v2",
            "discriminator": list(SWAP_V2_DISCRIMINATOR),
            # Account order from @orca-so/whirlpools-client getSwapV2Instruction.
            "accounts": [
                {"name": "token_program_a"},
                {"name": "token_program_b"},
                {"name": "memo_program", "address": MEMO},
                {"name": "token_authority", "signer": True},
                {"name": "whirlpool", "writable": True},
                {"name": "token_mint_a"},
                {"name": "token_mint_b"},
                {"name": "token_owner_account_a", "writable": True},
                {"name": "token_vault_a", "writable": True},
                {"name": "token_owner_account_b", "writable": True},
                {"name": "token_vault_b", "writable": True},
                {"name": "tick_array_0", "writable": True},
                {"name": "tick_array_1", "writable": True},
                {"name": "tick_array_2", "writable": True},
                {"name": "oracle", "writable": True},
            ],
            "args": [
                {"name": "amount", "type": "u64"},
                {"name": "other_amount_threshold", "type": "u64"},
                {"name": "sqrt_price_limit", "type": "u128"},
                {"name": "amount_specified_is_input", "type": "bool"},
                {"name": "a_to_b", "type": "bool"},
                {
                    "name": "remaining_accounts_info",
                    "type": {"option": {"defined": {"name": "RemainingAccountsInfo"}}},
                },
            ],
        }
    ]
}


def _swap_values(owner: str, amount_in: int, min_out: int) -> dict[str, Any]:
    return {
        "whirlpool": POOL,
        "token_mint_a": NATIVE_SOL_MINT,
        "token_mint_b": USDC_MINT,
        "token_program_a": TOKEN_PROGRAM_ID,
        "token_program_b": TOKEN_PROGRAM_ID,
        "token_owner_account_a": wsol_ata(owner),
        "token_owner_account_b": usdc_ata(owner),
        "token_vault_a": VAULT_A,
        "token_vault_b": VAULT_B,
        "tick_array_0": TICKS[0],
        "tick_array_1": TICKS[1],
        "tick_array_2": TICKS[2],
        "amount": amount_in,
        "other_amount_threshold": min_out,
        "sqrt_price_limit": 4295048016,
        "amount_specified_is_input": True,
        "a_to_b": True,
        "remaining_accounts_info": None,
    }


def fixed_planner(min_out: int | None = None) -> Any:
    """A settlement SwapPlanner that needs no pool on chain: exact input, a fixed floor."""

    def plan(owner: str, lamports_in: int) -> SwapLeg:
        floor = AMOUNT if min_out is None else min_out
        values = _swap_values(owner, lamports_in, floor)
        return leg_from_values(SWAP_V2_IDL, values, owner=owner, pool=POOL)

    return plan


def fixed_pricer(lamports: int = SOL_IN) -> Any:
    """A SolPricer that needs no pool on chain: the quote is ``lamports``."""

    def price(usdc_micro: int) -> SolQuote:
        return SolQuote(lamports, RATE_MICRO, POOL, MARGIN_BPS)

    return price


def _row(index: int, owner: str, mint: str, amount: int, decimals: int = 6) -> dict:
    return {
        "accountIndex": index,
        "mint": mint,
        "owner": owner,
        "programId": TOKEN_PROGRAM_ID,
        "uiTokenAmount": {
            "amount": str(amount),
            "decimals": decimals,
            "uiAmountString": str(amount / 10**decimals),
        },
    }


@dataclass
class FakeChain:
    """A replaying node. Every answer is mainnet-shaped and synthesized."""

    pay_with: str = "USDC"
    sim_err: Any = None
    landed_err: Any = None
    #: What the fee wallet's USDC account gained in the LANDED transaction.
    landed_received: int = AMOUNT
    #: What the settlement wallet's lamports gained in the LANDED transaction (SOL).
    landed_lamports: int = SOL_IN
    settlement_exists: bool = True
    amount: int = AMOUNT
    payer_usdc: int = 1_000_000
    payer_lamports: int = 1_000_000_000
    fee_ata_exists: bool = True
    #: False: the payment is confirmed but getTransaction does not show it yet.
    visible: bool = True
    rebate_lamports: int = REBATE_LAMPORTS
    #: Who the rebate transaction in the payment's slot pays.
    rebate_to: str = RELAY
    #: Lamports a second, unrelated transaction 50 slots earlier pays the relay.
    other_inflow_lamports: int = 0
    relay_lamports: int = 50_000_000
    #: An exception the confirmation poll raises (a 429, a dropped connection).
    status_error: Exception | None = None
    calls: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    simulations: list[list[Any]] = field(default_factory=list)

    def _token_rows(self, received: int) -> tuple[list[dict], list[dict]]:
        if self.pay_with == "SOL":
            return [], []  # a plain SOL transfer moves no token
        pre = [
            _row(1, PAYER, USDC_MINT, self.payer_usdc),
            _row(2, FEE_WALLET, USDC_MINT, 0),
        ]
        post = [
            _row(1, PAYER, USDC_MINT, self.payer_usdc - self.amount),
            _row(2, FEE_WALLET, USDC_MINT, received),
        ]
        return pre, post

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        self.calls.append(method)
        if method == "getLatestBlockhash":
            return {
                "result": {
                    "value": {"blockhash": BLOCKHASH, "lastValidBlockHeight": 655}
                }
            }
        if method == "getRecentPrioritizationFees":
            return {"result": []}
        if method == "getAccountInfo":
            target = params[0]
            if target == usdc_ata(FEE_WALLET) and not self.fee_ata_exists:
                return {"result": {"value": None}}
            if target == SETTLEMENT and not self.settlement_exists:
                return {"result": {"value": None}}
            return {
                "result": {
                    "value": {"lamports": self.payer_lamports, "data": ["", "base64"]}
                }
            }
        if method == "getBalance":
            if params[0] == RELAY:
                return {"result": {"value": self.relay_lamports}}
            return {"result": {"value": self.payer_lamports}}
        if method == "getTokenAccountBalance":
            return {"result": {"value": {"amount": str(self.payer_usdc)}}}
        if method == "simulateTransaction":
            self.simulations.append(params)
            spent = 5_000 + (SOL_IN if self.pay_with == "SOL" else 0)
            pre, post = self._token_rows(self.amount)
            value = {
                "err": self.sim_err,
                "logs": ["Program log: gecko billing"],
                "unitsConsumed": 30_000,
                "accounts": [
                    {"lamports": self.payer_lamports - spent, "data": ["", "base64"]}
                ],
                "preTokenBalances": pre,
                "postTokenBalances": post,
            }
            return {"result": {"context": {"slot": SLOT - 1}, "value": value}}
        if method == "getSlot":
            return {"result": SLOT - 1}
        if method == "sendTransaction":
            self.sent.append(params[0])
            # One signature per broadcast, as a node gives; the first is PAY_SIG.
            return {
                "result": PAY_SIG
                if len(self.sent) == 1
                else "2" + "Q" * 85 + str(len(self.sent))
            }
        if method == "getSignatureStatuses":
            if self.status_error is not None:
                raise self.status_error
            return {
                "result": {
                    "value": [
                        {"confirmationStatus": "confirmed", "err": None, "slot": SLOT}
                    ]
                }
            }
        if method == "getTransaction":
            if params[0] == OTHER_INFLOW_SIG:
                gained = self.other_inflow_lamports
                return {
                    "result": {
                        "slot": SLOT - 50,
                        "transaction": {"message": {"accountKeys": [STRANGER, RELAY]}},
                        "meta": {
                            "err": None,
                            "preBalances": [10**10, 1_000],
                            "postBalances": [10**10 - gained, 1_000 + gained],
                        },
                    }
                }
            if params[0] == REBATE_SIG:
                return {
                    "result": {
                        "slot": SLOT,
                        "transaction": {
                            "message": {"accountKeys": [STRANGER, self.rebate_to]}
                        },
                        "meta": {
                            "err": None,
                            "preBalances": [10**10, 1_000],
                            "postBalances": [
                                10**10 - self.rebate_lamports,
                                1_000 + self.rebate_lamports,
                            ],
                        },
                    }
                }
            if not self.visible:
                return {"result": None}
            pre, post = self._token_rows(self.landed_received)
            gained = self.landed_lamports if self.pay_with == "SOL" else 0
            return {
                "result": {
                    "slot": SLOT,
                    "transaction": {"message": {"accountKeys": [PAYER, SETTLEMENT]}},
                    "meta": {
                        "err": self.landed_err,
                        "computeUnitsConsumed": 29_000,
                        "preBalances": [self.payer_lamports, 3_000_000],
                        "postBalances": [
                            self.payer_lamports - 5_000 - gained,
                            3_000_000 + gained,
                        ],
                        "preTokenBalances": pre,
                        "postTokenBalances": post,
                    },
                }
            }
        if method == "getSignaturesForAddress":
            # The same history for any address: getTransaction says who gained what.
            return {
                "result": [
                    {"signature": PAY_SIG, "slot": SLOT, "err": None},
                    {"signature": REBATE_SIG, "slot": SLOT, "err": None},
                    {"signature": OTHER_INFLOW_SIG, "slot": SLOT - 50, "err": None},
                ]
            }
        raise AssertionError(f"unscripted RPC method {method}")


@dataclass
class FakeBackend:
    """Signs with a throwaway key whose pubkey IS the payer. Counts every ask."""

    asked: int = 0
    attestations: list[SigningAttestation] = field(default_factory=list)

    @property
    def pubkey(self) -> str:
        return PAYER

    def sign_transaction(
        self, unsigned_transaction: bytes, attestation: SigningAttestation
    ) -> bytes:
        self.asked += 1
        self.attestations.append(attestation)
        tx = Transaction.from_bytes(unsigned_transaction)
        signature = PAYER_KEY.sign_message(bytes(tx.message))
        return bytes(Transaction.populate(tx.message, [signature]))


class _FakeKeyring:
    def get_keyring(self) -> object:
        return object()

    def get_password(self, service: str, user: str) -> str | None:
        return FAKE_KEY if service == "gecko:HELIUS_API_KEY" else None


@dataclass
class RecordingPost:
    chain: FakeChain
    urls: list[str] = field(default_factory=list)

    def __call__(self, url: str, body: bytes) -> dict[str, Any]:
        self.urls.append(url)
        request = json.loads(body)
        return self.chain("helius", request["method"], request["params"])


CONFIG = AnyQueryConfig(
    billing="solana",
    fee_wallet=FEE_WALLET,
    relay_fee_payer=RELAY,
    rebate_split_user_pct=50,
    max_run_usdc_micro=5_000_000,
    settlement_wallet_id=SETTLEMENT_ID,
    settlement_wallet=SETTLEMENT,
    settlement_pool=POOL,
)


@dataclass
class StubSettlement:
    """Stands in for :class:`gecko.anyquery.settlement.Settlement`: records each charge
    it is asked to settle, or raises. Its own suite is test_anyquery_settlement."""

    raises: Exception | None = None
    asked: list[LedgerEntry] = field(default_factory=list)

    def settle(self, charge: LedgerEntry) -> LedgerEntry:
        self.asked.append(charge)
        if self.raises is not None:
            raise self.raises
        return charge


@dataclass
class Rig:
    billing: SolanaBilling
    store: MemoryStore
    chain: FakeChain
    backend: FakeBackend
    settlement: StubSettlement


def _rig(
    *,
    pay_with: str = "USDC",
    bound: str | None = PAYER,
    rebate_index: MemoryRebateIndex | None = None,
    config: AnyQueryConfig = CONFIG,
    settlement: StubSettlement | None = None,
    **chain_kw: Any,
) -> Rig:
    chain = FakeChain(pay_with=pay_with, **chain_kw)
    store = MemoryStore()
    directory = InMemoryWalletDirectory()
    if bound is not None:
        directory.bind(WalletBinding(CUSTOMER, "privy-wallet-1", bound))
    backend = FakeBackend()
    counter = iter(range(1, 10_000))
    stub = settlement if settlement is not None else StubSettlement()
    billing = SolanaBilling(
        config=config,
        store=store,
        wallets=directory,
        rpc_url="https://rpc.example",
        network="mainnet",
        backend_for=lambda _binding: backend,
        sol_pricer=fixed_pricer(),
        settlement=stub,  # type: ignore[arg-type]
        rpc_call=chain,
        sleep=lambda _s: None,
        clock=lambda: "2026-10-08T12:00:00Z",
        new_id=lambda: f"id{next(counter):04d}",
        rebate_index=rebate_index if rebate_index is not None else MemoryRebateIndex(),
    )
    return Rig(billing, store, chain, backend, stub)


def _quote(store: MemoryStore, *, total: int = AMOUNT, quote_id: str = "q1") -> Quote:
    quote = Quote(
        quote_id=quote_id,
        plan_id="p1",
        customer=CUSTOMER,
        upstream_max_cents=1,
        upstream_max_usdc_micro=10_000,
        gecko_fee_usdc_micro=total - 10_000,
        discount_usdc_micro=0,
        total_usdc_micro=total,
        max_calls=1,
        price_from="pinned",
        created_at="2026-10-08T12:00:00Z",
        expires_at="2026-10-08T12:15:00Z",
    )
    store.save_quote(quote)
    return quote


def _payment(
    *,
    pay_with: str = "USDC",
    amount: int = AMOUNT,
    payer: str = PAYER,
    run_id: str = "20261008-120000-aaaaaa",
    quote_id: str = "q1",
    discount: int = 0,
    payee: str | None = None,
) -> Payment:
    return Payment(
        customer=CUSTOMER,
        run_id=run_id,
        quote_id=quote_id,
        amount_usdc_micro=amount,
        discount_usdc_micro=discount,
        pay_with=pay_with,  # type: ignore[arg-type]
        payer=payer,
        payee_usdc_ata=payee or usdc_ata(FEE_WALLET),
    )


def _sent_instructions(raw_b64: str) -> list[tuple[str, bytes]]:
    decoded = decode_message(raw_b64)
    return [(ix.program_id, ix.discriminator) for ix in decoded.instructions]


# --------------------------------------------------------------------------------------
# Paying in USDC
# --------------------------------------------------------------------------------------


def test_usdc_payment_lands_and_is_read_back_before_it_counts() -> None:
    rig = _rig()
    _quote(rig.store)

    entry = rig.billing.capture(_payment())

    assert entry.status == "landed"
    assert entry.signature == PAY_SIG and entry.slot == SLOT
    assert entry.send_label == "public-rpc" and entry.rebate_requested is False
    assert rig.backend.asked == 1
    # The read-back happened AFTER the confirmation, over RPC.
    assert rig.chain.calls.index("getTransaction") > rig.chain.calls.index(
        "getSignatureStatuses"
    )
    [sent] = rig.chain.sent
    [(program, data)] = _sent_instructions(sent)
    assert program == TOKEN_PROGRAM_ID and data[0] == 12  # TransferChecked
    message = decode_message(sent)
    assert message.fee_payer == PAYER
    assert usdc_ata(FEE_WALLET) in message.writable_accounts
    [held] = rig.store.ledger(CUSTOMER)
    assert held == entry


def test_the_transfer_moves_exactly_the_quote_at_six_decimals() -> None:
    build = build_payment(
        payer=PAYER, fee_wallet=FEE_WALLET, amount_usdc_micro=AMOUNT, pay_with="USDC"
    )
    tx = Transaction.from_bytes(base64.b64decode(build.built.tx))
    [ix] = tx.message.instructions
    assert ix.data[0] == 12
    assert int.from_bytes(ix.data[1:9], "little") == AMOUNT
    assert ix.data[9] == 6


def test_a_payer_that_is_not_the_bound_wallet_is_refused_before_anything_is_built() -> (
    None
):
    rig = _rig()
    _quote(rig.store)

    with pytest.raises(BillingError, match="bound wallet"):
        rig.billing.capture(_payment(payer=STRANGER))

    assert rig.backend.asked == 0 and rig.chain.sent == []
    assert rig.store.ledger(CUSTOMER) == []


def test_no_bound_wallet_means_no_payment() -> None:
    rig = _rig(bound=None)
    _quote(rig.store)
    with pytest.raises(BillingError, match="no wallet"):
        rig.billing.capture(_payment())
    assert rig.backend.asked == 0


def test_a_payee_other_than_the_fee_wallet_is_refused() -> None:
    rig = _rig()
    _quote(rig.store)
    with pytest.raises(BillingError, match="payee"):
        rig.billing.capture(_payment(payee=usdc_ata(STRANGER)))
    assert rig.backend.asked == 0


def test_an_amount_above_the_confirmed_quote_is_refused() -> None:
    rig = _rig()
    _quote(rig.store, total=AMOUNT)
    with pytest.raises(BillingError, match="above the confirmed quote"):
        rig.billing.capture(_payment(amount=AMOUNT + 1))
    assert rig.backend.asked == 0


def test_a_payment_without_a_stored_quote_is_refused() -> None:
    rig = _rig()
    with pytest.raises(BillingError, match="no confirmed quote"):
        rig.billing.capture(_payment())


def test_a_quote_the_discount_covers_moves_nothing_and_spends_the_discount() -> None:
    rig = _rig()
    _quote(rig.store, total=0)

    entry = rig.billing.capture(_payment(amount=0, discount=500))

    assert entry.status == "not_charged" and entry.signature is None
    assert rig.chain.calls == [] and rig.backend.asked == 0
    # Charged before the run, so the discount is spent now, once.
    kinds = sorted((e.kind, e.amount_usdc_micro) for e in rig.store.ledger(CUSTOMER))
    assert kinds == [("charge", 0), ("discount", 500)]


def test_a_broadcast_not_yet_visible_stays_pending_and_is_rechecked_not_resent() -> (
    None
):
    rig = _rig(visible=False)
    _quote(rig.store)

    entry = rig.billing.capture(_payment())

    assert entry.status == "pending" and entry.signature == PAY_SIG
    assert "never charged again" in entry.note
    [held] = rig.store.ledger(CUSTOMER)
    assert (held.status, held.signature) == ("pending", PAY_SIG)
    assert rig.billing.recheck(held) == held  # still not visible: unchanged

    rig.chain.visible = True
    settled = rig.billing.recheck(held)

    assert settled.status == "landed" and settled.signature == PAY_SIG
    assert rig.backend.asked == 1 and len(rig.chain.sent) == 1
    [stored] = rig.store.ledger(CUSTOMER)
    assert stored.status == "landed"
    # A settled entry is not looked up again.
    calls = len(rig.chain.calls)
    assert rig.billing.recheck(stored) == stored and len(rig.chain.calls) == calls


@pytest.mark.parametrize(
    "error",
    [
        RpcError("getSignatureStatuses: 429 Too Many Requests"),
        OSError("connection reset by peer"),
    ],
)
def test_a_poll_error_after_the_broadcast_keeps_the_signature(error: Exception) -> None:
    # The money moved at sendTransaction. Whatever the confirmation poll raises after
    # that, the ledger must hold the signature, or nothing can ever look the payment up.
    rig = _rig(visible=False, status_error=error)
    _quote(rig.store)

    entry = rig.billing.capture(_payment())

    assert len(rig.chain.sent) == 1
    assert (entry.status, entry.signature) == ("pending", PAY_SIG)
    [held] = rig.store.ledger(CUSTOMER)
    assert (held.status, held.signature) == ("pending", PAY_SIG)
    rig.chain.visible = True
    assert rig.billing.recheck(held).status == "landed"
    assert len(rig.chain.sent) == 1 and rig.backend.asked == 1


def test_a_poll_error_on_a_visible_payment_settles_it_from_the_chain() -> None:
    rig = _rig(status_error=RpcError("getSignatureStatuses: 429"))
    _quote(rig.store)
    entry = rig.billing.capture(_payment())
    assert (entry.status, entry.signature) == ("landed", PAY_SIG)


def test_a_recheck_that_finds_a_failed_transaction_marks_it_failed() -> None:
    rig = _rig(visible=False, landed_err={"InstructionError": [0, "Custom"]})
    _quote(rig.store)
    held = rig.billing.capture(_payment())
    rig.chain.visible = True
    assert rig.billing.recheck(held).status == "failed"
    assert rig.backend.asked == 1


def test_a_second_capture_for_the_same_run_is_refused_before_a_second_signature() -> (
    None
):
    rig = _rig()
    _quote(rig.store)
    rig.billing.capture(_payment())

    with pytest.raises(DuplicateChargeError):
        rig.billing.capture(_payment())

    assert rig.backend.asked == 1 and len(rig.chain.sent) == 1


def test_a_simulation_that_reverts_is_recorded_failed_and_never_signed() -> None:
    rig = _rig(sim_err={"InstructionError": [0, {"Custom": 1}]})
    _quote(rig.store)

    entry = rig.billing.capture(_payment())

    assert entry.status == "failed" and entry.signature is None
    assert "receipt-failed" in entry.note
    assert rig.backend.asked == 0 and rig.chain.sent == []


def test_landed_but_short_is_failed_with_the_signature_kept_for_reconciliation() -> (
    None
):
    rig = _rig(landed_received=AMOUNT - 1)
    _quote(rig.store)

    entry = rig.billing.capture(_payment())

    assert entry.status == "failed"
    assert entry.signature == PAY_SIG
    assert "reconcile" in entry.note


def test_a_transaction_that_landed_and_failed_on_chain_is_failed() -> None:
    rig = _rig(landed_err={"InstructionError": [0, "Custom"]})
    _quote(rig.store)
    entry = rig.billing.capture(_payment())
    assert entry.status == "failed" and entry.signature == PAY_SIG


def test_a_spend_beyond_the_payment_policy_is_refused_by_the_gate() -> None:
    """The fake node reports the payer losing far more SOL than a fee: the gate the
    signer holds refuses, so the backend is never asked."""
    rig = _rig()
    _quote(rig.store)
    rig.chain.payer_lamports = 10**12

    original = rig.chain.__call__

    def lying(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        answer = original(url, method, params)
        if method == "simulateTransaction":
            answer["result"]["value"]["accounts"][0]["lamports"] = 0
        return answer

    rig.billing.rpc_call = lying
    entry = rig.billing.capture(_payment())

    assert entry.status == "failed" and "spend-refused" in entry.note
    assert rig.backend.asked == 0


@pytest.mark.parametrize("pay_with", ["USDC", "SOL"])
def test_a_build_that_pays_anyone_but_the_treasury_is_refused_before_signing(
    monkeypatch: pytest.MonkeyPatch, pay_with: str
) -> None:
    """Defence in depth: the builder is swapped for one that pays a stranger. Its own
    payment policy allows that (it is derived from the build), so only the wallet policy,
    derived from config, stands between those bytes and the signer."""
    import gecko.anyquery.solana_billing as solana_billing

    def hijacked(**kwargs: Any) -> Any:
        key = "fee_wallet" if pay_with == "USDC" else "settlement_wallet"
        return build_payment(**{**kwargs, key: STRANGER})

    monkeypatch.setattr(solana_billing, "build_payment", hijacked)
    rig = _rig(pay_with=pay_with)
    _quote(rig.store)

    entry = rig.billing.capture(_payment(pay_with=pay_with))

    assert entry.status == "failed"
    assert "destination-not-allowlisted" in entry.note
    assert rig.backend.asked == 0 and rig.chain.sent == []


# --------------------------------------------------------------------------------------
# Paying in SOL (option B): one transfer to the settlement wallet, final when it lands
# --------------------------------------------------------------------------------------


def test_a_sol_payment_is_one_transfer_to_the_settlement_wallet_and_final() -> None:
    rig = _rig(pay_with="SOL")
    _quote(rig.store)

    entry = rig.billing.capture(_payment(pay_with="SOL"))

    assert entry.status == "landed" and entry.signature == PAY_SIG
    assert entry.amount_lamports == SOL_IN
    # The price the quote was converted at is recorded on the charge itself.
    assert entry.sol_usdc_rate_micro == RATE_MICRO
    # A plain transfer has no backrun: public endpoint, no rebate asked.
    assert entry.send_label == "public-rpc" and entry.rebate_requested is False
    assert "final" in entry.note
    [sent] = rig.chain.sent
    [(program, _data)] = _sent_instructions(sent)
    assert program == SYSTEM_PROGRAM_ID
    message = decode_message(sent)
    assert message.fee_payer == PAYER
    assert SETTLEMENT in message.writable_accounts
    # No wrap, no swap, no token account in the user's transaction.
    assert usdc_ata(PAYER) not in message.writable_accounts
    assert wsol_ata(PAYER) not in message.writable_accounts
    assert FEE_WALLET not in message.writable_accounts


def test_a_landed_sol_payment_is_handed_to_the_settlement_once() -> None:
    rig = _rig(pay_with="SOL")
    _quote(rig.store)

    entry = rig.billing.capture(_payment(pay_with="SOL"))

    assert rig.settlement.asked == [entry]


def test_a_settlement_that_raises_never_touches_the_charge(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    rig = _rig(
        pay_with="SOL", settlement=StubSettlement(raises=RpcError("pool read: 429"))
    )
    _quote(rig.store)

    entry = rig.billing.capture(_payment(pay_with="SOL"))

    assert entry.status == "landed"
    [held] = rig.store.ledger(CUSTOMER)
    assert held == entry
    assert rig.backend.asked == 1 and len(rig.chain.sent) == 1
    assert any("settlement of run" in r.getMessage() for r in caplog.records)


def test_a_usdc_payment_is_never_settled() -> None:
    rig = _rig()
    _quote(rig.store)
    rig.billing.capture(_payment())
    assert rig.settlement.asked == []


def test_a_sol_charge_seen_on_recheck_is_settled_then() -> None:
    rig = _rig(pay_with="SOL", visible=False)
    _quote(rig.store)
    held = rig.billing.capture(_payment(pay_with="SOL"))
    assert held.status == "pending" and rig.settlement.asked == []

    rig.chain.visible = True
    settled = rig.billing.recheck(held)

    assert settled.status == "landed"
    assert [c.signature for c in rig.settlement.asked] == [PAY_SIG]


def test_a_sol_payment_short_of_its_lamports_is_failed_for_reconciliation() -> None:
    rig = _rig(pay_with="SOL", landed_lamports=SOL_IN - 1)
    _quote(rig.store)

    entry = rig.billing.capture(_payment(pay_with="SOL"))

    assert entry.status == "failed" and entry.signature == PAY_SIG
    assert "reconcile" in entry.note
    assert rig.settlement.asked == []


def test_sol_without_a_settlement_wallet_is_refused_before_anything_is_signed() -> None:
    config = replace(CONFIG, settlement_wallet=None, settlement_wallet_id=None)
    rig = _rig(pay_with="SOL", config=config)
    _quote(rig.store)

    with pytest.raises(BillingError, match="settlement wallet"):
        rig.billing.capture(_payment(pay_with="SOL"))

    assert rig.backend.asked == 0 and rig.store.ledger(CUSTOMER) == []


def test_settling_by_hand_retries_every_outstanding_charge() -> None:
    rig = _rig(pay_with="SOL")
    _quote(rig.store)
    rig.billing.settlement = None
    charge = rig.billing.capture(_payment(pay_with="SOL"))
    with pytest.raises(BillingError, match="no settlement"):
        rig.billing.settle(charge)
    stub = StubSettlement()
    rig.billing.settlement = stub  # type: ignore[assignment]
    assert rig.billing.settle(charge) == charge
    assert stub.asked == [charge]


# --------------------------------------------------------------------------------------
# Rebates: matched to the SETTLEMENT's slot, credited once
# --------------------------------------------------------------------------------------


def _settlement_entry(
    customer: str = CUSTOMER,
    *,
    signature: str = SETTLE_SIG,
    slot: int = SLOT,
    run_id: str = "20261008-120000-aaaaaa",
) -> LedgerEntry:
    """A landed settlement of ``customer``'s SOL charge, sent with a rebate asked."""
    return LedgerEntry(
        entry_id=f"settle-{customer}-{signature[:4]}",
        customer=customer,
        kind="settlement",
        run_id=run_id,
        quote_id="q1",
        amount_usdc_micro=AMOUNT,
        status="landed",
        pay_with="SOL",
        signature=signature,
        slot=slot,
        send_label="helius-mainnet",
        rebate_requested=True,
        rebate_lamports=None,
        split_user_pct=None,
        sol_usdc_rate_micro=USDC_OUT * 1_000_000_000 // SOL_IN,
        created_at="2026-10-08T12:00:01Z",
        note="",
        amount_lamports=SOL_IN - 5_000,
    )


def _settled_rig(**kw: Any) -> Rig:
    rig = _rig(pay_with="SOL", **kw)
    _quote(rig.store)
    rig.billing.capture(_payment(pay_with="SOL"))
    rig.store.append_ledger(_settlement_entry())
    return rig


def test_an_observed_rebate_is_credited_once_at_the_settlements_rate() -> None:
    rig = _settled_rig()
    settlement = _settlement_entry()

    [credit] = rig.billing.observe_rebates(CUSTOMER)

    assert credit.kind == "rebate_credit" and credit.run_id == settlement.run_id
    assert credit.rebate_lamports == REBATE_LAMPORTS
    assert credit.split_user_pct == 50
    assert credit.signature == REBATE_SIG
    assert credit.amount_usdc_micro == rebate_credit_micro(
        REBATE_LAMPORTS, 50, settlement.sol_usdc_rate_micro or 0
    )
    assert "settlement" in credit.note
    assert rig.billing.observe_rebates(CUSTOMER) == []
    assert rig.billing.rebate_index is not None
    # Attributed to the settlement swap, not to the user's payment.
    row = rig.billing.rebate_index.row(REBATE_SIG)
    assert row == RebateRow(REBATE_SIG, SLOT, REBATE_LAMPORTS, SETTLE_SIG)


def test_a_rebate_in_the_payments_slot_alone_is_not_credited() -> None:
    """The payment is a plain transfer: nothing backruns it. Before its settlement
    lands, a relay inflow in the payment's slot is not this user's rebate."""
    rig = _rig(pay_with="SOL")
    _quote(rig.store)
    rig.billing.capture(_payment(pay_with="SOL"))

    assert rig.billing.observe_rebates(CUSTOMER) == []
    assert all(e.kind != "rebate_credit" for e in rig.store.ledger(CUSTOMER))


def test_a_rebate_follows_the_settlement_slot_not_the_payment_slot() -> None:
    rig = _rig(pay_with="SOL")
    _quote(rig.store)
    rig.billing.capture(_payment(pay_with="SOL"))  # lands in SLOT
    rig.store.append_ledger(_settlement_entry(slot=SLOT + 7))

    assert rig.billing.observe_rebates(CUSTOMER) == []
    index = rig.billing.rebate_index
    assert index is not None
    # The inflow in the payment's slot is recorded, unattributed, for a human.
    assert index.row(REBATE_SIG) == RebateRow(REBATE_SIG, SLOT, REBATE_LAMPORTS, None)


@pytest.mark.parametrize("first", ["alice", "bob"])
def test_one_rebate_is_credited_once_across_customers(first: str) -> None:
    """Review finding 5: two customers' settlements in one slot; the rebate is one
    signature, and the global index lets exactly one of them be credited for it."""
    index = MemoryRebateIndex()
    alice = _settled_rig(rebate_index=index)
    bob = _rig(pay_with="SOL", rebate_index=index)
    bob_settle = "7" + "B" * 86
    bob.store.append_ledger(_settlement_entry("acct_bob", signature=bob_settle))

    order = [(alice, CUSTOMER), (bob, "acct_bob")]
    if first == "bob":
        order.reverse()
    credits = [rig.billing.observe_rebates(who) for rig, who in order]

    assert [len(c) for c in credits] == [1, 0]
    winner = SETTLE_SIG if first == "alice" else bob_settle
    assert index.row(REBATE_SIG) == RebateRow(REBATE_SIG, SLOT, REBATE_LAMPORTS, winner)
    all_credits = [
        e
        for store, who in ((alice.store, CUSTOMER), (bob.store, "acct_bob"))
        for e in store.ledger(who)
        if e.kind == "rebate_credit"
    ]
    assert [e.signature for e in all_credits] == [REBATE_SIG]


def test_an_inflow_outside_every_settlement_slot_is_recorded_unattributed() -> None:
    rig = _settled_rig(other_inflow_lamports=7_000)

    [credit] = rig.billing.observe_rebates(CUSTOMER)

    assert credit.signature == REBATE_SIG
    index = rig.billing.rebate_index
    assert index is not None
    assert index.row(OTHER_INFLOW_SIG) == RebateRow(
        OTHER_INFLOW_SIG, SLOT - 50, 7_000, None
    )


def test_an_unattributed_inflow_is_claimed_when_its_settlement_is_seen() -> None:
    index = MemoryRebateIndex()
    index.record_unattributed(REBATE_SIG, slot=SLOT, lamports=REBATE_LAMPORTS)
    rig = _settled_rig(rebate_index=index)

    [credit] = rig.billing.observe_rebates(CUSTOMER)

    assert credit.signature == REBATE_SIG
    assert index.row(REBATE_SIG) == RebateRow(
        REBATE_SIG, SLOT, REBATE_LAMPORTS, SETTLE_SIG
    )


def test_an_inflow_to_the_fee_wallet_is_not_a_rebate_any_more() -> None:
    """The fee wallet was the rebate destination before the ruling; it is not now."""
    rig = _settled_rig(rebate_to=FEE_WALLET)
    assert rig.billing.observe_rebates(CUSTOMER) == []


def test_observing_rebates_reminds_the_founder_to_sweep_a_hot_relay(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    rig = _settled_rig(relay_lamports=250_000_000)

    rig.billing.observe_rebates(CUSTOMER)

    assert any("sweep" in r.getMessage() for r in caplog.records)
    assert rig.billing.relay_balance().status == "sweep"


def test_solana_billing_needs_a_relay_fee_payer() -> None:
    with pytest.raises(BillingError, match="relay"):
        replace(_rig().billing, config=replace(CONFIG, relay_fee_payer=None))


def test_observing_rebates_without_an_index_refuses_rather_than_double_credit() -> None:
    rig = _settled_rig()
    rig.billing.rebate_index = None
    with pytest.raises(BillingError, match="rebate index"):
        rig.billing.observe_rebates(CUSTOMER)


def test_no_rebate_in_the_slot_means_no_credit() -> None:
    rig = _settled_rig(rebate_lamports=0)
    assert rig.billing.observe_rebates(CUSTOMER) == []


def test_a_usdc_payment_never_looks_for_a_rebate() -> None:
    rig = _rig()
    _quote(rig.store)
    rig.billing.capture(_payment())
    calls_before = list(rig.chain.calls)
    assert rig.billing.observe_rebates(CUSTOMER) == []
    assert rig.chain.calls == calls_before


def _plan() -> QueryPlan:
    source = PlannedSource(
        rank=0,
        candidate_id="orthogonal:x:GET:/y",
        catalog="orthogonal",
        api="x",
        path="/y",
        price_cents=10,
        field_path="a.b",
        inputs=(),
        storage="allowed",
    )
    return QueryPlan(
        plan_id="p2",
        customer=CUSTOMER,
        query="q",
        subjects=("acme",),
        fields=(PlannedField("funding", "m", "text", (source,)),),
        unplanned=(),
        planner="fake",
        planner_model=None,
        catalog_sha="x",
        created_at="2026-10-08T12:00:00Z",
    )


def test_the_users_share_is_a_discount_on_the_next_run_and_is_spent_once() -> None:
    rig = _settled_rig()
    [credit] = rig.billing.observe_rebates(CUSTOMER)

    quote = rig.billing.quote(_plan(), now="2026-10-08T13:00:00Z")
    rig.store.save_quote(quote)
    assert quote.discount_usdc_micro == min(credit.amount_usdc_micro, 100_000)

    rig.chain.amount = quote.total_usdc_micro
    rig.chain.landed_received = quote.total_usdc_micro
    rig.chain.pay_with = "USDC"
    rig.billing.capture(
        _payment(
            amount=quote.total_usdc_micro,
            discount=quote.discount_usdc_micro,
            run_id="20261008-130000-bbbbbb",
            quote_id=quote.quote_id,
        )
    )
    kinds = sorted(e.kind for e in rig.store.ledger(CUSTOMER))
    assert kinds == ["charge", "charge", "discount", "rebate_credit", "settlement"]
    again = rig.billing.quote(_plan(), now="2026-10-08T14:00:00Z")
    assert (
        again.discount_usdc_micro
        == credit.amount_usdc_micro - quote.discount_usdc_micro
    )


# --------------------------------------------------------------------------------------
# Preflight and rehearsal: reads only, no signature
# --------------------------------------------------------------------------------------


def test_preflight_passes_a_funded_bound_wallet() -> None:
    rig = _rig()
    quote = _quote(rig.store)
    verdict = rig.billing.preflight(CUSTOMER, quote, "USDC")
    assert verdict.ok and verdict.payer == PAYER


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"bound": None}, "no wallet bound"),
        ({"payer_usdc": AMOUNT - 1}, "needs 0.02 USDC"),
        ({"fee_ata_exists": False}, "fee wallet has no USDC account"),
        ({"payer_lamports": 10}, "no SOL for the fee"),
    ],
)
def test_preflight_refuses_with_a_reason(kw: dict[str, Any], reason: str) -> None:
    rig = _rig(**kw)
    quote = _quote(rig.store)
    verdict = rig.billing.preflight(CUSTOMER, quote, "USDC")
    assert not verdict.ok and reason in verdict.reason
    assert rig.backend.asked == 0


def test_preflight_in_sol_counts_the_converted_quote_and_the_fee() -> None:
    rig = _rig(pay_with="SOL", payer_lamports=SOL_IN)
    quote = _quote(rig.store)
    verdict = rig.billing.preflight(CUSTOMER, quote, "SOL")
    assert not verdict.ok and "SOL" in verdict.reason

    rig.chain.payer_lamports = SOL_IN + 100_000
    verdict = rig.billing.preflight(CUSTOMER, quote, "SOL")
    assert verdict.ok and f"{SOL_IN / 1e9:.6f} SOL" in verdict.reason


def test_preflight_in_sol_needs_a_funded_settlement_wallet() -> None:
    rig = _rig(pay_with="SOL", settlement_exists=False)
    quote = _quote(rig.store)
    verdict = rig.billing.preflight(CUSTOMER, quote, "SOL")
    assert not verdict.ok and "settlement wallet" in verdict.reason


def test_preflight_in_sol_without_a_settlement_wallet_says_so() -> None:
    config = replace(CONFIG, settlement_wallet=None, settlement_wallet_id=None)
    rig = _rig(pay_with="SOL", config=config)
    quote = _quote(rig.store)
    verdict = rig.billing.preflight(CUSTOMER, quote, "SOL")
    assert not verdict.ok and "not configured" in verdict.reason


def test_rehearsal_simulates_without_signing_or_sending() -> None:
    rig = _rig(pay_with="SOL")
    _quote(rig.store)

    receipt = rig.billing.rehearse(_payment(pay_with="SOL"))

    assert receipt.status == "pass"
    [params] = rig.chain.simulations
    assert params[1]["sigVerify"] is False
    assert params[1]["replaceRecentBlockhash"] is True
    assert rig.backend.asked == 0 and rig.chain.sent == []
    assert rig.store.ledger(CUSTOMER) == []


def test_solana_billing_refuses_a_fake_config() -> None:
    with pytest.raises(BillingError):
        replace(_rig().billing, config=AnyQueryConfig())
