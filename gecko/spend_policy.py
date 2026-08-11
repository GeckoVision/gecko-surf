"""The spend policy — AUTHORIZATION, the other half of the signing decision.

A receipt is **verification**: these bytes will land and burn this much CU. It says
nothing about whether the human ever wanted them to. A cap is **authorization**: you may
spend up to X. It says nothing about whether *this* transaction does what was asked.
Each is worthless as a substitute for the other — verification without authorization
signs a perfectly-simulated transfer of everything to an attacker's address, and
authorization without verification blesses a transaction that reverts, or one that does
something the limit does not measure.

**Both predicates must pass. Neither may be inferred from the other. A denial from either
is a refusal that yields no bytes.** This module is the second predicate only. It is not a
gate on correctness, it does not read a Receipt's binding, network or status, and
:mod:`gecko.signer` refusing correctly does not make this check optional. That separation
is enforced, not merely stated: ``tests/test_spend_policy.py`` walks this file's AST and
fails if any verification field is read here.

FOUR CAPS.

1. **Per transaction.** The most a single transaction may move, in lamports.
2. **Cumulative / velocity.** A rolling hour, a rolling day, and a maximum transaction
   count per day. This is the cap the OKX ``singleTxLimit`` lesson actually demands: a
   per-transaction cap of X is defeated by N transactions of X. Today's ``--max-usdc`` is
   a wallet-balance ceiling, and its second check bounds only the current process's
   ``--count`` — a loop counter, not a velocity cap. The cumulative form here is lifted
   from ``examples/txline_sharp_agent/wallet_sim.py:87-90``
   (``_spent + amount > cap -> PolicyViolation``), which already had the right shape with
   no keys and no chain, and extended to a rolling window persisted ACROSS PROCESSES.
3. **Allowlisted programs AND INSTRUCTIONS.** Not "which program" — "which instruction of
   which program". A program allowlist that permits every instruction of a DEX permits
   ``setAuthority``-shaped instructions in the same breath.
4. **Allowlisted destinations.** Which accounts this transaction may write.

THE VELOCITY COUNTER IS **ADVISORY**, AND SAYS SO. It is cumulative across processes,
which a loop counter is not, and that is a real improvement that a test falsifies offline.
It is still not a control: the ledger file is writable by the very process it bounds, so a
compromised agent resets its own budget. It ships labelled ADVISORY until the counter lives
somewhere the signer — not the agent — controls, and no docstring, demo line or outward
sentence here may call it a guarantee. Every other cap in this module is a control; this
one is a speed bump with an honest name.

THREE PROPERTIES THAT MAKE THIS A POLICY RATHER THAN A SUGGESTION.

* **Evaluated over the DECODED MESSAGE, never over stated intent.** Program ids,
  instruction discriminators, writable accounts and the receipt's own ``sol_delta`` are
  facts about the bytes; an agent's description of them is attacker-influenceable, so
  :meth:`SpendPolicyGate.authorize` has no parameter through which one could arrive. We
  decode locally, in :func:`gecko.txbind.decode_message`, and **an RPC read may not become
  an input to this gate** — ``txbind.py:68-73`` already ruled on that, and the reason is
  that a node which is unreachable or lying would otherwise decide a signature.

  THAT RULE CONSTRAINS CAP 4, AND HERE IS HOW IT IS HANDLED RATHER THAN QUIETLY WORKED
  AROUND. "Destination" for an SPL transfer means the *owner* of a token account, and
  resolving an owner requires reading that account — an RPC. So this gate does not resolve
  owners and does not pretend to. It allowlists the **writable account keys in the message
  itself**, which for an SPL transfer is the token ACCOUNT address, not its owner. The
  human authors the allowlist in that same vocabulary: the exact account addresses this
  agent may write. The consequence is deliberate and stated in both directions —
  (i) it OVER-APPROXIMATES, because every writable account is treated as a potential
  destination, so a DEX route requires the author to enumerate its writable accounts and
  an unenumerated one refuses; (ii) it cannot express "any token account owned by Alice",
  and a policy that needs that shape must wait for a resolution step whose trust root is
  not a single unauthenticated node. Over-refusal is the direction we accept.

* **Absent policy = REFUSE.** ``policy.py:28-29`` documents "an unset field is a no-op",
  which is right for a governed API session and wrong for a signer. Rather than change
  :class:`~gecko.policy.AgentPolicy`'s meaning under its current consumers, this is a
  DISTINCT TYPE with an explicit ``authorized`` flag that defaults to ``False`` and caps
  that default to ``None`` meaning *refuse*, not *skip*. A default-constructed
  :class:`SpendPolicy` authorizes nothing, and so does a :class:`SpendPolicyGate` built
  with no policy at all.

* **Authored by the human out of band; the agent cannot widen it.** ``authorize`` accepts
  an ``agent_supplied_policy`` argument for exactly one reason: to REFUSE it by name.
  It is never merged, never intersected, and rejected even when it is narrower — a policy
  that can be replaced downward can be replaced upward, and "is this narrower?" would be a
  comparison we perform over attacker-influenced input.

**AN UNDECODABLE ANYTHING REFUSES**, and the refusal names which one fired: an undecodable
transaction, an instruction with no discriminator, an unresolvable amount, an unreadable
ledger, and an absent policy are five different answers and never render as the same
string. Nothing here returns "no violation found" because it could not read the input —
which is exactly the shape ``risk.py:622-623`` and ``risk.py:723-725`` take, correctly for
a scorer that must not over-block and catastrophically for a signing path. Nothing from
that signal layer is imported, lifted or re-derived here, and a test walks this file's
imports to keep it that way.

This module holds no key, signs nothing, sends nothing, and stores no transaction. The
ledger records two numbers per entry — a timestamp and a lamport amount — and never an
address, a binding or a payload.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from .simulate import Receipt
from .txbind import (
    DISCRIMINATOR_CEILING,
    DecodedMessage,
    TxDecodeError,
    UnresolvedLookupError,
    decode_message,
)

__all__ = [
    "AdvisorySpendLedger",
    "AllowedInstruction",
    "FileSpendLedger",
    "InMemorySpendLedger",
    "LedgerError",
    "SpendPolicy",
    "SpendPolicyGate",
    "SpendRefusalCode",
    "SpendVerdict",
    "VelocityDecision",
    "VelocityLimits",
]

#: One rolling hour and one rolling day, in seconds. Windows, not calendar buckets: a
#: calendar bucket hands an agent a fresh budget at midnight and lets it spend two days'
#: worth in two minutes across the boundary.
HOUR_SECONDS = 3_600.0
DAY_SECONDS = 86_400.0

#: Every reason this gate declines, as a closed vocabulary. "Could not be read" and "was
#: read and was fine" are different answers and must never share a code.
SpendRefusalCode = Literal[
    "no-policy",
    "policy-not-authorized",
    "policy-incomplete",
    "agent-supplied-policy",
    "velocity-ledger-unavailable",
    "undecodable-transaction",
    "undecodable-instruction",
    "program-not-allowlisted",
    "instruction-not-allowlisted",
    "destination-not-allowlisted",
    "amount-unresolvable",
    "over-per-transaction-cap",
    "over-hourly-cap",
    "over-daily-cap",
    "over-daily-transaction-count",
    "ledger-unreadable",
    "predicate-raised",
]


class LedgerError(Exception):
    """The advisory counter could not be read or written. Never a silent zero."""


@dataclass(frozen=True)
class AllowedInstruction:
    """One (program, instruction) pair the human authorised. Hashable, so it lives in a set.

    ``discriminator`` is matched as a PREFIX of the instruction's leading data bytes, which
    is how program dispatch actually works: Anchor routes on eight bytes, SPL Token on one,
    System on four. The author declares the width their program uses, so a one-byte entry
    against an eight-byte-dispatch program is a broad entry the author chose — not a
    default this module picked for them.

    An empty discriminator is refused at construction. It would match every instruction of
    the program, which is precisely the program-wide allowlist cap 3 exists to prevent, and
    it would do so while looking like a specific entry.
    """

    program_id: str
    discriminator: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.discriminator, (bytes, bytearray)):
            raise ValueError("discriminator must be bytes")
        if not self.discriminator:
            raise ValueError(
                "an empty discriminator allowlists every instruction of the program; "
                "name the selector explicitly"
            )
        if len(self.discriminator) > DISCRIMINATOR_CEILING:
            raise ValueError(
                f"discriminator longer than {DISCRIMINATOR_CEILING} bytes; only the "
                f"leading selector is decoded, so a longer entry could never match"
            )
        object.__setattr__(self, "discriminator", bytes(self.discriminator))
        if not self.program_id or not isinstance(self.program_id, str):
            raise ValueError("program_id must be a non-empty string")


@dataclass(frozen=True)
class SpendPolicy:
    """What the human authorised, out of band. Every default is a REFUSAL.

    This is deliberately NOT :class:`~gecko.policy.AgentPolicy` extended. That record's
    documented contract is "an unset field is a no-op", which its current consumers rely
    on and which is correct for a governed API session. Inverting it there would change
    their behaviour silently; inverting it here, in a distinct type, changes nothing for
    anyone else.

    Every cap is required. There is no "cap I did not think about" that is therefore
    unlimited: :meth:`missing_fields` names each unset one and the gate refuses until all
    of them are authored.
    """

    #: Explicitly authored by a human, out of band. Never inferred, never widened by an
    #: agent, never true by default.
    authorized: bool = False
    per_transaction_cap_lamports: int | None = None
    hourly_cap_lamports: int | None = None
    daily_cap_lamports: int | None = None
    max_transactions_per_day: int | None = None
    allowed_instructions: frozenset[AllowedInstruction] = field(
        default_factory=frozenset
    )
    #: The exact account addresses this agent may WRITE, besides the fee payer. See the
    #: module docstring for why these are account keys and not resolved token-account
    #: owners: resolving an owner needs an RPC, and an RPC may not decide a signature.
    allowed_destinations: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "allowed_instructions", frozenset(self.allowed_instructions)
        )
        object.__setattr__(
            self, "allowed_destinations", frozenset(self.allowed_destinations)
        )
        for name in (
            "per_transaction_cap_lamports",
            "hourly_cap_lamports",
            "daily_cap_lamports",
            "max_transactions_per_day",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer or None")

    def missing_fields(self) -> tuple[str, ...]:
        """Which caps were never authored. Empty means the policy is complete."""
        missing = [
            name
            for name in (
                "per_transaction_cap_lamports",
                "hourly_cap_lamports",
                "daily_cap_lamports",
                "max_transactions_per_day",
            )
            if getattr(self, name) is None
        ]
        if not self.allowed_instructions:
            missing.append("allowed_instructions")
        if not self.allowed_destinations:
            missing.append("allowed_destinations")
        return tuple(missing)


@dataclass(frozen=True)
class SpendVerdict:
    """Authorised, or refused with the code that says which check fired.

    Carries no transaction and no bytes, by construction: this predicate's job is to deny,
    and a denial that hands back something signable is not a denial. The bytes stay with
    :class:`~gecko.handoff.SignerHandoff`, where the verification verdict is welded to them.
    """

    authorized: bool
    reason: str
    code: SpendRefusalCode | None = None
    #: What this transaction moves OUT, in lamports, once it could be resolved.
    outflow_lamports: int | None = None


@dataclass(frozen=True)
class VelocityLimits:
    """The three rolling bounds, passed to the ADVISORY ledger as one value."""

    hourly_lamports: int
    daily_lamports: int
    max_transactions_per_day: int


@dataclass(frozen=True)
class VelocityDecision:
    """The ADVISORY ledger's answer. ``within`` false means nothing was recorded."""

    within: bool
    reason: str
    code: SpendRefusalCode | None = None


