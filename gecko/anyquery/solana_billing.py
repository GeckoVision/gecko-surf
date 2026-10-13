"""``SolanaBilling``: the :class:`~gecko.anyquery.billing.Billing` Protocol on chain.

Pay per run, no balance, no custody. The user's Privy wallet pays one run's confirmed
price in one transaction it signs itself (``signTransaction`` via
:class:`gecko.privy_backend.PrivyBackend`; Gecko never holds the key and never sends
through Privy). A ``landed`` ledger entry is written only after the chain, read back over
RPC, shows the money arrived:

* USDC: one ``TransferChecked`` to the cold treasury; its USDC account must be up by
  exactly the amount.
* SOL (option B, founder ruling 9 Oct): one System ``Transfer`` to Gecko's SETTLEMENT
  wallet of the quote converted at the pinned pool's price plus a stated margin
  (:mod:`gecko.anyquery.payment_tx`); the settlement wallet's lamports must be up by
  exactly that. The charge is FINAL when it lands. Gecko then settles its own revenue
  (:mod:`gecko.anyquery.settlement`): swap on the one pool, forward USDC to the treasury,
  through Helius with ``rebate-address`` = the relay fee payer. A settlement that fails
  is reported and retried; it never touches the user's wallet again.

THE PATH IS THE ONE EVERY GECKO SIGNATURE TAKES. :func:`gecko.autonomous_purchase.run_purchase`
stamps a fresh blockhash, simulates the exact bytes, requires an ``exact`` binding, asks
the spend gate (the wallet policy first, then a policy authored for THIS payment only:
:func:`payment_policy`), signs through :class:`gecko.signer.TransactionSigner`, sends and
confirms. Payments go to the public endpoint: a plain transfer has no backrun.

A BROADCAST THAT IS NOT YET VISIBLE is not an error and not a failure: the charge stays
``pending`` WITH its signature, and :meth:`SolanaBilling.recheck` reads that signature
back later. Nothing ever signs a second payment for the run (the ledger holds one charge
per run, written before signing).

REBATES are credited only when observed: a transaction in a SETTLEMENT's slot that raised
the RELAY's lamports (the swap is what a searcher backruns, so the payment's slot no
longer counts). Each rebate signature is claimed in the global
:class:`~gecko.anyquery.rebates.RebateIndex` before it is credited, so it is credited at
most once across all customers; relay inflows in no settlement's slot are recorded there
unattributed. The user's share (``rebate_split_user_pct``) becomes a ``rebate_credit``
entry, priced at the settlement's own realised rate, which the next quote subtracts as a
discount (:func:`gecko.anyquery.billing.available_discount`). Each observation also checks
the relay's hot balance and logs a top-up or sweep reminder for the founder.

Nothing here reads a secret: the Privy backend and the Helius transport resolve their own
from the OS keychain, per call.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ..autonomous_purchase import (
    USDC_MINT,
    PurchasePlan,
    PurchaseRefused,
    PurchaseTransportError,
    run_purchase,
)
from ..credentials import ChainResolver
from ..networks import Network
from ..rpc import RpcCall, RpcError, default_rpc_call
from ..send_endpoint import PublicSend, SendTransport
from ..signer import (
    EXTERNAL_SIGNER_PROFILE_NAME,
    SignerProfile,
    SigningBackend,
    TransactionSigner,
)
from ..simulate import BuiltTx, Receipt, simulate
from ..spend_policy import InMemorySpendLedger
from ..wallet_binding import WalletBinding, WalletBindingError, WalletDirectory
from .billing import (
    LEDGER_SCAN_LIMIT,
    BillingError,
    Preflight,
    available_discount,
    build_quote,
    rebate_credit_micro,
    spend_discount,
)
from .chain_read import lamport_delta, token_delta
from .config import AnyQueryConfig
from .rebates import (
    RebateIndex,
    RebateIndexError,
    RebateRow,
    RelayBalance,
    check_relay_balance,
)
from .payment_tx import PaymentBuild, SolPricer, build_payment, usdc_ata
from .settlement import Settlement
from .store import DuplicateChargeError, Store, StoreError
from .types import LedgerEntry, Payment, PayWith, QueryPlan, Quote
from .wallet_policy import WalletPolicyGate, wallet_policy_from_config

__all__ = [
    "MIN_FEE_LAMPORTS",
    "REBATE_LOOKBACK",
    "SolanaBilling",
    "privy_backend_for",
]

logger = logging.getLogger(__name__)

#: What a payer must hold in SOL beyond the payment itself, for the network fee.
MIN_FEE_LAMPORTS = 100_000
#: How many of the relay's recent signatures one rebate check reads.
REBATE_LOOKBACK = 100
#: How many recent landed settlements one ``observe_rebates`` call re-checks.
_REBATE_SETTLEMENTS = 20
_NOTE_CAP = 300


def _new_id() -> str:
    return secrets.token_hex(8)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _note(text: str) -> str:
    return text if len(text) <= _NOTE_CAP else text[: _NOTE_CAP - 3] + "..."


def privy_backend_for(
    network: Network, *, resolver: ChainResolver | None = None
) -> Callable[[WalletBinding], SigningBackend]:
    """The live signer: a Privy backend for the BOUND wallet id. The app id and secret
    come from ``resolver`` (default: the keychain alone), per payment."""

    def make(binding: WalletBinding) -> SigningBackend:
        from ..privy_backend import from_keychain

        return from_keychain(
            network=network, wallet_id=binding.wallet_id, resolver=resolver
        )

    return make


@dataclass
class SolanaBilling:
    """The live :class:`~gecko.anyquery.billing.Billing`. Every seam is injected."""

    config: AnyQueryConfig
    store: Store
    #: Who pays. Read from the directory, never from a tool argument or a Payment field.
    wallets: WalletDirectory
    rpc_url: str
    #: ASSERTED by the operator, never inferred from ``rpc_url``.
    network: Network
    backend_for: Callable[[WalletBinding], SigningBackend]
    #: Prices a quote in SOL on the pinned pool. ``None``: paying in SOL is off.
    sol_pricer: SolPricer | None = None
    #: Settles a landed SOL charge from Gecko's settlement wallet. ``None``: SOL charges
    #: land and wait in the settlement wallet until ``settle`` is run by hand.
    settlement: Settlement | None = None
    rpc_call: RpcCall = default_rpc_call
    #: Where payments are sent. ``None``: the public endpoint (a transfer has no backrun).
    send: SendTransport | None = None
    priority_fee_microlamports: int | None = None
    confirm_timeout_seconds: float = 60.0
    poll_interval_seconds: float = 1.0
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    clock: Callable[[], str] = _now
    new_id: Callable[[], str] = _new_id
    #: The global rebate index (outside every customer's database). ``None`` makes
    #: :meth:`observe_rebates` refuse: without it a rebate could be credited twice.
    rebate_index: RebateIndex | None = None
    #: Relay transactions already read and found not to be inflows, this process.
    _not_inflows: set[str] = field(init=False, repr=False, default_factory=set)

    def __post_init__(self) -> None:
        if self.config.billing != "solana":
            raise BillingError("SolanaBilling needs GECKO_ANYQUERY_BILLING=solana")
        if not self.config.fee_wallet or self.config.rebate_split_user_pct is None:
            raise BillingError("SolanaBilling needs a fee wallet and a rebate split")
        if not self.config.relay_fee_payer:
            raise BillingError("SolanaBilling needs the relay fee payer address")
        if self.send is None:
            self.send = PublicSend(self.rpc_url, self.rpc_call)

    @property
    def fee_wallet(self) -> str:
        assert self.config.fee_wallet is not None  # checked in __post_init__
        return self.config.fee_wallet

    @property
    def relay(self) -> str:
        assert self.config.relay_fee_payer is not None  # checked in __post_init__
        return self.config.relay_fee_payer

    # -- the Protocol ---------------------------------------------------------------

    def quote(self, plan: QueryPlan, *, now: str) -> Quote:
        held = self.store.ledger(plan.customer, limit=LEDGER_SCAN_LIMIT)
        return build_quote(
            plan,
            self.config,
            available=available_discount(held),
            quote_id=self.new_id(),
            now=now,
        )

    def preflight(self, customer: str, quote: Quote, pay_with: PayWith) -> Preflight:
        """Can the bound wallet pay this quote right now? Reads only; signs nothing."""
        try:
            binding = self.wallets.wallet_for(customer)
        except WalletBindingError:
            return Preflight(False, None, "the wallet directory could not be read")
        if binding is None:
            return Preflight(False, None, "no wallet bound; wallet creation is off")
        payer = binding.pubkey
        if quote.customer != customer:
            return Preflight(False, payer, "this quote belongs to another account")
        try:
            if not self._exists(usdc_ata(self.fee_wallet)):
                return Preflight(
                    False,
                    payer,
                    "the fee wallet has no USDC account yet; nothing is due",
                )
            lamports = self._lamports(payer)
            if pay_with == "USDC":
                held = self._usdc(payer)
                if held < quote.total_usdc_micro:
                    return Preflight(
                        False,
                        payer,
                        f"needs {quote.total_usdc_micro / 1e6:.2f} USDC, "
                        f"has {held / 1e6:.2f}",
                    )
                if lamports < MIN_FEE_LAMPORTS:
                    return Preflight(
                        False, payer, "the wallet holds no SOL for the fee"
                    )
                return Preflight(True, payer, "the wallet can cover the quote")
            if quote.total_usdc_micro == 0:
                return Preflight(True, payer, "nothing is due")
            settlement = self.config.settlement_wallet
            if self.sol_pricer is None or settlement is None:
                return Preflight(False, payer, "paying in SOL is not configured")
            if not self._exists(settlement):
                return Preflight(
                    False, payer, "the settlement wallet is not funded yet; pay in USDC"
                )
            sol = self.sol_pricer(quote.total_usdc_micro)
        except (RpcError, BillingError) as exc:
            return Preflight(False, payer, _note(f"could not check the wallet: {exc}"))
        need = sol.lamports + MIN_FEE_LAMPORTS
        if lamports < need:
            return Preflight(
                False,
                payer,
                f"needs about {need / 1e9:.6f} SOL, has {lamports / 1e9:.6f}",
            )
        return Preflight(
            True,
            payer,
            f"the wallet can cover the quote in SOL ({sol.lamports / 1e9:.6f} SOL)",
        )

    def capture(self, payment: Payment) -> LedgerEntry:
        """Pay ``payment`` on chain and return its settled ledger entry.

        Refuses (raises :class:`BillingError`, nothing signed) when the payer is not the
        bound wallet, the payee is not the fee wallet's USDC account, or the amount is
        above the confirmed quote. A refusal or revert after the pending entry was
        written returns that entry as ``failed``. A broadcast the chain does not show yet
        returns it ``pending`` with its signature, stored, for :meth:`recheck`. A SOL
        charge that landed is then settled (:meth:`settle`); how that goes never changes
        the charge.
        """
        binding = self._check(payment)
        if payment.amount_usdc_micro == 0:
            # The discount covered the whole quote: nothing moves, the discount is spent.
            entry = self._entry(
                payment,
                status="not_charged",
                send_label=None,
                note="the discount covered the quote; nothing to transfer",
            )
            self.store.append_ledger(entry)
            spend_discount(self.store, entry, payment.discount_usdc_micro, self.new_id)
            return entry

        build = self._build(payment, binding)
        send = self._send()
        pending = self._entry(
            payment,
            send_label=send.label,
            amount_lamports=build.sol.lamports if build.sol else None,
            sol_usdc_rate_micro=build.sol.rate_micro if build.sol else None,
        )
        # The duplicate-charge guard: the store refuses a second charge for this run
        # BEFORE anything is signed.
        self.store.append_ledger(pending)
        settled = self._settle(pending, build, binding, send)
        self._record(settled)
        spend_discount(self.store, settled, payment.discount_usdc_micro, self.new_id)
        self._settle_after(settled)
        return settled

    def recheck(self, entry: LedgerEntry) -> LedgerEntry:
        """Read a ``pending`` charge's signature back from the chain. Never builds, signs
        or sends: a payment that may be on chain is looked up, never paid again.

        Returns the entry unchanged while the chain still does not show it, or when it
        has no signature to look up (it was never broadcast, or the process stopped
        between send and record: reconcile by hand). A SOL charge that is now seen to
        have landed is settled.
        """
        if entry.kind != "charge" or entry.status != "pending":
            return entry
        if entry.signature is None:
            return entry
        try:
            result = self._transaction(entry.signature)
        except RpcError:
            return entry
        settled = self._verdict(entry, entry.signature, result)
        if settled.status == "pending":
            return entry
        self._record(settled)
        quote = self.store.quote(entry.customer, entry.quote_id or "")
        discount = quote.discount_usdc_micro if quote is not None else 0
        spend_discount(self.store, settled, discount, self.new_id)
        self._settle_after(settled)
        return settled

    def settle(self, charge: LedgerEntry) -> LedgerEntry:
        """Settle one landed SOL charge (or retry its settlement). Raises
        :class:`BillingError` when no settlement is configured or it cannot start."""
        if self.settlement is None:
            raise BillingError("no settlement wallet is configured")
        return self.settlement.settle(charge)

    def settle_outstanding(self, customer: str) -> list[LedgerEntry]:
        """Retry every landed SOL charge of ``customer`` that has no landed settlement.
        A charge whose settlement cannot start is reported in the log and skipped."""
        if self.settlement is None:
            raise BillingError("no settlement wallet is configured")
        out: list[LedgerEntry] = []
        for charge in self.settlement.outstanding(customer):
            try:
                out.append(self.settlement.settle(charge))
            except BillingError as exc:
                logger.warning(
                    "settlement of run %s not attempted: %s",
                    charge.run_id,
                    _note(str(exc)),
                )
        return out

    def _settle_after(self, charge: LedgerEntry) -> None:
        """Settle a SOL charge that just landed. A failure here is reported and left for
        :meth:`settle_outstanding`; it never reaches the user's charge."""
        if (
            charge.pay_with != "SOL"
            or charge.status != "landed"
            or self.settlement is None
        ):
            return
        try:
            self.settlement.settle(charge)
        except Exception as exc:  # noqa: BLE001 - the charge is final; report, retry later
            logger.warning(
                "settlement of run %s did not run (%s): %s",
                charge.run_id,
                type(exc).__name__,
                _note(str(exc)),
            )

    def _record(self, settled: LedgerEntry) -> None:
        try:
            self.store.update_ledger(settled)
        except StoreError as exc:
            # The chain moved and the record did not: say which transaction to reconcile.
            raise BillingError(
                f"payment {settled.status} on chain (signature {settled.signature}) but "
                f"the ledger refused the update: {exc}"
            ) from None

    def rehearse(self, payment: Payment) -> Receipt:
        """Build and SIMULATE the payment (``sigVerify: false``, blockhash replaced). No
        signature, no send, no ledger row: the founder's dry run."""
        binding = self._check(payment)
        if payment.amount_usdc_micro == 0:
            raise BillingError("a zero payment has nothing to rehearse")
        built = self._build(payment, binding).built
        return simulate(
            {},
            rpc_url=self.rpc_url,
            rpc_call=self.rpc_call,
            build_call=lambda _plan: built,
            replace_blockhash=True,
            network_label=f"rehearsed against {self.network} (read-only, unsigned)",
            network=self.network,
            track=[binding.pubkey],
        )

    def observe_rebates(self, customer: str) -> list[LedgerEntry]:
        """Credit the user's share of any rebate the relay received in the slot of one of
        their settlements. A rebate signature is claimed in the global index first, so it
        is credited at most once across ALL customers; relay inflows in no settlement slot
        of this customer are recorded there unattributed. Idempotent per signature."""
        index = self.rebate_index
        if index is None:
            raise BillingError(
                "no rebate index is configured; refusing to credit rebates that "
                "another customer could also be credited"
            )
        held = self.store.ledger(customer, limit=LEDGER_SCAN_LIMIT)
        credited_runs = {e.run_id for e in held if e.kind == "rebate_credit"}
        seen: set[str] = {e.signature for e in held if e.signature is not None}
        swaps = [
            e
            for e in held
            if e.kind == "settlement"
            and e.status == "landed"
            and e.rebate_requested
            and e.slot is not None
            and e.signature is not None
            and e.run_id not in credited_runs
        ][:_REBATE_SETTLEMENTS]
        if not swaps:
            return []
        by_slot: dict[int, LedgerEntry] = {}
        for swap in swaps:
            assert swap.slot is not None
            by_slot.setdefault(swap.slot, swap)
        ours = {s.signature for s in swaps}
        recent = self._recent_signatures(self.relay)
        written: list[LedgerEntry] = []
        for row in recent:
            signature, slot = row.get("signature"), row.get("slot")
            if (
                not isinstance(signature, str)
                or not isinstance(slot, int)
                or isinstance(slot, bool)
                or row.get("err") is not None
                or signature in seen
                or signature in ours
                or signature in self._not_inflows
            ):
                continue
            matched = by_slot.get(slot)
            known: RebateRow | None = self._index(lambda: index.row(signature))
            if known is not None and (
                known.payment_signature is not None or matched is None
            ):
                continue  # already credited somewhere, or already noted unattributed
            lamports = self._relay_gain(signature)
            if lamports <= 0:
                self._not_inflows.add(signature)
                continue
            if matched is None:
                self._index(
                    lambda: index.record_unattributed(
                        signature, slot=slot, lamports=lamports
                    )
                )
                continue
            credit = self._credit(index, customer, matched, signature, slot, lamports)
            if credit is not None:
                seen.add(signature)
                written.append(credit)
        try:
            self.relay_balance()
        except RpcError as exc:
            # A reminder that could not be read must not lose the credits just written.
            logger.warning("relay balance not checked: %s", _note(str(exc)))
        return written

    def relay_balance(self) -> RelayBalance:
        """Read the relay's lamports and log a top-up or sweep reminder for the founder.
        A reminder only: nothing here signs or moves money."""
        return check_relay_balance(self._lamports(self.relay), self.config)

    def entries(self, customer: str, *, limit: int = 50) -> list[LedgerEntry]:
        return self.store.ledger(customer, limit=limit)

    # -- internals ------------------------------------------------------------------

    def _check(self, payment: Payment) -> WalletBinding:
        if payment.amount_usdc_micro < 0 or payment.discount_usdc_micro < 0:
            raise BillingError("payment amounts are never negative")
        try:
            binding = self.wallets.wallet_for(payment.customer)
        except WalletBindingError:
            raise BillingError("the wallet directory could not be read") from None
        if binding is None:
            raise BillingError("no wallet is bound to this account")
        if payment.payer != binding.pubkey:
            raise BillingError("the payer is not this account's bound wallet")
        if payment.payee_usdc_ata != usdc_ata(self.fee_wallet):
            raise BillingError("the payee is not the fee wallet's USDC account")
        quote = self.store.quote(payment.customer, payment.quote_id)
        if quote is None:
            raise BillingError("no confirmed quote for this payment")
        if payment.amount_usdc_micro > quote.total_usdc_micro:
            raise BillingError("the payment is above the confirmed quote")
        if payment.amount_usdc_micro > self.config.max_run_usdc_micro:
            raise BillingError("the payment is above the per-run ceiling")
        return binding

    def _build(self, payment: Payment, binding: WalletBinding) -> PaymentBuild:
        return build_payment(
            payer=binding.pubkey,
            fee_wallet=self.fee_wallet,
            amount_usdc_micro=payment.amount_usdc_micro,
            pay_with=payment.pay_with,
            settlement_wallet=self.config.settlement_wallet,
            sol_pricer=self.sol_pricer,
        )

    def _send(self) -> SendTransport:
        assert self.send is not None  # set in __post_init__
        return self.send

    def _entry(self, payment: Payment, **overrides: Any) -> LedgerEntry:
        base = LedgerEntry(
            entry_id=self.new_id(),
            customer=payment.customer,
            kind="charge",
            run_id=payment.run_id,
            quote_id=payment.quote_id,
            amount_usdc_micro=payment.amount_usdc_micro,
            status="pending",
            pay_with=payment.pay_with,
            signature=None,
            slot=None,
            send_label=None,
            rebate_requested=False,
            rebate_lamports=None,
            split_user_pct=None,
            sol_usdc_rate_micro=None,
            created_at=self.clock(),
            note="",
        )
        return replace(base, **overrides)

    def _settle(
        self,
        pending: LedgerEntry,
        build: PaymentBuild,
        binding: WalletBinding,
        send: SendTransport,
    ) -> LedgerEntry:
        # The wallet's own policy (the one Privy enforces in the enclave), checked here
        # first over the exact bytes: config-derived, so a build that names any other
        # destination is refused even though its own payment policy would allow it.
        gate = WalletPolicyGate(
            policy=build.policy,
            ledger=InMemorySpendLedger(),
            wallet_policy=wallet_policy_from_config(self.config),
            payer=binding.pubkey,
        )
        signer = TransactionSigner(
            backend=self.backend_for(binding),
            # The human's decision this profile records is the user's confirmation of
            # the quote; the policy beside it allows exactly this payment and no other.
            profile=SignerProfile(
                name=EXTERNAL_SIGNER_PROFILE_NAME, network=self.network, authorized=True
            ),
            spend_gate=gate,
        )
        payee = build.settlement_wallet or build.payee_usdc_ata
        plan = PurchasePlan(
            api_id="gecko_billing",
            instruction="pay_run",
            accounts={"payer": build.payer, "payee": payee},
            args={},
            fee_payer=build.payer,
        )
        built: BuiltTx = build.built
        try:
            outcome = run_purchase(
                network=self.network,
                rpc_url=self.rpc_url,
                plan=plan,
                signer=signer,
                spend_gate=gate,
                build_call=lambda _request: built,
                rpc_call=self.rpc_call,
                priority_fee_microlamports=self.priority_fee_microlamports,
                confirm_timeout_seconds=self.confirm_timeout_seconds,
                poll_interval_seconds=self.poll_interval_seconds,
                sleep=self.sleep,
                monotonic=self.monotonic,
                send=send,
            )
        except PurchaseTransportError as exc:
            if exc.signature is None:
                return replace(
                    pending, status="failed", note=_note(f"not sent: {exc.reason}")
                )
            # Broadcast, outcome unknown to the loop: ask the chain once more.
            return self._read_back(pending, exc.signature)
        if isinstance(outcome, PurchaseRefused):
            return replace(
                pending,
                status="failed",
                note=_note(
                    f"refused before signing [{outcome.code}]: {outcome.reason}"
                ),
            )
        return self._read_back(pending, outcome.signature)

    def _read_back(self, pending: LedgerEntry, signature: str) -> LedgerEntry:
        try:
            result = self._transaction(signature)
        except RpcError:
            result = None
        return self._verdict(pending, signature, result)

    def _verdict(
        self,
        pending: LedgerEntry,
        signature: str,
        result: Mapping[str, Any] | None,
    ) -> LedgerEntry:
        """The independent check, read over RPC: USDC must be in the treasury's account,
        SOL in the settlement wallet, by exactly the amount. Not visible yet is
        ``pending`` with the signature kept, never ``failed``: it may still land."""
        if result is None:
            return replace(
                pending,
                status="pending",
                signature=signature,
                note=_note(
                    f"broadcast but not yet visible; re-checked by signature "
                    f"{signature}, never charged again"
                ),
            )
        meta = result.get("meta") or {}
        slot = result.get("slot")
        slot_value = (
            slot if isinstance(slot, int) and not isinstance(slot, bool) else None
        )
        if meta.get("err") is not None:
            return replace(
                pending,
                status="failed",
                signature=signature,
                slot=slot_value,
                note=_note(f"landed and failed on chain: {meta.get('err')}"),
            )
        if pending.pay_with == "SOL":
            settlement = self.config.settlement_wallet or ""
            expected = pending.amount_lamports or 0
            received = lamport_delta(result, settlement)
            if not settlement or received != expected:
                return replace(
                    pending,
                    status="failed",
                    signature=signature,
                    slot=slot_value,
                    note=_note(
                        f"landed, but the settlement wallet received {received} "
                        f"lamports, not {expected}; reconcile by hand"
                    ),
                )
            note = (
                f"paid {expected} lamports to the settlement wallet for "
                f"{pending.amount_usdc_micro} micro-USDC; final"
            )
        else:
            received = token_delta(meta, owner=self.fee_wallet, mint=USDC_MINT)
            if received != pending.amount_usdc_micro:
                return replace(
                    pending,
                    status="failed",
                    signature=signature,
                    slot=slot_value,
                    note=_note(
                        f"landed, but the fee wallet received {received} micro-USDC, "
                        f"not {pending.amount_usdc_micro}; reconcile by hand"
                    ),
                )
            note = f"paid {pending.amount_usdc_micro} micro-USDC"
        return replace(
            pending,
            status="landed",
            signature=signature,
            slot=slot_value,
            note=_note(note),
        )

    def _exists(self, address: str) -> bool:
        answer = self.rpc_call(
            self.rpc_url, "getAccountInfo", [address, {"encoding": "base64"}]
        )
        return (answer.get("result") or {}).get("value") is not None

    def _lamports(self, address: str) -> int:
        answer = self.rpc_call(self.rpc_url, "getBalance", [address])
        value = (answer.get("result") or {}).get("value")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def _usdc(self, owner: str) -> int:
        """The owner's USDC in micro units; an absent account is 0, not an error."""
        if not self._exists(usdc_ata(owner)):
            return 0
        answer = self.rpc_call(
            self.rpc_url, "getTokenAccountBalance", [usdc_ata(owner)]
        )
        amount = ((answer.get("result") or {}).get("value") or {}).get("amount")
        return int(amount) if isinstance(amount, str) and amount.isdigit() else 0

    def _transaction(self, signature: str) -> Mapping[str, Any] | None:
        answer = self.rpc_call(
            self.rpc_url,
            "getTransaction",
            [
                signature,
                {
                    "encoding": "json",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        result = answer.get("result")
        return result if isinstance(result, Mapping) else None

    def _recent_signatures(self, address: str) -> list[Mapping[str, Any]]:
        answer = self.rpc_call(
            self.rpc_url,
            "getSignaturesForAddress",
            [address, {"limit": REBATE_LOOKBACK, "commitment": "confirmed"}],
        )
        rows = answer.get("result")
        return [r for r in rows or () if isinstance(r, Mapping)]

    def _index(self, action: Callable[[], Any]) -> Any:
        try:
            return action()
        except RebateIndexError as exc:
            raise BillingError(f"rebate index: {exc}") from None

    def _relay_gain(self, signature: str) -> int:
        """Lamports the relay gained in ``signature``; 0 when it is not an inflow."""
        result = self._transaction(signature)
        if result is None:
            return 0
        if (result.get("meta") or {}).get("err") is not None:
            return 0
        return lamport_delta(result, self.relay)

    def _credit(
        self,
        index: RebateIndex,
        customer: str,
        settlement: LedgerEntry,
        signature: str,
        slot: int,
        lamports: int,
    ) -> LedgerEntry | None:
        """Claim the rebate for ``settlement`` in the global index, then write the user's
        share at the settlement's realised rate. ``None`` when another transaction holds
        it or the settlement has no rate."""
        if not settlement.sol_usdc_rate_micro or settlement.signature is None:
            return None
        swap_signature = settlement.signature
        claimed = self._index(
            lambda: index.claim(
                signature,
                slot=slot,
                lamports=lamports,
                payment_signature=swap_signature,
            )
        )
        if not claimed:
            return None
        split = self.config.rebate_split_user_pct or 0
        rate = settlement.sol_usdc_rate_micro
        credit = LedgerEntry(
            entry_id=self.new_id(),
            customer=customer,
            kind="rebate_credit",
            run_id=settlement.run_id,
            quote_id=settlement.quote_id,
            amount_usdc_micro=rebate_credit_micro(lamports, split, rate),
            status="landed",
            pay_with=None,
            signature=signature,
            slot=slot,
            send_label=settlement.send_label,
            rebate_requested=False,
            rebate_lamports=lamports,
            split_user_pct=split,
            sol_usdc_rate_micro=rate,
            created_at=self.clock(),
            note=(
                "relay rebate observed in the settlement swap's slot; your share is a "
                "discount"
            ),
        )
        try:
            self.store.append_ledger(credit)
        except DuplicateChargeError:
            return None  # this signature is already in the customer's ledger
        return credit
