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

FIVE CAPS.

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
5. **Per-mint token caps, in that mint's OWN raw base units.** Caps 1 and 2 are
   lamport-denominated, and a USDC purchase moves no lamports beyond the fee: the drain
   this seam was built for reached a lamport cap as an outflow of ~5,000 and was
   authorised. **A raw SPL amount is never assigned to a lamport field and never summed
   into the lamport windows.** 25 USDC is 25,000,000 raw — a thousandth of the lamport
   number it would be compared against at 6 decimals, and a rounding error at 0 or 2 —
   so one fold makes every cap clear a large drain. Each mint therefore carries its own
   per-transaction/hourly/daily bounds AND the decimals the human wrote them at: a cap in
   raw units means nothing without the scale, so a mint observed at a different scale
   REFUSES rather than being converted (converting would trust a node's decimals over the
   human's). A mint absent from the map refuses with ``mint-not-allowlisted`` — never
   passes unmeasured.

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

**A TOKEN LEG WE CANNOT READ IS A REFUSAL, NOT A ZERO.** ``Receipt.token_delta`` has
three states and they are not interchangeable: ``None`` is NOT TRACKED, a ``measured``
report with no movements is an OBSERVED zero, and an ``unmeasurable`` report is one we
will not put a number on. The last maps onto ``amount-unresolvable``, the same answer an
unreadable ``sol_delta`` earns. ``None`` earns ``token-leg-not-measured`` — with ONE
derivation, not an exemption: if every program the message invokes is one that
**cannot move an SPL token at all** (:data:`_PROGRAMS_THAT_CANNOT_MOVE_TOKENS`, today the
System and ComputeBudget programs, neither of which can CPI into a token program), then
there is no token leg to measure and its absence is a fact rather than a blind spot.
Anything else — including the Memo program, which is almost certainly inert and is
deliberately not on a list whose every entry is a claim that can rot — is token-capable
and refuses. **The consequence is deliberate: a simulation that carries no token
balances makes every token-capable transaction refuse here.** Current mainnet DOES carry
them (measured 2026-08-12, agave 4.2.0-rc.1) — which is why real purchases authorize —
while surfpool nulls them, which is why a fork's token leg refuses today. That is the
intended direction. The alternative is a gate that authorises a drain it cannot see.

RESIDUALS, NAMED RATHER THAN LEFT FOR A READER TO DISCOVER.

* **A retry that refreshes its blockhash still double-reserves.** The identical-bytes
  retry no longer does: :meth:`SpendPolicyGate.authorize` derives an ``exact`` message
  binding (``_dedupe_key``) and the ledger treats a repeat within ``DEDUPE_SECONDS`` as
  the replay it is. What made that possible was a founder ruling (D-B) permitting a
  one-way digest into the ledger; the previous text here said it was impossible because
  "a binding may not enter the ledger", and that premise is what changed, not the
  reasoning built on it.

  The remaining case is a retry that had to re-quote — a backend refusal handled minutes
  later, past blockhash validity. Its bytes differ, so its digest differs, and it reserves
  again. This is NOT closed, and closing it with a ``structural`` binding would be a
  regression rather than a fix: structural digests are stable across re-quoting, so two
  transfers a human deliberately made twice would collapse into one reservation and the
  second would spend no budget. Over-counting a rare re-quoted retry errs toward refusing;
  under-counting a repeated transfer errs toward a cap bypass. The direction is chosen.
* **Outflows from accounts the fee payer does not own are not charged.** The per-mint cap
  is applied to outflows whose owner is the message's fee payer — the account the signer
  separately proved it controls. A DEX vault paying tokens INTO us is an outflow of some
  other owner and must not be charged, which is why the filter exists; the cost is that a
  transfer moving tokens out of a third party's account under a delegation is not bounded
  here. Token-2022's permanent-delegate mints are refused outright upstream; a classic
  SPL delegation is not, and this is where that shows.
* **The velocity counter is ADVISORY**, per the note above, in every denomination.

This module holds no key, signs nothing, sends nothing, and stores no transaction. A
ledger row carries a timestamp and an amount, and — for a token row — the MINT, which is
a public program-surface identifier. It never carries an owner, a token-account address
or a payload, and :func:`_read_entries` refuses any row it cannot fully account for
rather than skipping it.

A lamport row ALSO carries a one-way ``exact`` message binding for as long as
``DEDUPE_SECONDS``, which is what makes the reservation idempotent. That is a deliberate
narrowing of the sentence above, granted by founder ruling D-B, and it is bounded on
purpose: the digest is erased on the next reserve after the window, so the file is a list
of amounts for a day and a list of WHICH transactions for fifteen minutes. It is still
never a payload and never a party — a hash does not name a counterparty.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

from .simulate import Receipt, TokenDeltaUnmeasurable, TokenOutflow
from .txbind import (
    DISCRIMINATOR_CEILING,
    DecodedMessage,
    TxDecodeError,
    UnresolvedLookupError,
    decode_message,
    message_binding,
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
    "TokenCap",
    "TokenCaps",
    "TokenSpend",
    "TokenVelocity",
    "VelocityDecision",
    "VelocityLimits",
]

#: One rolling hour and one rolling day, in seconds. Windows, not calendar buckets: a
#: calendar bucket hands an agent a fresh budget at midnight and lets it spend two days'
#: worth in two minutes across the boundary.
HOUR_SECONDS = 3_600.0
DAY_SECONDS = 86_400.0

#: How long an identical retry is recognised as the SAME transaction rather than a second
#: one. Deliberately far shorter than ``DAY_SECONDS``, and that gap is the point.
#:
#: A dedupe key only matches BYTE-IDENTICAL messages, and byte-identical means the same
#: blockhash, which a cluster accepts for roughly 60-90 seconds. After that a retry must
#: carry a fresh blockhash, so its digest differs and no window length would have matched
#: it. A longer window therefore buys no additional deduplication — it only keeps the
#: correlatable part of the row alive longer.
#:
#: So the digest is dropped from a row once this elapses (see ``_expire_digests``) while
#: the row itself survives for the full day. That is what keeps the concession D-B granted
#: as narrow as it can be: the budget file remains a list of amounts and times for 24
#: hours, and is a list of WHICH transactions for fifteen minutes.
DEDUPE_SECONDS = 900.0

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
    # Cap 5 — the token leg. Each one is a DIFFERENT answer: "the mint was never
    # authored", "the scale disagrees", "it moved more than the cap", and "we could not
    # see the token leg at all" must never render as the same string.
    "mint-not-allowlisted",
    "token-decimals-mismatch",
    "over-per-transaction-token-cap",
    "over-hourly-token-cap",
    "over-daily-token-cap",
    "token-leg-not-measured",
]

