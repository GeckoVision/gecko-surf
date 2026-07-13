"""Probe sandbox — offline validation that answers with the API's OWN error shape.

The self-healing loop's engine-safe core: ``evaluate`` runs an agent's call
through validation gates derived entirely from the comprehended spec and returns
a *synthetic result* either way — a malformed call yields the API's own declared
error body (422) plus machine-readable signals + remediation, a well-formed call
yields a schema-synthesized success. Nothing here ever reaches the wire, injects
auth, or persists anything: this module sits on the no-wire side of the transport
edge (invariant #3) and, by structural gate (see ``test_sandbox_evaluate``), has
no outcome-record call site at all — capture stays in the client, where probe
outcomes route ``source="synthetic"`` and never touch a published metric.

Gates (in order):
  (a) structural — declared-required presence (``caller._missing_required`` as a
      RESULT, not an exception);
  (b) schema — the comprehension-native conformance check (``risk._schema_conformance``:
      type/enum/unknown-field against the API's own schema);
  (c) state — the per-session ``SimWorld`` (deposit→withdraw correlations); rules
      auto-derived from ``risk`` (verb + amount shape), balances are fabricated
      ``Decimal``s under opaque keys, never persisted (invariant #1).
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from .caller import _missing_required
from .catalog import _token_list  # the shared identifier tokenizer (do not re-invent)
from .client import _error_schema, _success_schema
from .enforce import REMEDIATION
from .ingest import Operation
from .risk import (
    _extract_amount,
    _extract_recipients,
    _schema_conformance,
    classify_operation,
)
from .sample import example_from_schema
from .sanitize import sanitize_schema
from .tools import to_tool

#: Marks every probe result unmistakably synthetic to the agent (the recorded-mode
#: lesson: an agent cannot tell a zeroed placeholder from real data unless told).
PROBE_MODE_NOTE = (
    "Synthetic probe result — validated offline against the API's own schema; "
    "no live call was made and no real data is returned. Fix the reported "
    "signals and retry, or switch to live mode for real responses."
)

#: Fallback error body when the spec declares no 4xx/default error schema. A CODE
#: CONSTANT — it never interpolates an arg value, so it is control-plane safe.
_GENERIC_ERROR_BODY: dict[str, str] = {"error": "invalid request (synthetic probe)"}

#: The synthetic status for a call that fails validation. Fixed at 422 (the
#: canonical "well-formed but semantically invalid" code); ``_error_schema`` scans
#: 422 first so the body shape aligns whenever the API declares one.
_VALIDATION_STATUS = 422

#: The state-gate signal for a debit against an insufficient simulated balance. A CODE
#: CONSTANT (its remediation lives in ``enforce.REMEDIATION``); it names a synthetic
#: condition, never an arg value, so it is control-plane safe.
_INSUFFICIENT_SIGNAL = "state.insufficient"

#: Comprehension-derived sim-rules (invariant #2: auto-derived from the op's shape, never
#: hand-written per API). A debit verb + an amount pre-checks and decrements the balance; a
#: credit verb + an amount increments it; anything else has no state effect. Matched as WHOLE
#: tokens on the op's operationId/path/summary (same discipline as ``risk.MONEY_VERBS``).
_DEBIT_VERBS: frozenset[str] = frozenset(
    {"withdraw", "withdrawal", "send", "swap", "debit"}
)
_CREDIT_VERBS: frozenset[str] = frozenset({"deposit", "mint", "fund", "credit"})

#: Default TTL / session cap for the in-memory store. The store is a control-plane cache of
#: FABRICATED balances only; these bounds keep it from growing unbounded, they are not a
#: durability guarantee — the store is process-local and ephemeral by design.
_DEFAULT_TTL_SECONDS = 3600.0
_DEFAULT_MAX_SESSIONS = 1024


@dataclass
class SimWorld:
    """One session's ephemeral simulated state — process-local, NEVER written to disk.

    ``balances`` maps an OPAQUE key (a hash of the recipient/account arg, or the ``"self"``
    bucket) to a FABRICATED ``Decimal`` — never a real payload, arg value, or secret
    (invariant #1). ``last_touched`` is the injected wall-clock of the last access, used only
    for TTL eviction."""

    balances: dict[str, Decimal] = field(default_factory=dict)
    last_touched: float = 0.0


class SimStore:
    """Per-session ``SimWorld`` cache with TTL eviction + an LRU session cap.

    In-memory and process-local: it holds only fabricated balances under opaque keys and is
    never persisted. The clock is INJECTED on every access (``now: float``) — the store never
    calls an argless clock, so eviction is deterministic and unit-testable (repo rule)."""

    def __init__(
        self,
        ttl: float = _DEFAULT_TTL_SECONDS,
        max_sessions: int = _DEFAULT_MAX_SESSIONS,
    ) -> None:
        self._ttl = ttl
        self._max_sessions = max_sessions
        # OrderedDict as the LRU: least-recently-used at the front, most-recent at the back.
        self._worlds: OrderedDict[str, SimWorld] = OrderedDict()

    def gc(self, now: float) -> None:
        """Evict every world older than the TTL. Injected clock; safe to call anytime."""
        stale = [
            sid
            for sid, world in self._worlds.items()
            if now - world.last_touched > self._ttl
        ]
        for sid in stale:
            del self._worlds[sid]

    def get_or_create(self, session_id: str, now: float) -> SimWorld:
        """Return this session's world (creating a fresh one if absent or evicted).

        TTL eviction runs first (a stale world is dropped and recreated empty); the accessed
        world moves to the LRU tail; then the oldest sessions are trimmed to the cap."""
        self.gc(now)
        world = self._worlds.get(session_id)
        if world is None:
            world = SimWorld()
            self._worlds[session_id] = world
        else:
            self._worlds.move_to_end(session_id)
        world.last_touched = now
        while len(self._worlds) > self._max_sessions:
            self._worlds.popitem(last=False)  # drop the least-recently-used session
        return world


@dataclass(frozen=True)
class SimResult:
    """One synthetic probe outcome — always a result, never an exception.

    ``data`` is synthesized from the spec's own response schemas (sanitized), so it
    can never carry a real payload; ``signals``/``remediation`` are code-constant
    names and generic fix strings (no arg values) — the agent's self-heal input."""

    status: int
    data: Any
    signals: list[str] = field(default_factory=list)
    remediation: dict[str, str] = field(default_factory=dict)
    mode: Literal["probe"] = "probe"
    mode_note: str = PROBE_MODE_NOTE


def _synthesize(schema: dict[str, Any]) -> Any:
    """Schema -> sanitized example. Response-side scrub (``route_to_arg=False``),
    the same defense recorded mode applies: a poisoned response schema must not
    surface an injected instruction or secret through the synthetic body."""
    clean, _ = sanitize_schema(schema, route_to_arg=False)
    return example_from_schema(clean)


def _balance_key(args: dict[str, Any]) -> str:
    """The opaque balance bucket for a call. A recipient/account arg is HASHED (the store
    never holds the raw account string, only a fabricated Decimal under an opaque key); with
    no recipient arg the call buckets under ``"self"``."""
    recipients = _extract_recipients(args)
    if recipients:
        digest = hashlib.sha256(recipients[0].encode("utf-8")).hexdigest()[:16]
        return f"acct:{digest}"
    return "self"


def _op_verb_tokens(op: Operation) -> set[str]:
    """Whole-token set from the op's operationId/path/summary — the sim-rule verb probe."""
    return set(_token_list(f"{op.operation_id} {op.path} {op.summary}"))


def _insufficient(op: Operation) -> SimResult:
    """The synthetic 422 for a debit that exceeds the simulated balance — the API's OWN
    declared error shape (or the generic constant body) + the remediation line."""
    data = _synthesize(_error_schema(op))
    if data is None:
        data = dict(_GENERIC_ERROR_BODY)
    return SimResult(
        status=_VALIDATION_STATUS,
        data=data,
        signals=[_INSUFFICIENT_SIGNAL],
        remediation={_INSUFFICIENT_SIGNAL: REMEDIATION[_INSUFFICIENT_SIGNAL]},
    )


def _apply_state(
    op: Operation, args: dict[str, Any], world: SimWorld
) -> SimResult | None:
    """Gate (c): mutate the session balance per the comprehension-derived sim-rule.

    Returns a 422 ``SimResult`` to short-circuit an insufficient debit; otherwise mutates the
    balance in place and returns ``None`` so ``evaluate`` proceeds to the synthetic success.
    A read tier or an amount-less call has no state effect (returns ``None`` unchanged)."""
    if classify_operation(op).tier == "read":
        return None  # a read moves no value
    amount = _extract_amount(args)
    if amount is None:
        return None  # no amount shape -> nothing to move
    verbs = _op_verb_tokens(op)
    key = _balance_key(args)
    balance = world.balances.get(key, Decimal("0"))
    if verbs & _DEBIT_VERBS:
        if balance < amount:
            return _insufficient(op)
        world.balances[key] = balance - amount
        return None
    if verbs & _CREDIT_VERBS:
        world.balances[key] = balance + amount
        return None
    return None  # a value-moving write with no debit/credit verb: no simulated effect


def evaluate(
    op: Operation, args: dict[str, Any], world: SimWorld | None = None
) -> SimResult:
    """Run one probe call through the gates and synthesize the outcome.

    ``world`` is the per-session ``SimWorld`` seam (the state gate). When supplied, a
    well-formed debit/credit call correlates against the session balance (deposit -> withdraw)
    and an over-balance debit returns the synthetic 422; when omitted, ``evaluate`` is
    stateless (byte-identical to the pre-state behavior).
    """
    tool = to_tool(op)
    schema = tool.get("inputSchema") or {}

    signals: list[str] = []
    # gate (a): declared-required presence — the same check the caller enforces
    # pre-flight, surfaced here as a result instead of a raised CallError.
    if _missing_required(tool, args):
        signals.append("schema.required")
    # gate (b): conformance against the API's own schema (type / enum / unknown).
    for reason in _schema_conformance(schema, args):
        if reason.signal not in signals:
            signals.append(reason.signal)

    if signals:
        data = _synthesize(_error_schema(op))
        if data is None:
            data = dict(_GENERIC_ERROR_BODY)
        return SimResult(
            status=_VALIDATION_STATUS,
            data=data,
            signals=signals,
            remediation={s: REMEDIATION[s] for s in signals if s in REMEDIATION},
        )

    # gate (c): state (SimWorld) — deposit/withdraw correlations across a session. Only a
    # supplied world engages the gate; an insufficient debit short-circuits with a 422.
    if world is not None:
        state_result = _apply_state(op, args, world)
        if state_result is not None:
            return state_result

    return SimResult(status=200, data=_synthesize(_success_schema(op)))
