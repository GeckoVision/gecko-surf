"""One call that prepares, verifies, authorises, signs and settles a purchase.

WHY THIS IS ONE CALL, and not a prepare/approve/sign trio. A Solana blockhash is valid for
roughly 150 slots — about sixty seconds. A surface that returns a prepared transaction to
its caller, waits for a decision, and then signs, spends that minute on the round trip: the
blockhash expires mid-conversation and the approved bytes can no longer land. The proven
mainnet run worked because prepare and sign happened in ONE process. So the policy decision
has to be taken INSIDE this call, from rules configured before it started, and there is no
parameter through which a caller could supply one late.

That constraint is what makes this safe rather than reckless. Nothing here is a shortcut
past a check: every refusal that guards the two-script path guards this one, in the same
order, and the only thing removed is the human keystroke between two of them — which was
never a check, only a delay that the blockhash could not afford.

THE ORDER, AND WHY EACH STEP IS WHERE IT IS:

1. :func:`~gecko.plan_refusals.check_plan_accounts` on the RESOLVED account map, before the
   builder is asked. A builder is not the authority on whether a plan is sane — the real
   one answered ``HTTP 200`` to a purchase that paid the buyer back — so judging its
   response instead would mean the bad plan had already left this machine.
2. Build, once, through an injected :data:`~gecko.simulate.BuildCall`. Once, because
   building twice yields two different transactions and we would verify the one we do not
   sign.
3. A FRESH blockhash from the RPC we are about to submit to, written into the built bytes
   at its structurally derived offset (:func:`~gecko.txbind.blockhash_offset`). See
   :func:`_with_fresh_blockhash` for why this is a substitution rather than a re-assembly.
4. Simulate THOSE bytes with ``replaceRecentBlockhash: false``, on the network the caller
   ASSERTED. Nothing here infers a network: a fork proxy answers at any hostname.
5. Require a passing receipt AND an ``exact`` binding. Anything weaker attests a message
   that is not the one about to be signed.
6+7. Sign through :class:`~gecko.signer.TransactionSigner`, which asks the configured
   :class:`~gecko.spend_policy.SpendPolicyGate` and publishes the verdict it gated on.
   That is the ONLY consultation: the gate reserves budget when it approves, so asking it
   a second time to learn what it decided writes two ledger rows for one purchase and
   silently halves every rolling cap. ``agent_supplied_policy`` is pinned to the literal
   ``None`` inside the signer: that argument exists to be refused.
8. Send, confirm, and report predicted CU beside consumed CU.

WHAT THIS MODULE DOES NOT DO. It holds no key: the signature comes from a
:class:`~gecko.signer.SigningBackend` implemented outside ``gecko/``. It decides no policy:
the caps are authored out of band by a human. It infers no network. And it is not a
mainnet-safety argument — pointing it at mainnet spends real money, which is the founder's
decision to make and never this module's.

KNOWN LIMITATION — THE LEDGER FORGETS. The default
:class:`~gecko.spend_policy.InMemorySpendLedger` lives in one process. A restarted process
starts its hourly and daily windows at zero, so a crash-loop can spend the daily cap many
times over in an hour. It is ADVISORY, and it is not a control. Use
:class:`~gecko.spend_policy.FileSpendLedger` to survive a restart, and read that class's
own docstring for why even it is not a control.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .handoff import verify_handoff
from .feebump import FeebumpError, with_priority_fee
from .landing import RPC_COMMITMENT, latest_blockhash
from .landing import priority_fee_microlamports as estimate_priority_fee
from .networks import Network
from .plan_refusals import PlanRefused, check_plan_accounts
from .rpc import RpcCall, default_rpc_call
from .signer import SignerRefused, TransactionSigner
from .simulate import BuildCall, BuiltTx, Receipt, simulate
from .spend_policy import (
    AllowedInstruction,
    SpendPolicy,
    SpendPolicyGate,
    SpendVerdict,
    TokenCap,
    TokenCaps,
)

# `_message_of` is imported rather than re-implemented so that "which wire layout is this"
# is answered in exactly one place. A second copy of that branch here is how a v0 message
# eventually gets patched at a legacy offset.
from .txbind import TxDecodeError, _message_of, blockhash_offset

__all__ = [
    "LET_ME_BUY_PROGRAM",
    "MAKE_PURCHASE_DISCRIMINATOR",
    "PurchaseConfigurationError",
    "PurchaseOutcome",
    "PurchasePlan",
    "PurchaseRefusalCode",
    "PurchaseRefused",
    "PurchaseSettled",
    "PurchaseTransportError",
    "RefusedRunHasNoSignature",
    "USDC_DECIMALS",
    "USDC_MINT",
    "default_spend_policy",
    "run_purchase",
]


# --------------------------------------------------------------------------------------
# The surface these defaults describe.
# --------------------------------------------------------------------------------------

#: USDC on Solana mainnet. Six decimals — every raw figure below is in THOSE units.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

LET_ME_BUY_PROGRAM = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
#: The runtime's fee program. Allowlisted ONLY for SetComputeUnitPrice (tag 3, below).
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
#: Anchor's eight-byte selector for ``make_purchase`` — ``sha256("global:make_purchase")``
#: truncated to 8. Pinned as a literal and cross-checked against a real built transaction,
#: so a rename upstream refuses here instead of quietly matching nothing.
MAKE_PURCHASE_DISCRIMINATOR = bytes.fromhex("c13ee38869d4c914")

#: The demo ceiling on the SOL leg — fees and any rent the purchase pays. It is NOT the
#: price: the price is denominated in USDC and capped by :data:`DEFAULT_USDC_*` below. A
#: lamport cap cannot see a token drain, which is the whole reason both exist.
DEFAULT_PER_TRANSACTION_LAMPORTS = 10_000_000  # 0.01 SOL
DEFAULT_HOURLY_LAMPORTS = 50_000_000  # 0.05 SOL
DEFAULT_DAILY_LAMPORTS = 200_000_000  # 0.2 SOL
DEFAULT_MAX_TRANSACTIONS_PER_DAY = 20

#: USDC bounds, in RAW base units at six decimals. 1_000_000 raw == 1.00 USDC. Written as
#: raw integers rather than floats because that is what the cap is compared against, and a
#: float that reads "1.0" next to an integer that means "one millionth" is how a cap gets
#: authored a million times too wide.
DEFAULT_USDC_PER_TRANSACTION_RAW = 1_000_000  # 1.00 USDC
DEFAULT_USDC_HOURLY_RAW = 5_000_000  # 5.00 USDC
DEFAULT_USDC_DAILY_RAW = 20_000_000  # 20.00 USDC


def default_spend_policy(
    *,
    allowed_destinations: frozenset[str],
    usdc_per_transaction_raw: int = DEFAULT_USDC_PER_TRANSACTION_RAW,
    usdc_hourly_raw: int = DEFAULT_USDC_HOURLY_RAW,
    usdc_daily_raw: int = DEFAULT_USDC_DAILY_RAW,
    per_transaction_lamports: int = DEFAULT_PER_TRANSACTION_LAMPORTS,
    hourly_lamports: int = DEFAULT_HOURLY_LAMPORTS,
    daily_lamports: int = DEFAULT_DAILY_LAMPORTS,
    max_transactions_per_day: int = DEFAULT_MAX_TRANSACTIONS_PER_DAY,
) -> SpendPolicy:
    """A complete, deliberately small policy for the ``let_me_buy make_purchase`` demo.

    Every number is a keyword with an obvious default so that changing one is a one-line
    edit at the call site rather than a fork of this function.

    ``allowed_destinations`` has NO default and never will. It is the set of accounts this
    agent may write, and the only honest default is "none" — which would refuse everything
    and teach a caller to pass a wildcard. Naming the store's accounts is the decision.

    ``authorized=True`` is set here because constructing this function's result IS the act
    of authoring a policy. The human's decision is the call site, not a later mutation.
    """
    if not allowed_destinations:
        raise PurchaseConfigurationError(
            "a policy with no allowed destinations authorises nothing; name the accounts "
            "this agent may write"
        )
    return SpendPolicy(
        authorized=True,
        per_transaction_cap_lamports=per_transaction_lamports,
        hourly_cap_lamports=hourly_lamports,
        daily_cap_lamports=daily_lamports,
        max_transactions_per_day=max_transactions_per_day,
        allowed_instructions=frozenset(
            {
                AllowedInstruction(
                    program_id=LET_ME_BUY_PROGRAM,
                    discriminator=MAKE_PURCHASE_DISCRIMINATOR,
                ),
                # SetComputeUnitPrice (tag 3): the priority-fee bid gecko/feebump.py
                # prepends. It moves no funds and touches no accounts — the narrowest
                # entry that lets a priced purchase through the gate. Added 2026-08-31
                # when the gate (correctly) refused the first fee-injected transaction
                # as program-not-allowlisted.
                AllowedInstruction(
                    program_id=COMPUTE_BUDGET_PROGRAM,
                    discriminator=b"\x03",
                ),
            }
        ),
        allowed_destinations=frozenset(allowed_destinations),
        token_caps=TokenCaps.of(
            [
                TokenCap(
                    mint=USDC_MINT,
                    decimals=USDC_DECIMALS,
                    per_transaction_raw=usdc_per_transaction_raw,
                    hourly_raw=usdc_hourly_raw,
                    daily_raw=usdc_daily_raw,
                )
            ]
        ),
    )


# --------------------------------------------------------------------------------------
# Outcomes.
# --------------------------------------------------------------------------------------

#: Closed. Each member is a DIFFERENT operator action, so they may never render as one
#: string. ``spend-refused`` additionally carries the gate's own finer code on the verdict.
PurchaseRefusalCode = Literal[
    "plan-refused",
    "build-returned-nothing",
    "receipt-failed",
    "binding-refused",
    "spend-refused",
    "signer-refused",
]


class PurchaseConfigurationError(Exception):
    """The loop was wired wrong — a programming error, raised loudly rather than refused.

    Distinct from a refusal on purpose: a refusal is a run that was checked and denied, and
    reporting a mis-wiring as one would put a configuration bug in the same bucket as a
    working policy saying no.
    """


class PurchaseTransportError(Exception):
    """A network or node failure. Never a policy answer, and never a silent zero.

    ``signature`` is present when the transaction was already broadcast and the failure came
    afterwards — a caller needs it to find out what actually happened on chain. It is a
    public identifier, not a secret.
    """

    def __init__(self, reason: str, *, signature: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.signature = signature


class RefusedRunHasNoSignature(Exception):
    """Raised by :attr:`PurchaseRefused.signature`. Nothing was signed, so there is nothing.

    It RAISES rather than returning ``None`` for the reason
    :meth:`~gecko.simulate.TokenDeltaReport.outflows` raises: a falsy placeholder is read as
    an answer by the next line of caller code, and "no signature" quietly becomes "" in a
    log, a receipt, or a UI that then claims a purchase happened.
    """


@dataclass(frozen=True)
class PurchasePlan:
    """What to buy, fully resolved. Crosses a module boundary, so it is typed.

    ``accounts`` is the RESOLVED map — real addresses, already derived. The distinctness
    rule runs over exactly this, so a plan that still holds placeholders is refused for
    ``account-unresolved`` rather than checked loosely.
    """

    api_id: str
    instruction: str
    accounts: Mapping[str, str]
    args: Mapping[str, Any]
    fee_payer: str

    def build_request(self) -> dict[str, Any]:
        """The builder's wire shape. Kept here so the loop never hand-assembles one."""
        return {
            "accounts": dict(self.accounts),
            "args": dict(self.args),
            "feePayer": self.fee_payer,
        }