#: Programs that CANNOT move an SPL token, so a message built only from them has no token
#: leg to measure and ``token_delta is None`` is a fact rather than a blind spot.
#:
#: This is a derivation, not an exemption list, and it is kept at two entries because
#: every entry is a claim about a program's behaviour that can rot. The System program
#: creates, assigns and funds accounts and cannot invoke another program; ComputeBudget
#: only sets limits. Anything else — including the Memo program, which is very probably
#: inert too — is treated as token-capable and refuses without a measured token leg.
_PROGRAMS_THAT_CANNOT_MOVE_TOKENS: frozenset[str] = frozenset(
    {
        "11111111111111111111111111111111",
        "ComputeBudget111111111111111111111111111111",
    }
)


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
class TokenCap:
    """One mint's bounds, written in that mint's RAW base units, at a stated scale.

    ``decimals`` is not decoration. A cap of ``1_000_000`` is one USDC at 6 decimals and
    ten thousand units of a 2-decimal mint; the number alone says nothing. So the human
    writes the scale they meant, and a movement observed at a different one REFUSES
    (:data:`token-decimals-mismatch`). We do not rescale: rescaling would silently
    re-denominate the human's cap using a number a node reported.

    All three bounds are required. There is no window that is unlimited because the
    author only thought about one of them.
    """

    mint: str
    decimals: int
    per_transaction_raw: int
    hourly_raw: int
    daily_raw: int

    def __post_init__(self) -> None:
        if not self.mint or not isinstance(self.mint, str):
            raise ValueError("mint must be a non-empty string")
        if isinstance(self.decimals, bool) or not isinstance(self.decimals, int):
            raise ValueError("decimals must be an integer")
        if not 0 <= self.decimals <= 18:
            raise ValueError("decimals outside the range this module will render")
        for name in ("per_transaction_raw", "hourly_raw", "daily_raw"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class TokenCaps:
    """The per-mint map, and the one way to say "this agent moves no tokens".

    ``TokenCaps.none()`` is that sentence — an EXPLICIT empty map, under which any token
    outflow refuses with ``mint-not-allowlisted``. It exists so that "no token caps" can
    be authored deliberately instead of arriving as an unset field, which is the same
    reasoning ``simulate``'s ``network`` argument settled: a default lets a run that
    stated nothing look exactly like a run somebody thought about.
    """

    caps: Mapping[str, TokenCap]

    def __post_init__(self) -> None:
        for mint, cap in self.caps.items():
            if not isinstance(cap, TokenCap) or cap.mint != mint:
                raise ValueError(f"token cap for {mint} is keyed by a different mint")
        object.__setattr__(self, "caps", MappingProxyType(dict(self.caps)))

    @classmethod
    def of(cls, caps: Iterable[TokenCap]) -> TokenCaps:
        return cls(caps={cap.mint: cap for cap in caps})

    @classmethod
    def none(cls) -> TokenCaps:
        """An authored "no mint is allowed to move". Not the same as never authoring one."""
        return cls(caps={})

    def cap_for(self, mint: str) -> TokenCap | None:
        return self.caps.get(mint)


@dataclass(frozen=True)
class TokenSpend:
    """One mint's outflow being reserved, in that mint's raw base units. Never lamports."""

    mint: str
    raw: int


@dataclass(frozen=True)
class TokenVelocity:
    """One mint's rolling bounds, in its raw base units. Never summed with lamports."""

    mint: str
    hourly_raw: int
    daily_raw: int


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
    #: Cap 5. ``None`` means NEVER AUTHORED, which :meth:`missing_fields` reports and the
    #: gate refuses. An agent that genuinely moves no tokens authors
    #: :meth:`TokenCaps.none` — a sentence, not a silence.
    token_caps: TokenCaps | None = None

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
        # An EMPTY TokenCaps is authored ("this agent moves no tokens"); an ABSENT one was
        # never thought about. The two must not collapse, which is the whole reason the
        # field is a distinct type rather than a bare mapping that defaults to empty.
        if self.token_caps is None:
            missing.append("token_caps")
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
    #: What this transaction moves OUT, in lamports, once it could be resolved. LAMPORTS
    #: ONLY — a raw token amount is never folded in here, which is exactly the assignment
    #: that made a 25-USDC drain read as a 5,000-lamport fee.
    outflow_lamports: int | None = None
    #: What it moves out per MINT, each in that mint's own raw base units. A separate
    #: field rather than a converted total, because there is no honest conversion.
    outflow_tokens: tuple[TokenSpend, ...] = ()


@dataclass(frozen=True)
class VelocityLimits:
    """The rolling bounds, passed to the ADVISORY ledger as one value.

    The lamport bounds and the per-mint bounds travel together and are never added: they
    are different units, and ``token_limits`` is keyed by mint precisely so that no code
    path has a place to put a raw amount into a lamport window.
    """

    hourly_lamports: int
    daily_lamports: int
    max_transactions_per_day: int
    token_limits: tuple[TokenVelocity, ...] = ()


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
        self,
        *,
        at: float,
        lamports: int,
        limits: VelocityLimits,
        tokens: tuple[TokenSpend, ...] = (),
        digest: str | None = None,
    ) -> VelocityDecision:
        """Check every rolling window and, if within all of them, record the spend.

        ``tokens`` is per-mint and denominated in each mint's raw base units. It is a
        SEPARATE argument from ``lamports`` and stays separate all the way down: an
        implementation that adds the two has silently re-denominated the caps.

        ``digest`` makes the reservation IDEMPOTENT: if a live row within
        ``DEDUPE_SECONDS`` already carries it, this call records nothing and reports
        ``within`` true, because the budget for these exact bytes was already taken.

        It must be DERIVED from the transaction, never chosen by the caller. A
        caller-chosen key is a request to spend for free — reuse one value across
        different transactions and every one after the first reads as a replay. The one
        caller in this package passes an ``exact`` message binding for that reason.
        ``None`` disables deduplication and reserves unconditionally, which is the
        over-counting behaviour this argument exists to fix and remains the safe default.

        The commit is all-or-nothing. A lamport window that passes while a mint window
        refuses must record NOTHING, or a refused transaction has spent budget.

        Raises :class:`LedgerError` if the record cannot be read. It must never treat an
        unreadable ledger as an empty one — that turns a corrupt file into an unlimited
        budget.
        """
        ...


