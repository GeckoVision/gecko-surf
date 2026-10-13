"""Pay one Orthogonal call over x402 on Solana, from Gecko's settlement wallet.

Founder ruling, 9 Oct: with ``GECKO_ORTHOGONAL_PAY=x402`` the runner reaches Orthogonal
through ``x402.orthogonal.com`` and pays per call in USDC from the settlement wallet's
float, instead of spending the API key's card credits. Measured 9 Oct: ``GET
https://x402.orthogonal.com/{api}{path}`` answers 402 with a ``payment-required`` header
whose offers include Solana mainnet USDC, scheme ``exact``, and a payTo that differs PER
PROVIDER (scrapecreators ``4Bf6…k3Wg``, fiber ``DHZJ…Jy4NC``).

One call, in order (:meth:`OrthogonalX402.call`):

1. ask without payment; a 402's ``PAYMENT-REQUIRED`` is the challenge;
2. keep ONLY the Solana mainnet USDC ``exact`` offer, and refuse unless its payTo is the
   one PINNED for this operation (``pay_to_for``: the service registry's last challenge
   probe, refused when there is none or the operation is quarantined; the env override
   ``GECKO_ORTHOGONAL_X402_PAYTO`` replaces it for every call), its amount is within the call's catalog quote and the per-call
   cap, and it names the facilitator's fee payer;
3. build the reference payment (:mod:`gecko.x402_svm`) and SIMULATE it (``sigVerify``
   off, the real blockhash kept so the binding covers the exact bytes);
4. sign it the way every Gecko signature is signed: an ``exact`` binding over a passing
   receipt, the x402 gate first (:class:`gecko.anyquery.x402_policy.X402PaymentGate`:
   mint, destination = payTo's USDC account, amount = the challenge's), then Privy's
   ``signTransaction`` in the AUTHORITY role (the facilitator is the fee payer);
5. take our one signature out of Privy's answer, refusing a substituted message, and
   place it into OUR copy of the bytes (:mod:`gecko.cosign`);
6. retry with ``PAYMENT-SIGNATURE`` and return the reply in the key path's envelope,
   with a receipt: what was paid, to whom, and the signature the server reported.

Nothing before step 6 can have moved money: a refusal there raises
:class:`~gecko.orthogonal.errors.X402RefusedError` (``charge_possible`` False) and the
spend guard releases its hold. From step 6 on the signed payment is out of our hands, so
every failure is an :class:`~gecko.orthogonal.errors.UpstreamError` that may have been
charged, and the guard keeps the hold at the quote (founder-approved, 9 Oct).

Gecko never broadcasts here: the FACILITATOR submits the transaction. Nothing in this
module holds a key; the Privy backend is injected.
"""

from __future__ import annotations

import base64
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from ..autonomous_purchase import USDC_DECIMALS, USDC_MINT
from ..cosign import Contribution, CosignRefused, merge_signatures, take_signature
from ..handoff import SignerHandoff, verify_handoff
from ..landing import TOKEN_PROGRAM_ID
from ..networks import Network
from ..orthogonal.errors import UpstreamError, X402RefusedError
from ..orthogonal.extract import X402_RECEIPT_KEY
from ..orthogonal.spec import X402_HOST, Target
from ..rpc import RpcCall, RpcError, default_rpc_call
from ..servicemap.registry import RegistryError
from ..signer import (
    AUTHORITY_ROLE,
    EXTERNAL_SIGNER_PROFILE_NAME,
    SignerProfile,
    SignerRefused,
    SigningBackend,
    TransactionSigner,
)
from ..simulate import BuiltTx, Receipt, simulate
from ..spend_policy import InMemorySpendLedger
from ..x402_http import HttpReply, HttpRequest, HttpSend, live_http
from ..x402_svm import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    SOLANA_MAINNET,
    X402Error,
    build_exact_payment,
    decode_payment_required,
    decode_payment_response,
    encode_payment_signature,
    header,
    memo_for,
    solana_exact_offers,
)
from .config import ConfigError
from .x402_policy import (
    X402Expectation,
    X402PaymentGate,
    X402PayPolicy,
    check_x402_payment,
    x402_spend_policy,
)

__all__ = [
    "ORTHOGONAL_X402_HOST",
    "HttpReply",
    "HttpRequest",
    "OrthogonalX402",
    "PayToFor",
    "X402Receipt",
    "X402Rehearsal",
    "live_http",
]

#: The x402 host. A trust anchor, defined once in the Orthogonal spec.
ORTHOGONAL_X402_HOST = X402_HOST
#: One micro-USDC is 1/10_000 of a cent.
_MICRO_PER_CENT = 10_000
_API = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_NOTE_CAP = 200