@dataclass(frozen=True)
class PurchaseSettled:
    """A purchase that passed every check, was signed, and was confirmed on chain."""

    signature: str
    network: Network
    receipt: Receipt
    verdict: SpendVerdict
    #: What the simulation predicted, and what the chain actually charged. Reported side by
    #: side and never reconciled: a difference is a fact about the run, not an error to fix.
    predicted_units: int | None
    consumed_units: int | None
    blockhash: str
    last_valid_block_height: int
    priority_fee_microlamports: int
    settled: Literal[True] = True

    @property
    def code(self) -> None:
        """No refusal fired. Present so both variants answer the same question."""
        return None


@dataclass(frozen=True)
class PurchaseRefused:
    """A run that was checked and denied. It carries no signature, by construction.

    There is no ``signature`` FIELD here — reading the attribute goes through a property
    that raises. Both halves matter: the missing field means nothing can be assigned one,
    and the raising property means a caller who reads it blindly gets an exception instead
    of a plausible empty string.
    """

    code: PurchaseRefusalCode
    reason: str
    network: Network
    #: Present from step 4 onwards; ``None`` for a plan or build refusal, which happen
    #: before anything was simulated.
    receipt: Receipt | None = None
    #: The gate's answer, when the gate was reached. ``None`` means it never ran — which is
    #: not the same as "it approved", and must never be rendered as one.
    verdict: SpendVerdict | None = None
    predicted_units: int | None = None
    settled: Literal[False] = False

    @property
    def signature(self) -> str:
        raise RefusedRunHasNoSignature(
            f"this purchase was refused [{self.code}] and never signed, so it has no "
            f"signature: {self.reason}"
        )