@dataclass(frozen=True)
class _LedgerEntry:
    """One reserved spend. ``mint`` is ``None`` for the lamport row.

    A public mint address is a program-surface identifier and may be recorded (it is what
    makes a per-mint window possible at all). An owner, a token-account address or a
    payload may NOT — invariant #1.

    ``digest`` IS a message binding, and it is here by an explicit founder ruling (D-B)
    that reverses what this docstring used to say. The objection was never that a one-way
    hash leaks a payload — it cannot — but that a file of bindings is a log of what was
    signed, correlatable by anyone who also holds the transaction. That objection is real
    and is answered by BOUNDING it rather than by denying it: the digest is erased from
    the row after ``DEDUPE_SECONDS`` while the amount and timestamp live for the full day.

    It is carried on the LAMPORT row only. There is exactly one of those per transaction,
    which makes it the natural identity anchor; repeating it on each mint row would be the
    same fact stored N times, free to disagree with itself.
    """

    at: float
    amount: int
    mint: str | None
    digest: str | None = None


def _window_decision(
    entries: list[_LedgerEntry],
    at: float,
    lamports: int,
    limits: VelocityLimits,
    tokens: tuple[TokenSpend, ...] = (),
) -> VelocityDecision:
    """The cumulative check, lifted from ``wallet_sim.py:87-90`` and given windows.

    ``_spent + amount > cap`` was already the right shape with no keys and no chain. What
    it lacked was a clock: a single running total never forgets and never refreshes, so it
    is a lifetime budget rather than a rate. Summing a rolling slice of timestamped entries
    is the same inequality applied to a window.

    Applied ONCE PER DENOMINATION. The lamport rows and each mint's rows are summed
    separately and compared against their own bounds; nothing crosses. The transaction
    COUNT is taken over the lamport rows only, because exactly one is written per
    authorized transaction — counting token rows too would charge a two-mint swap twice
    against a bound that is about frequency, not amount.
    """
    lamport_entries = [entry for entry in entries if entry.mint is None]
    hourly = sum(
        entry.amount for entry in lamport_entries if entry.at > at - HOUR_SECONDS
    )
    daily = sum(
        entry.amount for entry in lamport_entries if entry.at > at - DAY_SECONDS
    )
    daily_count = sum(1 for entry in lamport_entries if entry.at > at - DAY_SECONDS)

    token_decision = _token_window_decision(entries, at, limits, tokens)
    if token_decision is not None:
        return token_decision

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
    return VelocityDecision(within=True, reason="within every rolling bound")