#: ``(method, api, path template) -> payTo`` for one operation. Raises a
#: :class:`~gecko.servicemap.registry.RegistryError` when none may be pinned
#: (:func:`gecko.servicemap.pins.pay_to_resolver`).
PayToFor = Callable[[str, str, str], str]


@dataclass(frozen=True)
class X402Receipt:
    """One paid call: the endpoint, what was paid, to whom, and the settlement
    signature the server reported (``None`` when its answer carried none)."""

    endpoint: str
    amount_usdc_micro: int
    pay_to: str
    signature: str | None
    settled: bool


@dataclass(frozen=True)
class X402Rehearsal:
    """What a rehearsal saw: the offer it would pay, the simulation, the gate."""

    url: str
    amount_usdc_micro: int
    pay_to: str
    fee_payer: str
    simulation: str
    units_consumed: int | None
    #: ``None`` when the local gate would let these bytes be signed; else its refusal.
    gate: str | None


@dataclass(frozen=True)
class _Prepared:
    unsigned: bytes
    subject: str
    receipt: Receipt
    handoff: SignerHandoff
    expectation: X402Expectation
    memo: bytes


def _note(text: str) -> str:
    return text if len(text) <= _NOTE_CAP else text[: _NOTE_CAP - 3] + "..."


@dataclass
class OrthogonalX402:
    """An :class:`gecko.orthogonal.api.X402Caller` paying from the settlement wallet."""

    policy: X402PayPolicy
    #: ASSERTED by the operator. Only mainnet: the offer we take is mainnet USDC.
    network: Network
    rpc_url: str
    backend: Callable[[], SigningBackend]
    http: HttpSend = live_http
    rpc_call: RpcCall = default_rpc_call
    nonce: Callable[[], bytes] = lambda: os.urandom(16)  # noqa: E731
    host: str = ORTHOGONAL_X402_HOST
    #: Per-operation payTo pins. Unused when ``policy.pay_to`` is set (the env override).
    pay_to_for: PayToFor | None = None
    receipts: list[X402Receipt] = field(default_factory=list)

    def call(self, target: Target, body: dict[str, Any]) -> Any:
        if self.network != "mainnet":
            raise X402RefusedError(
                "x402 payments to Orthogonal are mainnet USDC only; nothing was paid"
            )
        request = self._request(target, body)
        first = self._send(request, paid=False)
        if 200 <= first.status < 300:
            # Free for once: nothing was paid, and nothing is claimed.
            return {"data": first.body, "priceCents": 0}
        if first.status != 402:
            raise UpstreamError(
                f"x402.orthogonal.com answered HTTP {first.status} before any payment.",
                status=first.status,
                charge_possible=False,
            )
        challenge = header(first.headers, PAYMENT_REQUIRED_HEADER)
        try:
            required = decode_payment_required(challenge or "")
        except X402Error as exc:
            raise X402RefusedError(f"{exc}; nothing was paid") from None
        offer, payment = self._pay(target, request.method, required)
        retry = HttpRequest(
            request.method,
            request.url,
            {
                **request.headers,
                PAYMENT_SIGNATURE_HEADER: encode_payment_signature(
                    required, offer, payment
                ),
            },
            request.json_body,
        )
        # From here the signed payment is out of our hands: every failure may cost.
        paid = self._send(retry, paid=True)
        answer = header(paid.headers, PAYMENT_RESPONSE_HEADER)
        settled = None
        if answer:
            try:
                settled = decode_payment_response(answer)
            except X402Error:
                settled = None
        receipt = X402Receipt(
            endpoint=target.label,
            amount_usdc_micro=offer.amount,
            pay_to=offer.pay_to,
            signature=settled.transaction if settled and settled.success else None,
            settled=bool(settled and settled.success),
        )
        self.receipts.append(receipt)
        if not 200 <= paid.status < 300:
            raise UpstreamError(
                f"x402.orthogonal.com answered HTTP {paid.status} to a paid call on "
                f"{target.label}. It may have been charged.",
                status=paid.status,
            )
        return {
            "data": paid.body,
            "priceCents": math.ceil(offer.amount / _MICRO_PER_CENT),
            "quotedPriceCents": target.pinned_cents,
            X402_RECEIPT_KEY: {
                "signature": receipt.signature,
                "amount_usdc_micro": receipt.amount_usdc_micro,
                "pay_to": receipt.pay_to,
            },
        }

    # -- the request ----------------------------------------------------------------

    def _request(self, target: Target, body: Mapping[str, Any]) -> HttpRequest:
        """``{host}/{api}{path}``: the measured shape. GET carries the query; a call
        with a body is POSTed as JSON (UNCONFIRMED on this host, 9 Oct)."""
        api, path = str(body.get("api")), str(body.get("path"))
        if not _API.match(api) or not path.startswith("/") or not target.matches(path):
            raise X402RefusedError("the call is not one this host is asked for")
        query = body.get("query") or {}
        payload = body.get("body")
        # The same allowlist the key path enforces: no input the target does not name.
        if not isinstance(query, Mapping) or not set(query) <= target.query_keys:
            raise X402RefusedError("the call carries a query input it may not carry")
        if isinstance(payload, Mapping) and not set(payload) <= target.body_keys:
            raise X402RefusedError("the call carries a body input it may not carry")
        url = f"{self.host}/{api}{path}"
        if query:
            url += "?" + urlencode(sorted((str(k), str(v)) for k, v in query.items()))
        method = "POST" if isinstance(payload, Mapping) else "GET"
        return HttpRequest(method, url, {"Accept": "application/json"}, payload)

    def _send(self, request: HttpRequest, *, paid: bool) -> HttpReply:
        try:
            return self.http(request)
        except Exception as exc:  # noqa: BLE001 - the type is all that is safe to name
            raise UpstreamError(
                f"The call to x402.orthogonal.com broke ({type(exc).__name__})."
                + (" It may have been charged." if paid else " Nothing was paid."),
                charge_possible=paid,
            ) from None

    # -- the payment ----------------------------------------------------------------

    def rehearse(self, target: Target, body: dict[str, Any]) -> X402Rehearsal:
        """Steps 1 to 3 and the local gate, and NOTHING after: the challenge is read,
        the offer checked, the payment built and simulated (``sigVerify`` off). No
        signature, no paid request. Refusals raise exactly as :meth:`call` would."""
        if self.network != "mainnet":
            raise X402RefusedError("x402 payments to Orthogonal are mainnet USDC only")
        request = self._request(target, body)
        first = self._send(request, paid=False)
        if first.status != 402:
            raise UpstreamError(
                f"x402.orthogonal.com answered HTTP {first.status}, not a challenge.",
                status=first.status,
                charge_possible=False,
            )
        try:
            required = decode_payment_required(
                header(first.headers, PAYMENT_REQUIRED_HEADER) or ""
            )
        except X402Error as exc:
            raise X402RefusedError(f"{exc}; nothing was paid") from None
        offer, policy = self._checked_offer(target, request.method, required)
        try:
            prepared = self._prepare(offer, policy)
        except (X402Error, RpcError) as exc:
            raise X402RefusedError(_note(f"{exc}; nothing was paid")) from None
        refused = check_x402_payment(prepared.subject, prepared.expectation)
        return X402Rehearsal(
            url=request.url,
            amount_usdc_micro=offer.amount,
            pay_to=offer.pay_to,
            fee_payer=offer.fee_payer or "",
            simulation=prepared.receipt.status,
            units_consumed=prepared.receipt.units_consumed,
            gate=None if refused is None else f"[{refused.code}] {refused.reason}",
        )

    def _policy_for(self, target: Target, method: str) -> X402PayPolicy:
        """The policy pinned to THIS operation's payTo, or a refusal (nothing paid)."""
        if self.policy.pay_to is not None:
            return self.policy  # the env override pays every call to one address
        if self.pay_to_for is None:
            raise X402RefusedError(
                "no payTo can be pinned: neither the service registry nor "
                "GECKO_ORTHOGONAL_X402_PAYTO is configured; nothing was paid"
            )
        try:
            pay_to = self.pay_to_for(method, target.api, target.path)
            return self.policy.pinned_to(pay_to)
        except (RegistryError, ConfigError) as exc:
            raise X402RefusedError(_note(f"{exc}; nothing was paid")) from None

    def _checked_offer(
        self, target: Target, method: str, required: Any
    ) -> tuple[Any, X402PayPolicy]:
        """The one offer this payer takes and the policy pinned for it, or a refusal."""
        p = self._policy_for(target, method)
        offers = solana_exact_offers(required, network=SOLANA_MAINNET, asset=USDC_MINT)
        if len(offers) != 1:
            raise X402RefusedError(
                f"the challenge carries {len(offers)} Solana USDC 'exact' offers, not "
                f"one; nothing was paid"
            )
        [offer] = offers
        if offer.pay_to != p.pay_to:
            raise X402RefusedError(
                "the challenge's payTo is not the one pinned for this operation "
                "(the registry's last probe, or GECKO_ORTHOGONAL_X402_PAYTO); "
                "nothing was paid"
            )
        quote_micro = target.pinned_cents * _MICRO_PER_CENT
        if offer.amount > quote_micro:
            raise X402RefusedError(
                f"the challenge asks {offer.amount} micro-USDC, above this call's quoted "
                f"{quote_micro}; nothing was paid"
            )
        if offer.amount > p.max_call_usdc_micro:
            raise X402RefusedError(
                f"the challenge asks {offer.amount} micro-USDC, above the per-call cap "
                f"{p.max_call_usdc_micro}; nothing was paid"
            )
        if offer.amount <= 0 or offer.fee_payer is None:
            raise X402RefusedError(
                "the challenge names no amount or no fee payer; nothing was paid"
            )
        return offer, p

    def _pay(self, target: Target, method: str, required: Any) -> tuple[Any, str]:
        """The checked offer and the base64 payment carrying our signature, or a
        refusal (nothing signed, nothing sent)."""
        offer, policy = self._checked_offer(target, method, required)
        try:
            return offer, self._signed(offer, policy)
        except (X402Error, RpcError) as exc:
            raise X402RefusedError(_note(f"{exc}; nothing was paid")) from None

    def _prepare(self, offer: Any, p: X402PayPolicy) -> _Prepared:
        """Build and simulate the payment; the receipt must pass and bind exactly."""
        assert p.pay_to_usdc_account is not None  # pinned by _policy_for
        nonce = self.nonce()
        unsigned = build_exact_payment(
            offer,
            payer=p.wallet,
            source=p.wallet_usdc_account,
            destination=p.pay_to_usdc_account,
            decimals=USDC_DECIMALS,
            token_program=TOKEN_PROGRAM_ID,
            blockhash=self._blockhash(),
            nonce=nonce,
        )
        subject = base64.b64encode(unsigned).decode("ascii")
        receipt = simulate(
            {},
            rpc_url=self.rpc_url,
            rpc_call=self.rpc_call,
            build_call=lambda _plan: BuiltTx(tx=subject, encoding="base64"),
            replace_blockhash=False,
            network_label=f"simulated against {self.network} (x402, unsigned)",
            network=self.network,
            track=[p.wallet],
        )
        if receipt.status != "pass":
            raise X402Error(
                f"the payment did not pass simulation (status={receipt.status}, "
                f"class={receipt.revert_class})"
            )
        handoff = verify_handoff(
            subject,
            receipt,
            require="exact",
            expected_network=self.network,
            encoding="base64",
        )
        if not handoff.approved or handoff.transaction_base64 is None:
            raise X402Error(
                f"the receipt does not attest the payment: {handoff.reason}"
            )
        assert offer.fee_payer is not None  # checked by _checked_offer
        expectation = X402Expectation(
            policy=p, amount_usdc_micro=offer.amount, fee_payer=offer.fee_payer
        )
        return _Prepared(
            unsigned=unsigned,
            subject=subject,
            receipt=receipt,
            handoff=handoff,
            expectation=expectation,
            memo=memo_for(offer, nonce),
        )

    def _signed(self, offer: Any, p: X402PayPolicy) -> str:
        prepared = self._prepare(offer, p)
        unsigned, subject = prepared.unsigned, prepared.subject
        receipt, handoff = prepared.receipt, prepared.handoff
        gate = X402PaymentGate(
            policy=x402_spend_policy(prepared.expectation, memo=prepared.memo),
            ledger=InMemorySpendLedger(),
            expectation=prepared.expectation,
        )
        signer = TransactionSigner(
            backend=self.backend(),
            # The human decision this records is the founder's: the settlement wallet's
            # policy and the per-call cap from config, the payTo from the registry's
            # probe (or the founder's override).
            profile=SignerProfile(
                name=EXTERNAL_SIGNER_PROFILE_NAME,
                network=self.network,
                authorized=True,
                signing_as=AUTHORITY_ROLE,
            ),
            spend_gate=gate,
        )
        try:
            signed = signer.sign(
                handoff, receipt=receipt, current_slot=self._current_slot()
            )
        except SignerRefused as refusal:
            raise X402Error(f"signing refused [{refusal.code}]: {refusal}") from None
        from solders.transaction import VersionedTransaction

        try:
            ours = take_signature(
                signed.signed_transaction_base64,
                expect_message=bytes(VersionedTransaction.from_bytes(unsigned).message),
                signer=p.wallet,
            )
            return merge_signatures(
                subject, [Contribution(p.wallet, ours)], require_complete=False
            )
        except CosignRefused as refusal:
            raise X402Error(f"[{refusal.code}] {refusal.reason}") from None

    def _blockhash(self) -> str:
        answer = self.rpc_call(
            self.rpc_url, "getLatestBlockhash", [{"commitment": "confirmed"}]
        )
        value = (answer.get("result") or {}).get("value") or {}
        blockhash = value.get("blockhash")
        if not isinstance(blockhash, str) or not blockhash:
            raise X402Error("the RPC returned no blockhash")
        return blockhash

    def _current_slot(self) -> int:
        answer = self.rpc_call(self.rpc_url, "getSlot", [{"commitment": "processed"}])
        slot = answer.get("result")
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise X402Error("the RPC returned no usable slot")
        return slot