PurchaseOutcome = PurchaseSettled | PurchaseRefused


def run_purchase(
    *,
    network: Network,
    rpc_url: str,
    plan: PurchasePlan,
    signer: TransactionSigner,
    spend_gate: SpendPolicyGate,
    build_call: BuildCall,
    rpc_call: RpcCall | None = None,
    mint_extensions: Mapping[str, Sequence[str]] | None = None,
    now: float | None = None,
    priority_fee_microlamports: int | None = None,
    confirm_timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> PurchaseOutcome:
    """Prepare, verify, authorise, sign and settle one purchase. Fail-closed at every step.

    Returns a typed :data:`PurchaseOutcome`. An EXPECTED refusal — a plan that violates a
    distinctness rule, a simulation that reverts, a policy that says no — comes back as
    :class:`PurchaseRefused` rather than an exception, because those are answers and a
    caller must handle them. Transport failures and mis-wiring RAISE, because they are not
    answers.

    ``network`` is ASSERTED by the caller and never derived from ``rpc_url``: a fork proxy
    answers at any hostname, so the URL is evidence of nothing.

    ``spend_gate`` must be the very gate ``signer`` holds. Two gates would mean two policies
    decided one purchase, and the one that mattered would be whichever ran last.

    THERE IS NO PARAMETER FOR A VERDICT OR A POLICY. The authorization answer is produced
    inside, from the configured gate, over the exact bytes that were simulated. A caller
    cannot supply one, widen one, or skip one.
    """
    call = rpc_call or default_rpc_call
    if signer.spend_gate is not spend_gate:
        raise PurchaseConfigurationError(
            "the spend gate passed here is not the one the signer holds; one purchase is "
            "decided by one policy, and two gates make that whichever ran last"
        )

    # 1. THE PLAN, before the builder is asked. See the module docstring for why judging
    #    the builder's response instead is too late.
    try:
        check_plan_accounts(plan.api_id, plan.instruction, plan.accounts)
    except PlanRefused as refusal:
        return PurchaseRefused(
            code="plan-refused",
            reason=f"[{refusal.code}] {refusal.reason}",
            network=network,
        )

    # 2. BUILD, exactly once.
    built = build_call(plan.build_request())
    if not built.tx:
        return PurchaseRefused(
            code="build-returned-nothing",
            reason="the builder returned no transaction, so there is nothing to verify",
            network=network,
        )

    # 3. A FRESH BLOCKHASH from the node we are about to submit to, plus a priority fee.
    #    When the caller supplies one (``priority_fee_microlamports``), it is INJECTED as
    #    a prepended compute-budget instruction via a verified rebuild (gecko/feebump.py)
    #    — the builder's program must survive byte-for-byte semantically or the rebuild is
    #    refused and the untouched bytes proceed at fee 0. When the caller supplies None,
    #    the fee is estimated and RECORDED only, the pre-2026-08-31 behaviour (which lost
    #    7 of 12 broadcasts to a market this loop was bidding 0 into). Either way the
    #    simulation below covers the exact bytes that will be signed, injection included.
    blockhash, last_valid_block_height = latest_blockhash(rpc_url, call)
    if priority_fee_microlamports is None:
        fee = estimate_priority_fee([plan.fee_payer], rpc_url=rpc_url, rpc_call=call)
        priced = (
            built.tx
            if built.encoding == "base64"
            else base64.b64encode(_decode(built)).decode()
        )
    else:
        fee = max(0, priority_fee_microlamports)
        base64_tx = (
            built.tx
            if built.encoding == "base64"
            else base64.b64encode(_decode(built)).decode()
        )
        try:
            priced = with_priority_fee(base64_tx, fee)
        except FeebumpError:
            # The fee is an enrichment; a rebuild this module cannot PROVE intact must
            # never be signed, so the untouched builder bytes proceed unpriced.
            priced = base64_tx
            fee = 0
    subject = _with_fresh_blockhash(BuiltTx(tx=priced, encoding="base64"), blockhash)

    # 4. SIMULATE THE SUBJECT — the exact bytes that will be signed, nothing re-assembled.
    receipt = simulate(
        {},
        rpc_url=rpc_url,
        rpc_call=call,
        build_call=lambda _plan: BuiltTx(tx=subject, encoding="base64"),
        replace_blockhash=False,
        network_label=f"simulated against {network} (read-only, unsigned)",
        network=network,
        track=[plan.fee_payer],
        mint_extensions=mint_extensions,
    )
    if receipt.status != "pass":
        return PurchaseRefused(
            code="receipt-failed",
            reason=(
                f"the simulation did not pass (status={receipt.status}, "
                f"class={receipt.revert_class}); a transaction that reverts in simulation "
                f"is refused here rather than paid for on chain"
            ),
            network=network,
            receipt=receipt,
            predicted_units=receipt.units_consumed,
        )

    # 5. THE BINDING, at `exact`. A structural binding is blockhash-blind, so accepting one
    #    here would approve a message carrying a blockhash nobody simulated.
    handoff = verify_handoff(
        subject, receipt, require="exact", expected_network=network, encoding="base64"
    )
    if not handoff.approved or handoff.transaction_base64 is None:
        return PurchaseRefused(
            code="binding-refused",
            reason=f"the receipt does not attest these exact bytes: {handoff.reason}",
            network=network,
            receipt=receipt,
            predicted_units=receipt.units_consumed,
        )

    # 6+7. AUTHORISE AND SIGN — ONE consultation, not two.
    #
    #  The gate RESERVES budget when it approves. This loop used to authorise on its own
    #  account and then hand the signer a gate that asked again, so one purchase wrote two
    #  ledger rows and silently halved every rolling cap. A wrapper that replayed the first
    #  answer to the second call hid that, but the budget was still asked twice and any
    #  caller wiring the gate itself would have reintroduced it.
    #
    #  The signer already computes the verdict, and it is the consultation that MATTERS —
    #  it is the one a signature cannot be produced without. So it is the only one, and it
    #  now publishes its answer on the result and on the refusal.
    current_slot = _current_slot(call, rpc_url)
    try:
        signed = signer.sign(handoff, receipt=receipt, current_slot=current_slot)
    except SignerRefused as refusal:
        # A spend refusal keeps its own typed code and carries the gate's finer verdict;
        # every other refusal is the signer's own and has no spend decision to report.
        spend_refusal = refusal.code == "spend-not-authorized"
        return PurchaseRefused(
            code="spend-refused" if spend_refusal else "signer-refused",
            reason=f"[{refusal.code}] {refusal.reason}",
            network=network,
            receipt=receipt,
            verdict=refusal.verdict,
            predicted_units=receipt.units_consumed,
        )
    verdict = signed.spend_verdict
    if verdict is None:
        # Unreachable while the signer always gates — an unconfigured gate is itself a
        # refusal there. Kept as a refusal rather than a cast: a settled purchase carrying
        # no verdict would be a signature nobody can account for, and narrowing that away
        # to satisfy a type checker is how an unexplained signature becomes normal.
        return PurchaseRefused(
            code="signer-refused",
            reason=(
                "the signer produced a signature but published no spend verdict; "
                "a settled purchase must be able to say what authorised it"
            ),
            network=network,
            receipt=receipt,
            verdict=None,
            predicted_units=receipt.units_consumed,
        )

    # 8. SEND, CONFIRM, REPORT.
    signature = _send(call, rpc_url, signed.signed_transaction_base64)
    consumed = _confirm(
        call,
        rpc_url,
        signature,
        timeout_seconds=confirm_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        sleep=sleep,
        monotonic=monotonic,
    )
    return PurchaseSettled(
        signature=signature,
        network=network,
        receipt=receipt,
        verdict=verdict,
        predicted_units=receipt.units_consumed,
        consumed_units=consumed,
        blockhash=blockhash,
        last_valid_block_height=last_valid_block_height,
        priority_fee_microlamports=fee,
    )


# --------------------------------------------------------------------------------------
# Transport helpers. Everything below treats node output as untrusted.
# --------------------------------------------------------------------------------------


def _with_fresh_blockhash(built: BuiltTx, blockhash: str) -> str:
    """The built transaction, carrying ``blockhash``, byte-identical everywhere else.

    A SUBSTITUTION, NOT A RE-ASSEMBLY, and the difference is the point. Re-deriving the
    message from the builder's fields would produce a transaction we then verify and sign
    while the builder produced a different one; patching 32 bytes at the offset the LAYOUT
    dictates leaves every account, every instruction and every byte of instruction data
    exactly as the builder emitted them.

    Why patch at all: the builder stamps a blockhash from ITS OWN RPC. Against a fork that
    hash does not exist (``BlockhashNotFound``, observed), and against mainnet it is already
    seconds old when it arrives. The one node whose blockhash is guaranteed valid is the
    node we are about to submit to.

    The offset is DERIVED from the message layout, never found by searching for the old
    value — whoever chooses the blockhash would otherwise choose which 32 bytes get
    rewritten. Fails closed: if the patched bytes do not read back as ``blockhash``, this
    raises rather than returning something that will be signed.
    """
    from solders.hash import Hash

    raw = _decode(built)
    message, version = _message_of(raw)
    message_bytes = bytes(message)
    start = len(raw) - len(message_bytes)
    if start < 0 or raw[start:] != message_bytes:
        raise PurchaseTransportError(
            "the built transaction's message is not a suffix of its own bytes; refusing to "
            "patch a layout this code does not understand"
        )
    offset = start + blockhash_offset(message_bytes, version)
    try:
        replacement = bytes(Hash.from_string(blockhash))
    except Exception as exc:  # noqa: BLE001 - untrusted node output
        raise PurchaseTransportError(
            f"the RPC returned a blockhash that is not a hash ({type(exc).__name__})"
        ) from None
    if len(replacement) != 32:  # pragma: no cover - a Hash is 32 bytes by construction
        raise PurchaseTransportError("the replacement blockhash is not 32 bytes")

    patched = raw[:offset] + replacement + raw[offset + 32 :]
    if len(patched) != len(raw):  # pragma: no cover - slicing preserves length
        raise PurchaseTransportError(
            "patching the blockhash changed the message length"
        )

    # Read it back through the decoder rather than trusting the arithmetic above.
    try:
        checked, _version = _message_of(patched)
    except TxDecodeError as exc:
        raise PurchaseTransportError(
            f"the transaction stopped decoding after the blockhash was written ({exc})"
        ) from None
    if str(checked.recent_blockhash) != blockhash:
        raise PurchaseTransportError(
            "the patched transaction does not carry the blockhash that was written; "
            "refusing to simulate bytes whose blockhash is not the one intended"
        )
    return base64.b64encode(patched).decode()


def _decode(built: BuiltTx) -> bytes:
    """The built transaction as raw bytes, in whichever encoding the builder declared."""
    from .txbind import _b58decode

    if built.encoding == "base64":
        try:
            return base64.b64decode(built.tx, validate=True)
        except Exception as exc:  # noqa: BLE001 - untrusted builder output
            raise PurchaseTransportError(
                f"the builder returned base64 that does not decode ({type(exc).__name__})"
            ) from None
    if built.encoding == "base58":
        try:
            return _b58decode(built.tx)
        except Exception as exc:  # noqa: BLE001 - untrusted builder output
            raise PurchaseTransportError(
                f"the builder returned base58 that does not decode ({type(exc).__name__})"
            ) from None
    raise PurchaseTransportError(
        f"the builder declared an encoding this loop does not read ({built.encoding!r})"
    )


def _current_slot(call: RpcCall, rpc_url: str) -> int:
    """The node's current slot. The signer's age bound is meaningless without it."""
    response = call(rpc_url, "getSlot", [{"commitment": "processed"}])
    slot = response.get("result")
    if isinstance(slot, bool) or not isinstance(slot, int):
        raise PurchaseTransportError(
            "the RPC returned no usable slot, so the receipt's age cannot be read"
        )
    return slot


def _send(call: RpcCall, rpc_url: str, signed_transaction_base64: str) -> str:
    """Broadcast, and return the signature. A rejected send raises — it is not a verdict."""
    reply = call(
        rpc_url,
        "sendTransaction",
        [
            signed_transaction_base64,
            {
                "encoding": "base64",
                "skipPreflight": False,
                # The node re-forwards until the blockhash expires. This was 3, and at 3
                # a zero-priority-fee transaction is leader-luck: 5 of 12 broadcasts on
                # 2026-08-31 expired unspent. The blockhash window (~60s) is the real
                # bound; a retry budget below it just drops transactions early.
                "maxRetries": 40,
                # WITHOUT THIS THE PREFLIGHT RUNS AT `finalized`, which is the RPC default
                # and is STRICTER than the commitment the blockhash was issued at. That
                # combination fails 100% of the time with `BlockhashNotFound` — the
                # simulation passes (it runs at `processed`) and then the broadcast is
                # rejected by a node that has not seen the block yet.
                "preflightCommitment": RPC_COMMITMENT,
            },
        ],
    )
    if "error" in reply:
        raise PurchaseTransportError(
            f"the node rejected the broadcast: {reply.get('error')}"
        )
    signature = reply.get("result")
    if not isinstance(signature, str) or not signature:
        raise PurchaseTransportError(
            "the node accepted the send but returned no signature"
        )
    return signature


def _confirm(
    call: RpcCall,
    rpc_url: str,
    signature: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> int | None:
    """Wait for confirmation, then report what the chain actually charged.

    Returns the consumed compute units, or ``None`` when the node confirmed the transaction
    but reported no figure. ``None`` means NOT REPORTED — never zero, and never quietly
    substituted with the prediction, which would make the two columns agree by construction
    and destroy the only comparison worth printing.
    """
    deadline = monotonic() + timeout_seconds
    while True:
        response = call(rpc_url, "getSignatureStatuses", [[signature]])
        values = ((response.get("result") or {}).get("value")) or []
        status = values[0] if values and isinstance(values[0], dict) else None
        if status is not None:
            if status.get("err") is not None:
                raise PurchaseTransportError(
                    f"the transaction landed and failed on chain: {status.get('err')}",
                    signature=signature,
                )
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return _consumed_units(call, rpc_url, signature)
        if monotonic() >= deadline:
            raise PurchaseTransportError(
                f"the transaction was broadcast but not confirmed within "
                f"{timeout_seconds:g}s; look it up rather than assuming either outcome",
                signature=signature,
            )
        sleep(poll_interval_seconds)


def _consumed_units(call: RpcCall, rpc_url: str, signature: str) -> int | None:
    """What the chain charged, read from the landed transaction. ``None`` if unreported."""
    response = call(
        rpc_url,
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
    meta = ((response.get("result") or {}).get("meta")) or {}
    units = meta.get("computeUnitsConsumed")
    if isinstance(units, bool) or not isinstance(units, int):
        return None
    return units