def _token_window_decision(
    entries: list[_LedgerEntry],
    at: float,
    limits: VelocityLimits,
    tokens: tuple[TokenSpend, ...],
) -> VelocityDecision | None:
    """The same inequality, per mint, in that mint's raw base units. Never lamports.

    A spend on a mint with no bound in ``limits`` refuses here rather than passing: the
    caller resolves bounds from the authored caps, so a mint arriving without one means
    the two disagreed, and "we could not find the bound" is not "there is none".
    """
    bounds = {bound.mint: bound for bound in limits.token_limits}
    for spend in tokens:
        bound = bounds.get(spend.mint)
        if bound is None:
            return VelocityDecision(
                within=False,
                reason=(
                    f"no rolling bound is authored for mint {spend.mint}; an unbounded "
                    f"mint is refused, never counted as zero"
                ),
                code="mint-not-allowlisted",
            )
        rows = [entry for entry in entries if entry.mint == spend.mint]
        hourly = sum(entry.amount for entry in rows if entry.at > at - HOUR_SECONDS)
        daily = sum(entry.amount for entry in rows if entry.at > at - DAY_SECONDS)
        if hourly + spend.raw > bound.hourly_raw:
            return VelocityDecision(
                within=False,
                reason=(
                    f"{hourly + spend.raw} raw units of {spend.mint} in the last hour "
                    f"would pass the {bound.hourly_raw} hourly bound for that mint"
                ),
                code="over-hourly-token-cap",
            )
        if daily + spend.raw > bound.daily_raw:
            return VelocityDecision(
                within=False,
                reason=(
                    f"{daily + spend.raw} raw units of {spend.mint} in the last day "
                    f"would pass the {bound.daily_raw} daily bound for that mint"
                ),
                code="over-daily-token-cap",
            )
    return None


