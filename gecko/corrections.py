"""Args-side of the correctness corpus — captured corrections that raise first-call-correct
beyond comprehension alone (the flywheel's turning point).

Where ``corpus.py`` records the *outcome* of a call (status/error-class/shape), this module
records the *fix*: for a call that picked the right tool but under-supplied a non-obvious
required param, it captures a terse, control-plane-safe correctness note ("Also required:
``interval`` — omitting it returns a 400") that, re-injected into the tool's description,
teaches the next agent to get it right the first time. Capture is derived PURELY from FCC
failure telemetry (``RunRecord`` shapes + booleans), so it is metadata → metadata.

Control-plane discipline (mirrors ``corpus.py``):
  * A ``Correction`` carries NO arg value — only the param NAME, the failure KIND, a hint
    derived from the schema-shaped KIND (never an observed value), and an observation count.
  * The allowlist is the dataclass field set; ``assert_allowlisted`` fails closed on any
    stray key.
  * Every injected hint passes ``enrich.safe_blurb`` at the enrichment boundary, so a
    poisoned hint (injected instruction / secret) is dropped, never smuggled to the agent.

HONESTY (scope): this proves the flywheel **mechanism** — IF correctness corrections are
captured, they measurably raise FCC beyond comprehension alone. It does **NOT** prove
production auto-capture: the feedback path (the agent calls the API directly, so Gecko may
not see live outcomes) is still an unresolved design decision (invariant: never store
payloads). The corrections here are derived from our own FCC eval telemetry (metadata-only
``RunRecord``s), which is a legitimate capture source but a controlled one. A thin or zero
lift on a task is a real finding, not a bug.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from .enrich import safe_blurb
from .fcc_eval import RunRecord

# The closed set of correction kinds. ``value_semantics`` is reserved for a future capture
# path (a right-kind value that is nonetheless semantically wrong, e.g. an out-of-range enum);
# the two kinds derived from shape telemetry today are missing_required / wrong_kind.
CorrectionKind = Literal["missing_required", "wrong_kind", "value_semantics"]
CORRECTION_KINDS: frozenset[str] = frozenset(get_args(CorrectionKind))


class CorrectionError(Exception):
    """Raised when a Correction would violate the control-plane allowlist / closed set."""


@dataclass(frozen=True)
class Correction:
    """One captured correctness fix — control-plane clean (names + KIND + a derived hint,
    never an observed arg value). Frozen so it can't accrete fields at runtime; the field
    set IS the persisted schema (see ``ALLOWED_KEYS``)."""

    tool_name: str
    kind: str  # CorrectionKind
    param: str
    hint: (
        str  # a short imperative correctness note, derived from the schema-shaped KIND
    )
    n_observed: int


ALLOWED_KEYS = frozenset(Correction.__dataclass_fields__)


def assert_allowlisted(mapping: Mapping[str, Any]) -> None:
    """Reject (fail closed) any key not on the Correction allowlist."""
    extra = set(mapping) - ALLOWED_KEYS
    if extra:
        raise CorrectionError(
            f"non-allowlisted correction key(s) would be persisted: {sorted(extra)}"
        )


# --- CAPTURE: derive corrections from FCC failure telemetry (metadata -> metadata) ------


def _hint(kind: str, param: str, gold_kind: str, agent_kind: str | None) -> str:
    """A terse correctness note built from the param NAME + the schema-shaped KIND only.

    Never reads an observed value — ``gold_kind`` / ``agent_kind`` are ``value_kind``
    classifications (int/mint/symbol/…), i.e. the same control-plane projection the
    ``RunRecord`` already stores. That is what keeps capture metadata-only."""
    if kind == "missing_required":
        return f"Also required: `{param}` (expected {gold_kind}) — omitting it returns a 400."
    if kind == "wrong_kind":
        return (
            f"Parameter `{param}` expects a {gold_kind} value; "
            f"a {agent_kind} value is rejected."
        )
    return f"Parameter `{param}` needs a correct {gold_kind} value."


def _culprits(
    gold_shape: Mapping[str, str], agent_shape: Mapping[str, str]
) -> list[tuple[str, str, str, str | None]]:
    """Diff gold vs agent arg-SHAPES (name -> value-KIND) to find the mis-supplied param(s).

    A gold key absent from the agent's args -> ``missing_required``; present but a different
    KIND -> ``wrong_kind``. Returns ``(param, kind, gold_kind, agent_kind)`` tuples. Reads
    KINDs only — never a value."""
    out: list[tuple[str, str, str, str | None]] = []
    for name, gold_kind in gold_shape.items():
        if name not in agent_shape:
            out.append((name, "missing_required", gold_kind, None))
        elif agent_shape[name] != gold_kind:
            out.append((name, "wrong_kind", gold_kind, agent_shape[name]))
    return out


def corrections_from_records(records: list[RunRecord]) -> list[Correction]:
    """The CAPTURE step: turn GECKO-arm FCC failures into re-injectable corrections.

    Considers only records where the GECKO arm picked the RIGHT tool (``tool_correct``) but
    the args did not match (``args_match`` False) — i.e. comprehension surfaced the correct
    op, yet the agent under-/mis-supplied a param. Diffs ``gold_shape`` vs ``agent_shape`` to
    attribute the culprit, aggregates by ``(tool_name, param)`` with an observation count, and
    derives a schema-KIND hint. Buildable from ``RunRecord``s alone (no spec, no values)."""
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for r in records:
        if r.arm != "gecko" or not r.tool_correct or r.args_match:
            continue
        tool = r.picked
        if tool is None:
            continue
        for param, kind, gold_kind, agent_kind in _culprits(
            r.gold_shape, r.agent_shape
        ):
            key = (tool, param)
            if key not in agg:
                agg[key] = {
                    "kind": kind,
                    "gold_kind": gold_kind,
                    "agent_kind": agent_kind,
                    "n": 0,
                }
                order.append(key)
            agg[key]["n"] += 1
    corrections: list[Correction] = []
    for tool, param in order:
        info = agg[(tool, param)]
        corrections.append(
            Correction(
                tool_name=tool,
                kind=info["kind"],
                param=param,
                hint=_hint(info["kind"], param, info["gold_kind"], info["agent_kind"]),
                n_observed=info["n"],
            )
        )
    return corrections


# --- ENRICH: re-inject corrections into a tool def (the flywheel's output) ---------------


def enrich_with_corrections(
    tool_def: dict[str, Any], corrections: list[Correction]
) -> dict[str, Any]:
    """Return a COPY of ``tool_def`` with each matching correction's hint appended to the
    description as a ``Correctness note: …`` line, and (best-effort) stamped onto that param's
    schema ``description``.

    Every injected note passes ``enrich.safe_blurb`` (fail-closed sanitize): a poisoned hint
    is dropped entirely rather than smuggled into the agent's context. The input is never
    mutated — a deep copy is returned."""
    out: dict[str, Any] = copy.deepcopy(dict(tool_def))
    name = out.get("name")
    matching = [c for c in corrections if c.tool_name == name]
    if not matching:
        return out

    schema = out.get("inputSchema")
    props = schema.get("properties") if isinstance(schema, dict) else None

    notes: list[str] = []
    for c in matching:
        note = safe_blurb(f"Correctness note: {c.hint}")
        if not note:
            continue  # poisoned hint -> dropped (fail closed), never reaches the agent
        notes.append(note)
        # Optionally stamp the note onto the param's own schema description too.
        if isinstance(props, dict):
            pschema = props.get(c.param)
            if isinstance(pschema, dict):
                existing = str(pschema.get("description", "")).strip()
                pschema["description"] = (
                    f"{existing} {note}".strip() if existing else note
                )

    if notes:
        base = str(out.get("description", "")).strip()
        joined = "\n".join(notes)
        out["description"] = f"{base}\n{joined}" if base else joined

    return out