@runtime_checkable
class AdvisorySpendLedger(Protocol):
    """The cumulative counter. ADVISORY — see the module docstring for why.

    One method on purpose. Splitting "read the window" from "record the spend" opens a gap
    between the check and the commit that two concurrent agents drive a truck through; a
    single ``reserve`` lets an implementation hold one lock across both.
    """

    def reserve(
        self, *, at: float, lamports: int, limits: VelocityLimits
    ) -> VelocityDecision:
        """Check the rolling windows and, if within all three, record the spend.

        Raises :class:`LedgerError` if the record cannot be read. It must never treat an
        unreadable ledger as an empty one — that turns a corrupt file into an unlimited
        budget.
        """
        ...


def _window_decision(
    entries: list[tuple[float, int]], at: float, lamports: int, limits: VelocityLimits
) -> VelocityDecision:
    """The cumulative check, lifted from ``wallet_sim.py:87-90`` and given windows.

    ``_spent + amount > cap`` was already the right shape with no keys and no chain. What
    it lacked was a clock: a single running total never forgets and never refreshes, so it
    is a lifetime budget rather than a rate. Summing a rolling slice of timestamped entries
    is the same inequality applied to a window.
    """
    hourly = sum(amount for when, amount in entries if when > at - HOUR_SECONDS)
    daily = sum(amount for when, amount in entries if when > at - DAY_SECONDS)
    daily_count = sum(1 for when, _ in entries if when > at - DAY_SECONDS)

    if hourly + lamports > limits.hourly_lamports:
        return VelocityDecision(
            within=False,
            reason=(
                f"{hourly + lamports} lamports in the last hour would pass the "
                f"{limits.hourly_lamports} hourly bound"
            ),
            code="over-hourly-cap",
        )
    if daily + lamports > limits.daily_lamports:
        return VelocityDecision(
            within=False,
            reason=(
                f"{daily + lamports} lamports in the last day would pass the "
                f"{limits.daily_lamports} daily bound"
            ),
            code="over-daily-cap",
        )
    if daily_count + 1 > limits.max_transactions_per_day:
        return VelocityDecision(
            within=False,
            reason=(
                f"{daily_count + 1} transactions in the last day would pass the "
                f"{limits.max_transactions_per_day} count bound"
            ),
            code="over-daily-transaction-count",
        )
    return VelocityDecision(within=True, reason="within all three rolling bounds")