@dataclass
class InMemorySpendLedger:
    """An ADVISORY ledger for one process. For tests and for a single short-lived run.

    Explicitly NOT the cross-process form: a second process starts at zero. It exists so
    the policy is falsifiable offline without touching a filesystem, and naming it
    ``InMemory`` is the whole warning.
    """

    _entries: list[_LedgerEntry] = field(default_factory=list, init=False)

    def reserve(
        self,
        *,
        at: float,
        lamports: int,
        limits: VelocityLimits,
        tokens: tuple[TokenSpend, ...] = (),
        digest: str | None = None,
    ) -> VelocityDecision:
        self._entries = [
            entry for entry in self._entries if entry.at > at - DAY_SECONDS
        ]
        if _is_replay(self._entries, at, digest):
            return _REPLAY
        self._entries = _expire_digests(self._entries, at)
        decision = _window_decision(self._entries, at, lamports, limits, tokens)
        if decision.within:
            self._entries.extend(_rows_for(at, lamports, tokens, digest))
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

    A row is a timestamp and an amount, plus — on a token row — the public MINT the
    amount is denominated in. No owner, no token-account address, no binding, no payload:
    a budget needs the sum, not the history.
    """

    path: str

    def reserve(
        self,
        *,
        at: float,
        lamports: int,
        limits: VelocityLimits,
        tokens: tuple[TokenSpend, ...] = (),
        digest: str | None = None,
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
                kept = [entry for entry in entries if entry.at > at - DAY_SECONDS]
                # Read the digest BEFORE expiring any, and under the same lock as the
                # append. A replay check that is not inside the lock is the double-spend
                # it was added to prevent, just with more steps.
                replay = _is_replay(kept, at, digest)
                rows = _expire_digests(kept, at)
                if replay:
                    decision = _REPLAY
                else:
                    decision = _window_decision(rows, at, lamports, limits, tokens)
                    if decision.within:
                        # All-or-nothing: the lamport row and every mint row are appended
                        # together, under the one lock, or none of them is.
                        rows = rows + _rows_for(at, lamports, tokens, digest)
                if rows != entries:
                    handle.seek(0)
                    handle.truncate()
                    for entry in rows:
                        handle.write(json.dumps(_row_json(entry)) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                return decision
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _rows_for(
    at: float,
    lamports: int,
    tokens: tuple[TokenSpend, ...],
    digest: str | None = None,
) -> list[_LedgerEntry]:
    """The rows one authorized transaction writes: one lamport row, one row per mint.

    Exactly one lamport row per transaction, always — it is what the daily COUNT bound is
    taken over, so a transaction that moved zero lamports still writes it. That uniqueness
    is also why ``digest`` rides on it and on nothing else.
    """
    rows = [_LedgerEntry(at=at, amount=lamports, mint=None, digest=digest)]
    rows.extend(
        _LedgerEntry(at=at, amount=spend.raw, mint=spend.mint) for spend in tokens
    )
    return rows


def _expire_digests(entries: list[_LedgerEntry], at: float) -> list[_LedgerEntry]:
    """Drop the digest from rows older than ``DEDUPE_SECONDS``, keeping the amounts.

    The row still counts against the day; it simply stops saying WHICH transaction it was.
    Runs on every reserve, so expiry is driven by use rather than by a background job that
    may never run — a ledger nobody touches keeps its digests, and a ledger nobody touches
    is also one nobody is spending from.
    """
    cutoff = at - DEDUPE_SECONDS
    return [
        replace(entry, digest=None)
        if entry.digest is not None and entry.at <= cutoff
        else entry
        for entry in entries
    ]


def _is_replay(entries: list[_LedgerEntry], at: float, digest: str | None) -> bool:
    """Has this exact transaction already reserved budget inside the dedupe window?

    ``None`` is never a replay. Two rows that have both had their digests expired must not
    match each other, which is what comparing ``None`` to ``None`` would do — turning
    every old row into a free pass for every future one.
    """
    if digest is None:
        return False
    cutoff = at - DEDUPE_SECONDS
    return any(
        entry.digest == digest and entry.at > cutoff
        for entry in entries
        if entry.digest is not None
    )


#: The answer a replay gets. ``within`` is true because the budget was ALREADY taken for
#: these bytes; the windows are deliberately not re-evaluated, since re-checking would add
#: the same amount on top of its own reservation and could refuse a legitimate retry.
_REPLAY = VelocityDecision(
    within=True,
    reason="these exact transaction bytes already reserved velocity budget within the "
    "dedupe window; the reservation is idempotent and was not taken a second time",
)


def _row_json(entry: _LedgerEntry) -> dict[str, Any]:
    if entry.mint is None:
        row: dict[str, Any] = {"at": entry.at, "lamports": entry.amount}
        if entry.digest is not None:
            row["d"] = entry.digest
        return row
    return {"at": entry.at, "mint": entry.mint, "raw": entry.amount}


def _read_entries(text: str, where: str) -> list[_LedgerEntry]:
    """Parse the ledger, refusing anything it cannot fully account for.

    A malformed line is a :class:`LedgerError`, never a skipped line. Skipping would make a
    corrupted or truncated ledger read as a smaller total, which is the fail-open shaped
    exactly like a fresh start.

    Three row shapes and no others, matched on the EXACT key set: ``{at, lamports}``,
    ``{at, lamports, d}`` and ``{at, mint, raw}``. A row carrying an extra key is refused
    rather than read past — an unrecognised field is either a schema this code does not
    understand or something that should never have been written here (an owner, an
    address, a payload), and both are reasons to stop rather than to sum what is left.

    ``d`` is OPTIONAL on the lamport row rather than required, in both directions. A
    ledger written before deduplication existed stays readable, so upgrading does not
    read as a corrupt file and refuse every transaction; and a row whose digest has
    expired is the same shape as one that never had it.
    """
    entries: list[_LedgerEntry] = []
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
        keys = set(record)
        when = record.get("at")
        if isinstance(when, bool) or not isinstance(when, (int, float)):
            raise LedgerError(f"spend ledger line {number} has no usable timestamp")
        digest: str | None = None
        if keys in ({"at", "lamports"}, {"at", "lamports", "d"}):
            mint: str | None = None
            amount = record.get("lamports")
            if "d" in keys:
                stored = record.get("d")
                if not isinstance(stored, str) or not stored:
                    raise LedgerError(
                        f"spend ledger line {number} has an unusable dedupe digest; a "
                        f"row that half-identifies a transaction is refused, not read "
                        f"as an unidentified one"
                    )
                digest = stored
        elif keys == {"at", "mint", "raw"}:
            mint = record.get("mint")
            amount = record.get("raw")
            if not isinstance(mint, str) or not mint:
                raise LedgerError(f"spend ledger line {number} has no usable mint")
        else:
            raise LedgerError(
                f"spend ledger line {number} has fields this ledger does not account "
                f"for ({sorted(keys)}); an unaccountable row is refused, not skipped"
            )
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise LedgerError(f"spend ledger line {number} has no usable amount")
        entries.append(
            _LedgerEntry(at=float(when), amount=amount, mint=mint, digest=digest)
        )
    del where
    return entries


@dataclass(frozen=True)
class _Subject:
    """The decoded facts one authorization decision is taken over. No intent, by design."""

    policy: SpendPolicy
    decoded: DecodedMessage
    outflow_lamports: int
    #: Per-mint outflows attributable to the FEE PAYER, in each mint's raw base units.
    #: Never lamports, never summed with ``outflow_lamports``.
    token_outflows: tuple[TokenOutflow, ...] = ()


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


def _check_token_caps(subject: _Subject) -> SpendVerdict | None:
    """Cap 5 — every token outflow, against its own mint's cap, at its own scale.

    Three distinct refusals, and none of them is silence: a mint nobody authored, a scale
    that disagrees with the authored one, and an amount past the bound.
    """
    caps = subject.policy.token_caps
    if caps is None:  # pragma: no cover - completeness is checked before the predicates
        return _refuse("policy-incomplete", "token_caps is unset")
    for outflow in subject.token_outflows:
        cap = caps.cap_for(outflow.mint)
        if cap is None:
            return _refuse(
                "mint-not-allowlisted",
                f"this transaction moves {outflow.raw} raw units of mint "
                f"{outflow.mint} out, and the policy authorises no cap for that mint; an "
                f"unauthored mint is refused, never passed unmeasured",
            )
        if cap.decimals != outflow.decimals:
            return _refuse(
                "token-decimals-mismatch",
                f"the cap for {outflow.mint} was authored at {cap.decimals} decimals and "
                f"this movement was reported at {outflow.decimals}; a raw cap at the "
                f"wrong scale is off by a factor of ten per decimal, so this refuses "
                f"rather than rescaling the human's number with a node's",
            )
        if outflow.raw > cap.per_transaction_raw:
            return _refuse(
                "over-per-transaction-token-cap",
                f"this transaction moves {outflow.raw} raw units of {outflow.mint} out, "
                f"past the {cap.per_transaction_raw} per-transaction bound for that mint "
                f"(both at {cap.decimals} decimals)",
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

        ``receipt`` supplies ``sol_delta`` and ``token_delta`` — the two AMOUNT fields —
        and nothing else. Its binding, strength, status and network belong to the OTHER
        predicate; reading them here would let verification stand in for authorization,
        which is the substitution this whole module exists to prevent.

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

        resolved = _resolve_token_outflows(receipt, decoded)
        if isinstance(resolved, SpendVerdict):
            return resolved

        subject = _Subject(
            policy=policy,
            decoded=decoded,
            outflow_lamports=outflow,
            token_outflows=resolved,
        )
        for predicate in (
            _check_instructions,
            _check_destinations,
            _check_per_transaction_cap,
            _check_token_caps,
        ):
            verdict = _run_predicate(predicate, subject)
            if verdict is not None:
                return verdict

        # The cumulative cap runs LAST and is the only predicate with a side effect. A
        # transaction refused by any earlier check must not spend velocity budget: a
        # counter that ticks on refusals is a denial-of-service an attacker drives.
        return _reserve(
            ledger, policy, outflow, resolved, moment, _dedupe_key(transaction_base64)
        )


def _resolve_token_outflows(
    receipt: Receipt, decoded: DecodedMessage
) -> tuple[TokenOutflow, ...] | SpendVerdict:
    """What left the FEE PAYER, per mint — or the refusal that says why we cannot know.

    The three states of ``Receipt.token_delta`` are kept apart here, because collapsing
    any two of them is the bug this cap exists for:

    * ``None`` — NOT TRACKED. Refused as ``token-leg-not-measured``, unless every program
      the message invokes provably cannot move a token, in which case there is no token
      leg and the absence is a fact.
    * ``unmeasurable`` — reading an amount off it RAISES, and that raise is mapped onto
      ``amount-unresolvable``: the same answer an unreadable ``sol_delta`` earns, because
      it is the same sentence.
    * ``measured`` — the amounts are read, and an empty set is an OBSERVED zero.

    Only the fee payer's own outflows are charged; see the module docstring's residual on
    delegated authority for what that does not cover.
    """
    report = receipt.token_delta
    if report is None:
        token_capable = [
            one.program_id
            for one in decoded.instructions
            if one.program_id not in _PROGRAMS_THAT_CANNOT_MOVE_TOKENS
        ]
        if not token_capable:
            return ()
        return _refuse(
            "token-leg-not-measured",
            f"the simulation carried no token balances, and this message invokes "
            f"{', '.join(sorted(set(token_capable)))}, which can move tokens; an "
            f"unmeasured token leg is refused, never read as zero",
        )
    try:
        outflows = report.outflows()
    except TokenDeltaUnmeasurable as exc:
        return _refuse(
            "amount-unresolvable",
            f"the token leg of this simulation could not be measured ({exc}); there is "
            f"no amount to compare against a cap, and zero is not the answer",
        )
    payer = decoded.fee_payer
    return tuple(outflow for outflow in outflows if outflow.owner == payer)


def _dedupe_key(transaction_base64: str | bytes) -> str | None:
    """The idempotency key for these bytes: an ``exact`` message binding, or ``None``.

    DERIVED FROM THE SUBJECT, NEVER SUPPLIED. There is no parameter on
    :meth:`SpendPolicyGate.authorize` through which a key could arrive, and that is the
    whole safety property: a caller-chosen key reused across different transactions would
    make every one after the first read as an already-paid replay. Here "same key" means
    "same bytes" by construction, so the class of forgery does not exist.

    ``exact`` RATHER THAN ``structural``, and the difference is the entire design.
    ``structural`` normalises the blockhash to zero, so it is stable across re-quoting the
    same plan — which sounds like what deduplication wants and is in fact a hole. Two
    transfers a human deliberately made twice are structurally identical, so they would
    collapse into ONE reservation and the second would spend no budget. That is a cap
    bypass reachable by repeating a transfer.

    ``exact`` cannot do that, and the reason is not caution but arithmetic: byte-identical
    messages carry the same blockhash and produce the same signature, and a cluster
    rejects a duplicate signature. Two calls that match here could never both have
    settled, so counting them once is not a concession — it is correct.

    The cost is a NAMED RESIDUAL, not a silent one: a retry that refreshes its blockhash
    has different bytes, so it is not recognised and reserves again. The counter still
    over-counts there. That is the safe direction and it is stated in the module docstring.

    Returns ``None`` rather than raising. A key that cannot be computed means the
    reservation is simply not deduplicated, which is exactly the behaviour that shipped
    before this existed; refusing a legitimate transaction because an anti-over-counting
    measure was unavailable would trade a small accounting error for a denial of service.
    """
    try:
        return message_binding(transaction_base64, strength="exact")
    except Exception:  # noqa: BLE001 - see the docstring: no key is a valid answer here
        return None


def _reserve(
    ledger: AdvisorySpendLedger,
    policy: SpendPolicy,
    outflow: int,
    token_outflows: tuple[TokenOutflow, ...],
    moment: float,
    digest: str | None = None,
) -> SpendVerdict:
    """Cap 2 — the ADVISORY rolling windows, checked and committed under one lock.

    The per-mint bounds travel beside the lamport ones and are never added to them. The
    bounds are taken from the authored caps, so a mint that reached here without one is a
    disagreement between two code paths and the ledger refuses it rather than counting it.

    ``digest`` deduplicates a retry of the identical transaction; see
    :meth:`AdvisorySpendLedger.reserve`. It is passed through untouched — deriving it is
    the caller's job and validating it is nobody's, because it is not a claim.
    """
    caps = policy.token_caps
    token_limits = tuple(
        TokenVelocity(mint=cap.mint, hourly_raw=cap.hourly_raw, daily_raw=cap.daily_raw)
        for cap in (caps.caps.values() if caps is not None else ())
    )
    limits = VelocityLimits(
        hourly_lamports=policy.hourly_cap_lamports or 0,
        daily_lamports=policy.daily_cap_lamports or 0,
        max_transactions_per_day=policy.max_transactions_per_day or 0,
        token_limits=token_limits,
    )
    tokens = tuple(
        TokenSpend(mint=one.mint, raw=one.raw) for one in token_outflows if one.raw > 0
    )
    try:
        decision = ledger.reserve(
            at=moment, lamports=outflow, limits=limits, tokens=tokens, digest=digest
        )
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
            outflow_tokens=tokens,
        )
    reason = (
        "within the per-transaction cap, the allowlisted (program, instruction) "
        "pairs, the allowlisted destinations, the per-mint token caps, and the "
        "ADVISORY rolling bounds"
    )
    if decision is _REPLAY:
        # "Allowed, and charged" and "allowed, because already charged" are different
        # facts, and the second is the one somebody reconciling a budget needs to see.
        # Reporting the generic sentence would make an idempotent pass indistinguishable
        # from a fresh reservation in exactly the log they would be reading.
        reason = f"{reason} — {decision.reason}"
    return SpendVerdict(
        authorized=True,
        reason=reason,
        outflow_lamports=outflow,
        outflow_tokens=tokens,
    )
