"""Settling a landed SOL charge: Gecko's settlement wallet swaps it and pays the treasury.

Option B (founder ruling, 9 Oct). The user's charge is FINAL when their SOL lands in the
settlement wallet; from then on it is Gecko's revenue, and Gecko carries the swap's price
risk. :meth:`Settlement.settle` builds ONE transaction from the settlement wallet
(:mod:`gecko.anyquery.settlement_tx`: wrap, sync, ``swap_v2`` on the pinned pool,
``TransferChecked`` to the cold treasury), signs it through Privy (``signTransaction``
only, :class:`gecko.privy_backend.PrivyBackend` for the settlement wallet's id), and sends
it through the configured :class:`~gecko.send_endpoint.SendTransport`: Helius with
``rebate-address`` = the relay fee payer when the operator turned rebate routing on.

THE SAME PATH EVERY GECKO SIGNATURE TAKES. :func:`gecko.autonomous_purchase.run_purchase`
stamps a fresh blockhash, simulates the exact bytes, requires an ``exact`` binding, asks
the gate (:class:`~gecko.anyquery.settlement_policy.SettlementPolicyGate`: the pinned
pool and own accounts first, then a policy for THIS settlement only), signs and confirms.
A ``landed`` settlement is written only once the chain shows the treasury's USDC account
up by exactly the forwarded amount.

FAILURE NEVER RE-CHARGES THE USER. A settlement is its own ledger entry (``kind:
settlement``) on the run it settles; nothing here builds, signs or sends anything from the
user's wallet. A broadcast that is not visible yet stays ``pending`` WITH its signature
and is re-read by that signature (:meth:`recheck`), never sent again. A refused or
reverted attempt is ``failed``, and the next :meth:`settle` of the same charge tries
again, up to :data:`MAX_ATTEMPTS`; past that it stops and says so. One open (``pending``)
settlement per run is enforced by the store, so two settlers cannot race.

REBATES are matched to the SETTLEMENT's slot now, not the payment's: the swap is the
transaction a searcher can backrun (:meth:`gecko.anyquery.solana_billing.SolanaBilling.observe_rebates`).
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
from ..landing import NATIVE_SOL_MINT
from ..networks import Network
from ..rpc import RpcCall, RpcError, default_rpc_call
from ..send_endpoint import SendTransport, configured_send
from ..signer import (
    EXTERNAL_SIGNER_PROFILE_NAME,
    SignerProfile,
    SigningBackend,
    TransactionSigner,
)
from ..simulate import Receipt, simulate
from ..spend_policy import InMemorySpendLedger
from .billing import LEDGER_SCAN_LIMIT, BillingError
from .chain_read import token_delta
from .config import AnyQueryConfig, ConfigError
from .settlement_policy import (
    SettlementPolicy,
    SettlementPolicyGate,
    settlement_policy_from_config,
)
from .settlement_tx import (
    SETTLEMENT_FEE_LAMPORTS,
    SettlementBuild,
    SwapPlanner,
    build_settlement,
)
from .store import Store, StoreError
from .types import LedgerEntry

__all__ = ["MAX_ATTEMPTS", "Settlement", "settlement_backend_for"]

logger = logging.getLogger(__name__)

#: Failed attempts after which a charge is left for a human to reconcile.
MAX_ATTEMPTS = 3
_NOTE_CAP = 300


def _note(text: str) -> str:
    return text if len(text) <= _NOTE_CAP else text[: _NOTE_CAP - 3] + "..."


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def settlement_backend_for(
    config: AnyQueryConfig, network: Network, *, resolver: ChainResolver | None = None
) -> Callable[[], SigningBackend]:
    """The live signer: a Privy backend for the CONFIGURED settlement wallet id. Never
    an id from a request. The app id and secret come from the keychain, per settlement."""
    wallet_id = config.settlement_wallet_id
    if not wallet_id:
        raise BillingError(
            "no settlement wallet is configured (GECKO_SETTLEMENT_WALLET_ID)"
        )

    def make() -> SigningBackend:
        from ..privy_backend import from_keychain

        return from_keychain(network=network, wallet_id=wallet_id, resolver=resolver)

    return make


@dataclass
class Settlement:
    """Settle landed SOL charges from Gecko's settlement wallet. Every seam is injected."""

    config: AnyQueryConfig
    store: Store
    rpc_url: str
    #: ASSERTED by the operator, never inferred from ``rpc_url``.
    network: Network
    backend: Callable[[], SigningBackend]
    swap_planner: SwapPlanner
    rpc_call: RpcCall = default_rpc_call
    #: ``None``: :func:`gecko.send_endpoint.configured_send` with the relay fee payer.
    send: SendTransport | None = None
    #: Where the Helius key is read when ``send`` is ``None`` (default: the keychain).
    resolver: ChainResolver | None = None
    confirm_timeout_seconds: float = 60.0
    poll_interval_seconds: float = 1.0
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    clock: Callable[[], str] = _now
    new_id: Callable[[], str] = lambda: secrets.token_hex(8)  # noqa: E731
    policy: SettlementPolicy = field(init=False)

    def __post_init__(self) -> None:
        try:
            self.policy = settlement_policy_from_config(self.config)
        except ConfigError as exc:
            raise BillingError(f"settlement is not configured: {exc}") from None

    @property
    def wallet(self) -> str:
        return self.policy.wallet

    # -- the three verbs ------------------------------------------------------------

    def settle(self, charge: LedgerEntry) -> LedgerEntry:
        """Settle ``charge`` (a landed SOL charge) once; return its settlement entry.

        Idempotent per charge: a landed settlement is returned as is, a pending one is
        re-read by its signature, and only when every earlier attempt failed is a new one
        built. Raises :class:`BillingError` (nothing signed) when the charge is not a
        landed SOL charge, the wallet's token accounts are missing, or the attempts are
        spent.
        """
        lamports = self._check(charge)
        held = self._attempts(charge)
        for entry in held:
            if entry.status == "landed":
                return entry
        for entry in held:
            if entry.status == "pending":
                return self.recheck(entry)
        if len(held) >= MAX_ATTEMPTS:
            raise BillingError(
                f"settlement of run {charge.run_id} stopped after {len(held)} failed "
                f"attempts; the SOL is in the settlement wallet, reconcile by hand"
            )
        build = self._build(lamports)
        send = self._send()
        decision = send.rebate_for(build.built.tx)
        pending = LedgerEntry(
            entry_id=self.new_id(),
            customer=charge.customer,
            kind="settlement",
            run_id=charge.run_id,
            quote_id=charge.quote_id,
            amount_usdc_micro=build.transfer_usdc_micro,
            status="pending",
            pay_with="SOL",
            signature=None,
            slot=None,
            send_label=send.label,
            rebate_requested=decision.requested,
            rebate_lamports=None,
            split_user_pct=None,
            sol_usdc_rate_micro=None,
            created_at=self.clock(),
            note=_note(f"settles payment {charge.signature}"),
            amount_lamports=build.lamports_in,
        )
        # The store refuses a second OPEN settlement for this run before anything signs.
        self.store.append_ledger(pending)
        settled = self._run(pending, build, send)
        self._record(settled)
        if settled.status != "landed":
            logger.warning(
                "settlement of run %s is %s: %s",
                charge.run_id,
                settled.status,
                settled.note,
            )
        return settled

    def recheck(self, entry: LedgerEntry) -> LedgerEntry:
        """Read a pending settlement back by its signature. Never builds, signs or sends.
        Without a signature it was never broadcast, or the process stopped mid-send: it
        is returned unchanged for a human to reconcile."""
        if entry.kind != "settlement" or entry.status != "pending":
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
        return settled

    def rehearse(self, lamports_in: int) -> Receipt:
        """Build and SIMULATE a settlement of ``lamports_in`` (``sigVerify: false``,
        blockhash replaced). No signature, no send, no ledger row."""
        build = self._build(lamports_in)
        built = build.built
        return simulate(
            {},
            rpc_url=self.rpc_url,
            rpc_call=self.rpc_call,
            build_call=lambda _plan: built,
            replace_blockhash=True,
            network_label=f"rehearsed against {self.network} (read-only, unsigned)",
            network=self.network,
            track=[self.wallet],
        )

    def outstanding(self, customer: str) -> list[LedgerEntry]:
        """This customer's landed SOL charges with no landed settlement yet."""
        held = self.store.ledger(customer, limit=LEDGER_SCAN_LIMIT)
        done = {
            e.run_id for e in held if e.kind == "settlement" and e.status == "landed"
        }
        return [
            e
            for e in held
            if e.kind == "charge"
            and e.pay_with == "SOL"
            and e.status == "landed"
            and e.run_id not in done
        ]

    # -- internals ------------------------------------------------------------------

    def _check(self, charge: LedgerEntry) -> int:
        if charge.kind != "charge" or charge.pay_with != "SOL":
            raise BillingError("only a SOL charge is settled")
        if charge.status != "landed" or charge.signature is None:
            raise BillingError("only a charge that landed is settled")
        lamports = (charge.amount_lamports or 0) - SETTLEMENT_FEE_LAMPORTS
        if lamports <= 0:
            raise BillingError("the charge carries too little SOL to settle")
        return lamports

    def _attempts(self, charge: LedgerEntry) -> list[LedgerEntry]:
        return [
            e
            for e in self.store.ledger(charge.customer, limit=LEDGER_SCAN_LIMIT)
            if e.kind == "settlement" and e.run_id == charge.run_id
        ]

    def _build(self, lamports_in: int) -> SettlementBuild:
        p = self.policy
        try:
            for account, what in (
                (p.wsol_account, "wSOL"),
                (p.usdc_account, "USDC"),
            ):
                if not self._exists(account):
                    raise BillingError(
                        f"the settlement wallet has no {what} account yet; create it "
                        f"once with gecko.anyquery.privy_admin.create_settlement_wallet"
                    )
            if not self._exists(p.treasury_usdc_account):
                raise BillingError("the treasury has no USDC account yet")
            held = self._token_amount(p.usdc_account)
        except RpcError as exc:
            raise BillingError(
                _note(f"could not read the settlement wallet: {exc}")
            ) from None
        assert self.config.fee_wallet is not None  # checked by the policy
        return build_settlement(
            owner=p.wallet,
            fee_wallet=self.config.fee_wallet,
            lamports_in=lamports_in,
            held_usdc_micro=held,
            swap_planner=self.swap_planner,
            max_usdc_micro=p.max_usdc_micro,
            float_usdc_micro=self.config.settlement_float_usdc_micro,
        )

    def _send(self) -> SendTransport:
        if self.send is not None:
            return self.send
        transport, _note_text = configured_send(
            self.rpc_url,
            network=self.network,
            rpc_call=self.rpc_call,
            relay_fee_payer=self.config.relay_fee_payer,
            resolver=self.resolver,
        )
        return transport

    def _run(
        self, pending: LedgerEntry, build: SettlementBuild, send: SendTransport
    ) -> LedgerEntry:
        gate = SettlementPolicyGate(
            policy=build.policy,
            ledger=InMemorySpendLedger(),
            settlement_policy=self.policy,
        )
        signer = TransactionSigner(
            backend=self.backend(),
            # The human decision this records is the founder's: the settlement wallet's
            # policy, authored from config, and the pinned pool.
            profile=SignerProfile(
                name=EXTERNAL_SIGNER_PROFILE_NAME, network=self.network, authorized=True
            ),
            spend_gate=gate,
        )
        plan = PurchasePlan(
            api_id="gecko_settlement",
            instruction="settle_sol",
            accounts={"owner": build.owner, "treasury": build.treasury_usdc_ata},
            args={},
            fee_payer=build.owner,
        )
        built = build.built
        try:
            outcome = run_purchase(
                network=self.network,
                rpc_url=self.rpc_url,
                plan=plan,
                signer=signer,
                spend_gate=gate,
                build_call=lambda _request: built,
                rpc_call=self.rpc_call,
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
        """The treasury's USDC account must be up by exactly the forwarded amount."""
        if result is None:
            return replace(
                pending,
                status="pending",
                signature=signature,
                note=_note(
                    f"broadcast but not yet visible; re-read by signature {signature}, "
                    f"never sent again"
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
        assert self.config.fee_wallet is not None
        received = token_delta(meta, owner=self.config.fee_wallet, mint=USDC_MINT)
        if received != pending.amount_usdc_micro:
            return replace(
                pending,
                status="failed",
                signature=signature,
                slot=slot_value,
                note=_note(
                    f"landed, but the treasury received {received} micro-USDC, not "
                    f"{pending.amount_usdc_micro}; reconcile by hand"
                ),
            )
        pool = self.policy.pool
        sol_in = token_delta(meta, owner=pool, mint=NATIVE_SOL_MINT)
        usdc_out = -token_delta(meta, owner=pool, mint=USDC_MINT)
        rate = usdc_out * 1_000_000_000 // sol_in if sol_in > 0 else None
        return replace(
            pending,
            status="landed",
            signature=signature,
            slot=slot_value,
            sol_usdc_rate_micro=rate,
            note=_note(
                f"settled: swapped {sol_in} lamports for {usdc_out} micro-USDC on "
                f"Whirlpool {pool}; {pending.amount_usdc_micro} to the treasury, the "
                f"rest kept as the wallet's float"
            ),
        )

    def _record(self, settled: LedgerEntry) -> None:
        try:
            self.store.update_ledger(settled)
        except StoreError as exc:
            raise BillingError(
                f"settlement {settled.status} on chain (signature {settled.signature}) "
                f"but the ledger refused the update: {exc}"
            ) from None

    def _exists(self, address: str) -> bool:
        answer = self.rpc_call(
            self.rpc_url, "getAccountInfo", [address, {"encoding": "base64"}]
        )
        return (answer.get("result") or {}).get("value") is not None

    def _token_amount(self, account: str) -> int:
        answer = self.rpc_call(self.rpc_url, "getTokenAccountBalance", [account])
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
