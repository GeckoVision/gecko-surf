"""Control-plane-safe Preflight corpus — the moat, made load-bearing from day one.

Every Preflight run appends ONE metadata record: the failure CLASSES it found (closed-set
names), the ``surface_rev`` fingerprint, and per-severity counts — NEVER an arg value, a
payload, a spec string, or a secret. This is the compounding cross-API signal (the
[[moat-corpus-flywheel]]): a class first observed on API #1 is *read back* as a watched
class when Preflight checks API #2.

Two structural guarantees mirror ``corpus.py``:

1. **Closed class vocabulary.** ``KNOWN_CLASSES`` is the single source of truth for the
   failure classes v1 checks. A record whose classes fall outside it fails closed
   (``PreflightCorpusError``) — a stray free-text class breaks the build rather than
   smuggling a value out.
2. **Allowlist writer.** ``to_record`` rejects any key not on the frozen ``PreflightRun``
   schema; there is no field through which a payload/arg value could enter.

Append-only JSONL keeps it structurally safe (no UPDATE path that could accrete a
payload) and human-auditable (``grep`` the file; assert no value substrings). Opt-in:
nothing is written unless a caller passes a ``corpus_path`` (off by default, like
``corpus.py``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# --- the closed failure-class vocabulary (single source of truth) --------------------
# Every class Preflight can emit AND the seed of the cross-API known-classes registry.
# Append-only to the CLOSED set — a stray class is a build break, never free text. Grouped
# by check so the vocabulary reads as the product's contract.
COVERAGE_CLASSES = frozenset(
    {
        "coverage.auth_gated_no_cred",  # op needs auth the (public) session can't provide
        "coverage.malformed",  # op produced no usable tool (bad/empty operation)
        "coverage.quarantined",  # surface born quarantined (from-docs / poisoned)
        "coverage.low_usable",  # < half the surface is agent-usable (warning)
    }
)
HEALTH_CLASSES = frozenset(
    {
        "schema.invalid",  # tool inputSchema is not object+properties
        "schema.required_undeclared",  # a `required` entry has no matching property
        "param.untyped",  # a param has no JSON type (an agent can't fill it)
        "auth.misdeclared",  # a credential leaked into the agent-facing tool def (inv #4)
    }
)
SAFETY_CLASSES = frozenset(
    {
        "surface.poisoned",  # a secret-shaped value sits in a value channel
        "surface.injectable",  # instruction-injection text in a description/field
    }
)
DRIFT_CLASSES = frozenset(
    {
        "drift.op_removed",  # an operation the baseline had is gone
        "drift.field_renamed",  # a required field was renamed (old gone, new added)
        "drift.required_tightened",  # a field became required that wasn't before
        "drift.op_added",  # a new operation appeared (informational, non-breaking)
    }
)

KNOWN_CLASSES: frozenset[str] = (
    COVERAGE_CLASSES | HEALTH_CLASSES | SAFETY_CLASSES | DRIFT_CLASSES
)

# The classes that FAIL a build (blocking). Everything else is a warning. Drift-breaking =
# the three drift classes that break existing agent calls (op_removed / field_renamed /
# required_tightened); drift.op_added is non-breaking. Kept here beside the vocabulary so
# the verdict logic and the corpus share one source of truth.
BLOCKING_CLASSES: frozenset[str] = frozenset(
    {
        "auth.misdeclared",
        "surface.poisoned",
        "surface.injectable",
        "drift.op_removed",
        "drift.field_renamed",
        "drift.required_tightened",
    }
)


class PreflightCorpusError(Exception):
    """Raised when a record would violate the control-plane allowlist or the class set."""


@dataclass(frozen=True)
class PreflightRun:
    """Exactly what may be persisted — a timestamp, an opaque surface id, the content
    fingerprint, the CLOSED failure classes found, and per-severity counts. Frozen so it
    can't accrete a field at runtime; the field set IS the persisted schema
    (``ALLOWED_KEYS``). There is no field through which a payload or arg value could enter."""

    ts: int
    surface_id: str
    surface_rev: str
    classes: list[str]  # CLOSED-set failure-class NAMES, never a value or message
    counts: dict[str, int] = field(
        default_factory=dict
    )  # {"error": n, "warning": m, ...}


ALLOWED_KEYS = frozenset(PreflightRun.__dataclass_fields__)


def assert_allowlisted(mapping: Mapping[str, Any]) -> None:
    """Reject (fail closed) any key not on the ``PreflightRun`` allowlist."""
    extra = set(mapping) - ALLOWED_KEYS
    if extra:
        raise PreflightCorpusError(
            f"non-allowlisted preflight key(s) would be persisted: {sorted(extra)}"
        )


def assert_classes_closed(classes: list[str]) -> None:
    """Reject (fail closed) any class outside the CLOSED ``KNOWN_CLASSES`` set.

    This is the guarantee a class name can never smuggle a value out: it must be one of
    the vocabulary constants, or the write fails."""
    stray = [c for c in classes if c not in KNOWN_CLASSES]
    if stray:
        raise PreflightCorpusError(
            f"preflight class(es) not in the closed vocabulary: {sorted(set(stray))}"
        )


def to_record(run: PreflightRun) -> dict[str, Any]:
    """Serialize to a plain dict, enforcing the allowlist AND the closed class set before
    it can be written."""
    record_dict = asdict(run)
    assert_allowlisted(record_dict)
    assert_classes_closed(run.classes)
    return record_dict


def record(run: PreflightRun, path: str | Path) -> None:
    """Append one allowlisted JSONL record. A control-plane violation (a non-allowlisted
    key or an off-vocabulary class) surfaces; any operational IO failure is swallowed with
    a redacted note (the record is never echoed, to avoid re-leaking input)."""
    try:
        record_dict = to_record(run)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record_dict) + "\n")
    except PreflightCorpusError:
        raise  # a control-plane violation must surface, not be swallowed
    except Exception:  # noqa: BLE001 - best-effort; never break the gate
        import logging

        logging.getLogger("gecko.preflight_corpus").warning(
            "preflight corpus write failed (redacted)"
        )


def known_classes_from_corpus(path: str | Path | None) -> set[str]:
    """The cross-API read-path — the flywheel made architecturally present.

    Return the distinct failure classes recorded by PRIOR runs (across every surface the
    corpus has ever seen). Preflight unions this with the ``KNOWN_CLASSES`` seed to form
    the set of classes it WATCHES on the current surface, so a class first observed on
    API #1 is surfaced as watched when checking API #2 — even before v1 grows a dedicated
    detector for it. Only closed-vocabulary classes are returned (a corrupt line is
    skipped); nothing but class NAMES ever leaves the file."""
    if path is None:
        return set()
    p = Path(path)
    if not p.exists():
        return set()
    seen: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        classes = rec.get("classes") if isinstance(rec, dict) else None
        if isinstance(classes, list):
            seen.update(c for c in classes if c in KNOWN_CLASSES)
    return seen