@dataclass
class InMemorySpendLedger:
    """An ADVISORY ledger for one process. For tests and for a single short-lived run.

    Explicitly NOT the cross-process form: a second process starts at zero. It exists so
    the policy is falsifiable offline without touching a filesystem, and naming it
    ``InMemory`` is the whole warning.
    """

    _entries: list[tuple[float, int]] = field(default_factory=list, init=False)

    def reserve(
        self, *, at: float, lamports: int, limits: VelocityLimits
    ) -> VelocityDecision:
        self._entries = [
            when_amount
            for when_amount in self._entries
            if when_amount[0] > at - DAY_SECONDS
        ]
        decision = _window_decision(self._entries, at, lamports, limits)
        if decision.within:
            self._entries.append((at, lamports))
        return decision


@dataclass(frozen=True)
class FileSpendLedger:
    """An ADVISORY ledger persisted ACROSS PROCESSES, as JSON lines under an exclusive lock.

    Cross-process is the property the current ``--count`` loop counter lacks: that counter
    bounds one invocation, so an agent that can start a second process has no bound at all.
    The whole read-check-append runs while holding ``flock(LOCK_EX)``, so two agents racing
    the same window serialise rather than both seeing the pre-spend total.

    IT IS STILL NOT A CONTROL. The file is writable by the process it bounds. An agent that
    can call this can also truncate it, and no lock helps with that. Closing it means moving
    the counter somewhere the signer owns — see the module docstring.

    Two fields per entry, a timestamp and a lamport amount. No address, no binding, no
    payload: a budget needs the sum, not the history.
    """

    path: str

    def reserve(
        self, *, at: float, lamports: int, limits: VelocityLimits
    ) -> VelocityDecision:
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        # ``a+`` creates without truncating, which matters: an ``w`` mode here would zero
        # the budget of any concurrent holder the instant this one opened the file.
        with open(self.path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                entries = _read_entries(handle.read(), self.path)
                kept = [entry for entry in entries if entry[0] > at - DAY_SECONDS]
                decision = _window_decision(kept, at, lamports, limits)
                if decision.within:
                    kept.append((at, lamports))
                if len(kept) != len(entries) or decision.within:
                    handle.seek(0)
                    handle.truncate()
                    for when, amount in kept:
                        handle.write(
                            json.dumps({"at": when, "lamports": amount}) + "\n"
                        )
                    handle.flush()
                    os.fsync(handle.fileno())
                return decision
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_entries(text: str, where: str) -> list[tuple[float, int]]:
    """Parse the ledger, refusing anything it cannot fully account for.

    A malformed line is a :class:`LedgerError`, never a skipped line. Skipping would make a
    corrupted or truncated ledger read as a smaller total, which is the fail-open shaped
    exactly like a fresh start.
    """
    entries: list[tuple[float, int]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise LedgerError(
                f"spend ledger line {number} is not readable JSON; refusing to treat an "
                f"unreadable budget as an empty one"
            ) from exc
        if not isinstance(record, dict):
            raise LedgerError(f"spend ledger line {number} is not an object")
        when = record.get("at")
        amount = record.get("lamports")
        if isinstance(when, bool) or not isinstance(when, (int, float)):
            raise LedgerError(f"spend ledger line {number} has no usable timestamp")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise LedgerError(f"spend ledger line {number} has no usable amount")
        entries.append((float(when), amount))
    del where
    return entries


@dataclass(frozen=True)
class _Subject:
    """The decoded facts one authorization decision is taken over. No intent, by design."""

    policy: SpendPolicy
    decoded: DecodedMessage
    outflow_lamports: int


def _refuse(code: SpendRefusalCode, reason: str, **extra: Any) -> SpendVerdict:
    return SpendVerdict(authorized=False, reason=reason, code=code, **extra)


def _check_instructions(subject: _Subject) -> SpendVerdict | None:
    """Cap 3 — (program, instruction), never program alone.

    An instruction with no discriminator is refused rather than matched loosely: it names
    no selector, so it cannot be allowlisted, so it is refused. That is the general rule
    from the module docstring applied at its narrowest point.
    """
    allowed = subject.policy.allowed_instructions
    programs = {entry.program_id for entry in allowed}
    for position, one in enumerate(subject.decoded.instructions):
        if not one.discriminator:
            return _refuse(
                "undecodable-instruction",
                f"instruction {position} carries no data, so it names no selector; an "
                f"instruction that cannot be decoded cannot be allowlisted",
            )
        if one.program_id not in programs:
            return _refuse(
                "program-not-allowlisted",
                f"instruction {position} calls {one.program_id}, which the policy does "
                f"not allowlist",
            )
        if not any(
            entry.program_id == one.program_id
            and one.discriminator[: len(entry.discriminator)] == entry.discriminator
            for entry in allowed
        ):
            return _refuse(
                "instruction-not-allowlisted",
                f"instruction {position} on allowlisted program {one.program_id} has "
                f"selector {one.discriminator.hex()}, which the policy does not "
                f"allowlist; a program allowlist is not an instruction allowlist",
            )
    return None


def _check_destinations(subject: _Subject) -> SpendVerdict | None:
    """Cap 4 — every writable account besides the fee payer must be allowlisted.

    The fee payer is exempt here because it is checked elsewhere and more strictly: the
    signer refuses bytes whose fee payer is not its own account. Exempting it twice would
    be an exemption; exempting it once, where the stronger check lives, is a division of
    labour.
    """
    payer = subject.decoded.fee_payer
    unlisted = sorted(
        key
        for key in subject.decoded.writable_accounts
        if key != payer and key not in subject.policy.allowed_destinations
    )
    if unlisted:
        return _refuse(
            "destination-not-allowlisted",
            f"the transaction writes {', '.join(unlisted)}, which the policy does not "
            f"allowlist as a destination",
        )
    return None


def _check_per_transaction_cap(subject: _Subject) -> SpendVerdict | None:
    """Cap 1 — the most a single transaction may move."""
    cap = subject.policy.per_transaction_cap_lamports
    if cap is None:  # pragma: no cover - completeness is checked before the predicates
        return _refuse("policy-incomplete", "per_transaction_cap_lamports is unset")
    if subject.outflow_lamports > cap:
        return _refuse(
            "over-per-transaction-cap",
            f"this transaction moves {subject.outflow_lamports} lamports out, past the "
            f"{cap} per-transaction bound",
            outflow_lamports=subject.outflow_lamports,
        )
    return None


def _run_predicate(predicate: Any, subject: _Subject) -> SpendVerdict | None:  # noqa: ANN401
    """Run one predicate. A RAISE IS A REFUSAL.

    This is the deliberate opposite of ``risk.py:723-725``, which crash-contains a raising
    signal into "no reason" — correct for a scorer that must not over-block on a bug, and
    an ALLOW on a signing path. Here a predicate that cannot answer has not answered, and
    an unanswered authorization question is a no.
    """
    try:
        return predicate(subject)
    except Exception as exc:  # noqa: BLE001 - any predicate fault is a refusal
        return _refuse(
            "predicate-raised",
            f"the {getattr(predicate, '__name__', 'policy')} predicate raised "
            f"{type(exc).__name__}; a predicate that could not answer has not approved",
        )


@dataclass(frozen=True)
class SpendPolicyGate:
    """Is this transaction one the human authorised? Total, offline, and never permissive.

    Constructs cleanly with nothing configured and in that state authorizes nothing.
    ``authorize`` never raises: a gate that throws is a gate somebody wraps in a
    ``try/except``, and here that is safe to guarantee because the refusal object carries
    nothing signable either way.
    """

    #: ``None`` means no policy was authored, which means nothing is authorised.
    policy: SpendPolicy | None = None
    #: The ADVISORY cumulative counter. ``None`` is a refusal, not an unmetered pass.
    ledger: AdvisorySpendLedger | None = None

    def authorize(
        self,
        transaction_base64: str | bytes,
        receipt: Receipt,
        *,
        now: float | None = None,
        agent_supplied_policy: SpendPolicy | None = None,
    ) -> SpendVerdict:
        """Authorise these BYTES against the authored policy, or refuse naming the check.

        ``transaction_base64`` is the subject and is decoded here. There is no parameter
        through which a description, an intent, a tool name or a summary could arrive:
        every one of those is written upstream of an attacker-influenceable hop, and a
        policy evaluated over them authorises the sentence rather than the transaction.

        ``receipt`` supplies ``sol_delta`` and nothing else. Its binding, strength, status
        and network belong to the OTHER predicate; reading them here would let verification
        stand in for authorization, which is the substitution this whole module exists to
        prevent.

        ``agent_supplied_policy`` exists to be refused. It is never merged and never
        intersected, and a narrower one is refused with the same code as a wider one.

        ``now`` is injected so the rolling windows are falsifiable offline. It defaults to
        wall-clock time; it is never read from the transaction.
        """
        moment = time.time() if now is None else now
        try:
            return self._authorize(
                transaction_base64, receipt, moment, agent_supplied_policy
            )
        except Exception as exc:  # noqa: BLE001 - a gate that cannot decide has refused
            return _refuse(
                "predicate-raised",
                f"the spend policy could not be evaluated ({type(exc).__name__}); "
                f"'we could not check' is never 'authorised'",
            )

    def _authorize(
        self,
        transaction_base64: str | bytes,
        receipt: Receipt,
        moment: float,
        agent_supplied_policy: SpendPolicy | None,
    ) -> SpendVerdict:
        if agent_supplied_policy is not None:
            return _refuse(
                "agent-supplied-policy",
                "a policy arrived with the request; the policy is authored by the human "
                "out of band and an agent-supplied one is rejected, never merged — "
                "including a narrower one",
            )

        policy = self.policy
        if policy is None:
            return _refuse(
                "no-policy",
                "no spend policy is configured; an unauthored policy authorises nothing, "
                "which is the deliberate inversion of a governed session's opt-in fields",
            )
        if not policy.authorized:
            return _refuse(
                "policy-not-authorized",
                "this policy was never authorised by a human out of band; an unset "
                "authorization is a refusal here, not a no-op",
            )
        missing = policy.missing_fields()
        if missing:
            return _refuse(
                "policy-incomplete",
                f"the policy leaves {', '.join(missing)} unset; there is no cap that is "
                f"unlimited because nobody thought about it",
            )
        ledger = self.ledger
        if ledger is None:
            return _refuse(
                "velocity-ledger-unavailable",
                "no cumulative counter is configured, so the rolling caps cannot be "
                "evaluated; an uncountable budget is not an unlimited one",
            )

        try:
            decoded = decode_message(transaction_base64)
        except UnresolvedLookupError as exc:
            return _refuse(
                "undecodable-transaction",
                f"the message loads accounts from an address lookup table, so the bytes "
                f"do not name what it writes ({exc.__class__.__name__}); resolving them "
                f"would make a node an input to this decision",
            )
        except TxDecodeError:
            return _refuse(
                "undecodable-transaction",
                "the transaction could not be decoded, so there is nothing to evaluate "
                "the policy over; an unreadable transaction is refused, not skipped",
            )

        delta = receipt.sol_delta
        if delta is None or isinstance(delta, bool):
            return _refuse(
                "amount-unresolvable",
                "the receipt resolves no lamport delta, so how much this moves cannot be "
                "read; simulate with the paying account tracked and try again",
            )
        outflow = -delta if delta < 0 else 0

        subject = _Subject(policy=policy, decoded=decoded, outflow_lamports=outflow)
        for predicate in (
            _check_instructions,
            _check_destinations,
            _check_per_transaction_cap,
        ):
            verdict = _run_predicate(predicate, subject)
            if verdict is not None:
                return verdict

        # The cumulative cap runs LAST and is the only predicate with a side effect. A
        # transaction refused by any earlier check must not spend velocity budget: a
        # counter that ticks on refusals is a denial-of-service an attacker drives.
        return _reserve(ledger, policy, outflow, moment)


def _reserve(
    ledger: AdvisorySpendLedger,
    policy: SpendPolicy,
    outflow: int,
    moment: float,
) -> SpendVerdict:
    """Cap 2 — the ADVISORY rolling windows, checked and committed under one lock."""
    limits = VelocityLimits(
        hourly_lamports=policy.hourly_cap_lamports or 0,
        daily_lamports=policy.daily_cap_lamports or 0,
        max_transactions_per_day=policy.max_transactions_per_day or 0,
    )
    try:
        decision = ledger.reserve(at=moment, lamports=outflow, limits=limits)
    except LedgerError as exc:
        return _refuse(
            "ledger-unreadable",
            f"the cumulative counter could not be read ({exc}); an unreadable budget is "
            f"not an empty one",
        )
    if not decision.within:
        return SpendVerdict(
            authorized=False,
            reason=decision.reason,
            code=decision.code,
            outflow_lamports=outflow,
        )
    return SpendVerdict(
        authorized=True,
        reason=(
            "within the per-transaction cap, the allowlisted (program, instruction) "
            "pairs, the allowlisted destinations, and the ADVISORY rolling bounds"
        ),
        outflow_lamports=outflow,
    )
