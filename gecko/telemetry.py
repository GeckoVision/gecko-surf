"""Opt-out, control-plane-safe usage telemetry — measure adoption, not data.

This is the Surfpool-style "built-in CLI telemetry instead of an email waitlist."
It COMPOSES on the correctness corpus (``gecko.corpus``): it reads ONLY the
already-control-plane-safe ``CallOutcome`` records and emits ANONYMIZED COUNTS.

Three structural guarantees, mirroring ``corpus.py``:

1. **Aggregator, never a copier.** ``aggregate`` reads the corpus and returns
   counts/rates only — it derives every metric from existing ``CallOutcome`` fields
   and never copies a value, path, surface name, or token out.
2. **The payload IS the schema.** ``TelemetryPayload`` is a frozen dataclass whose
   field set is the allowlist (``PAYLOAD_ALLOWED_KEYS``). There is NO field that
   could hold a value/payload/token, and ``assert_payload_allowlisted`` fails closed
   on any non-allowlisted key — including a free-text ``error_class`` (which could
   otherwise smuggle a value), gated to the closed ``corpus.ERROR_CLASSES`` set.
3. **Phone-home ships DISABLED.** ``report`` is no-op by default (the default sink
   does nothing) and ``GECKO_TELEMETRY=off`` hard-disables it. Flipping default-on
   capture in the data path is an unratified founder decision (spec §7-#1) — this
   module does NOT make that call. Best-effort: it never raises into the call path,
   except a control-plane violation, which MUST surface.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import __version__
from . import corpus

logger = logging.getLogger("gecko.telemetry")

#: A phone-home sink receives ONLY the validated, allowlisted record (a plain dict).
Sink = Callable[[Mapping[str, Any]], None]

_TELEMETRY_ENV = "GECKO_TELEMETRY"
_DISABLED_VALUES = frozenset({"off", "0", "false", "no", "disabled"})


class TelemetryError(Exception):
    """Raised when a payload would violate the control-plane allowlist (fail closed)."""


# --------------------------------------------------------------------------- #
# Aggregate — anonymized counts derived from CallOutcome records only
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UsageAggregate:
    """Anonymized usage counts. Every field is a count/rate derived from existing
    ``CallOutcome`` fields — never a raw value, surface name, path, or token."""

    surfaces_comprehended: int  # distinct surface_id count (coverage; all real rows)
    surface_revs: int  # distinct surface_rev count (coverage; all real rows)
    total_calls: int  # OBSERVED calls only (the FCC-rate denominator)
    first_call_correct_rate: float  # OBSERVED-only — the published moat metric
    error_class_distribution: dict[str, int]  # OBSERVED-only counts per CLOSED class
    drift_events: int  # surface_rev transitions observed per surface_id, summed


def aggregate(corpus_path: str | Path) -> UsageAggregate:
    """Read a corpus JSONL and compute anonymized usage metrics.

    The published real-usage metrics (``total_calls`` / ``first_call_correct_rate`` /
    ``error_class_distribution``) are computed over ``source == "observed"`` rows ONLY.
    This is the moat-metric fix: a faked recorded ``200`` (``source == "synthetic"``)
    or a survivorship-biased agent report (``source == "reported"``) must never move
    the first-call-correct rate. Synthetic rows are additionally segregated to a
    separate file by ``corpus.record``, so a main-corpus read never even sees them;
    this filter is the belt-and-suspenders for reported rows and any legacy/mixed file.
    An UNLABELED row (no ``source`` — e.g. a legacy record) fails CLOSED: it is treated
    as non-observed and excluded from the rate rather than silently inflating it.

    Coverage counts (``surfaces_comprehended`` / ``surface_revs`` / ``drift_events``)
    span all real rows read.

    Best-effort and append-only-friendly: a malformed line is skipped, a missing
    file yields zeros. Reads ONLY metadata fields; never opens a value.
    """
    path = Path(corpus_path)
    surfaces: set[str] = set()
    revs: set[str] = set()
    total = 0
    first_call_correct = 0
    errors: dict[str, int] = {}
    drift_events = 0
    last_rev: dict[str, str] = {}

    if not path.exists():
        return UsageAggregate(0, 0, 0, 0.0, {}, 0)

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # best-effort: tolerate a partial/corrupt append
            if not isinstance(record, dict):
                continue
            surface_id = record.get("surface_id")
            surface_rev = record.get("surface_rev")
            if isinstance(surface_id, str):
                surfaces.add(surface_id)
            if isinstance(surface_rev, str):
                revs.add(surface_rev)
            if isinstance(surface_id, str) and isinstance(surface_rev, str):
                previous = last_rev.get(surface_id)
                if previous is not None and previous != surface_rev:
                    drift_events += 1
                last_rev[surface_id] = surface_rev
            # OBSERVED-ONLY from here: the FCC rate / total / error dist must never mix
            # in a faked recorded 200 (synthetic) or a reported row. Fails closed on an
            # unlabeled (sourceless) row.
            if record.get("source") != "observed":
                continue
            total += 1
            if record.get("first_call_correct") is True:
                first_call_correct += 1
            error_class = record.get("error_class")
            if isinstance(error_class, str):
                # Count as-is; a non-closed class fails closed at EMIT, never here —
                # so a corrupt corpus can't silently phone home an arbitrary string.
                errors[error_class] = errors.get(error_class, 0) + 1

    rate = round(first_call_correct / total, 4) if total else 0.0
    return UsageAggregate(
        surfaces_comprehended=len(surfaces),
        surface_revs=len(revs),
        total_calls=total,
        first_call_correct_rate=rate,
        error_class_distribution=errors,
        drift_events=drift_events,
    )


# --------------------------------------------------------------------------- #
# Payload — the allowlist IS the schema (no field can carry a value/token)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TelemetryPayload:
    """Exactly what is allowed to leave the machine: an opaque install id, the
    version, a timestamp, and anonymized aggregate counts. Frozen so it cannot
    accrete a field at runtime — the field set IS ``PAYLOAD_ALLOWED_KEYS``."""

    install_id: str  # opaque uuid4, no PII, not derived from anything identifying
    version: str
    ts: int
    surfaces_comprehended: int
    surface_revs: int
    total_calls: int
    first_call_correct_rate: float
    error_class_distribution: dict[str, int]
    drift_events: int


PAYLOAD_ALLOWED_KEYS = frozenset(TelemetryPayload.__dataclass_fields__)


def assert_payload_allowlisted(mapping: Mapping[str, Any]) -> None:
    """Reject (fail closed) any non-allowlisted top-level key, and any error_class
    key outside the closed ``corpus.ERROR_CLASSES`` set (a free-text key could
    otherwise smuggle a value)."""
    extra = set(mapping) - PAYLOAD_ALLOWED_KEYS
    if extra:
        raise TelemetryError(
            f"non-allowlisted telemetry key(s) would be emitted: {sorted(extra)}"
        )
    distribution = mapping.get("error_class_distribution") or {}
    if isinstance(distribution, Mapping):
        bad = set(distribution) - corpus.ERROR_CLASSES
        if bad:
            raise TelemetryError(
                f"error_class key(s) not in the closed set: {sorted(bad)}"
            )


def to_payload_record(payload: TelemetryPayload) -> dict[str, Any]:
    """Serialize a payload to a plain dict, enforcing the allowlist before emit."""
    record = asdict(payload)
    assert_payload_allowlisted(record)
    return record


# --------------------------------------------------------------------------- #
# install_id — a random opaque uuid4, persisted once
# --------------------------------------------------------------------------- #
def default_install_id_path() -> Path:
    """``~/.gecko/install-id`` — the canonical location."""
    return Path.home() / ".gecko" / "install-id"


def read_or_create_install_id(path: str | Path | None = None) -> str:
    """Read the opaque install id, creating a random uuid4 once if absent.

    No PII, not derived from anything identifying. Best-effort persistence: if the
    file can't be written, returns an ephemeral id rather than raising.
    """
    target = Path(path) if path is not None else default_install_id_path()
    try:
        if target.exists():
            existing = target.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:
        pass
    new_id = str(uuid.uuid4())
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_id + "\n", encoding="utf-8")
    except OSError:
        logger.warning("could not persist install-id (ephemeral this run)")
    return new_id


def build_payload(
    corpus_path: str | Path,
    *,
    version: str = __version__,
    install_id_path: str | Path | None = None,
    ts: int | None = None,
) -> TelemetryPayload:
    """Compose an anonymized aggregate with the opaque install identity."""
    agg = aggregate(corpus_path)
    return TelemetryPayload(
        install_id=read_or_create_install_id(install_id_path),
        version=version,
        ts=ts if ts is not None else int(time.time() * 1000),
        surfaces_comprehended=agg.surfaces_comprehended,
        surface_revs=agg.surface_revs,
        total_calls=agg.total_calls,
        first_call_correct_rate=agg.first_call_correct_rate,
        error_class_distribution=agg.error_class_distribution,
        drift_events=agg.drift_events,
    )


# --------------------------------------------------------------------------- #
# Opt-out + phone-home (ships DISABLED; opt-in via an injected sink)
# --------------------------------------------------------------------------- #
def telemetry_enabled() -> bool:
    """False iff ``GECKO_TELEMETRY`` is set to an off-like value. The local aggregate
    is always computable; this only gates the phone-home step."""
    return os.environ.get(_TELEMETRY_ENV, "").strip().lower() not in _DISABLED_VALUES


def _noop_sink(_record: Mapping[str, Any]) -> None:
    """The DEFAULT sink — does nothing. Telemetry never phones home unless the
    consumer injects a real sink (opt-in). This keeps default-on capture OUT of the
    data path (spec §7-#1, unratified)."""
    return None


def report(payload: TelemetryPayload, sink: Sink | None = None) -> bool:
    """Best-effort phone-home. Returns True only if a real (non-default) sink
    received the payload.

    Ships disabled: with no injected sink the no-op default runs, so importing/using
    gecko-surf never phones home. ``GECKO_TELEMETRY=off`` short-circuits even an
    injected sink. The payload is allowlist-validated BEFORE the sink can see it, so
    a control-plane violation surfaces (fail closed); every other error is swallowed
    so telemetry never breaks the agent's call path.
    """
    if not telemetry_enabled():
        return False
    # Validate first — a control-plane/allowlist violation MUST surface, not be sent.
    record = to_payload_record(payload)
    chosen = _noop_sink if sink is None else sink
    try:
        chosen(record)
    except TelemetryError:
        raise  # a control-plane violation must surface, never be swallowed
    except Exception:  # noqa: BLE001 - best-effort; never break the call path
        logger.warning("telemetry report failed (redacted)")
        return False
    return chosen is not _noop_sink


def _run() -> None:  # CLI: gecko-telemetry <corpus.jsonl> — read-only, no network.
    import sys

    argv = sys.argv[1:]
    if not argv:
        print("usage: gecko-telemetry <corpus.jsonl>", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(asdict(aggregate(argv[0])), indent=2))
    raise SystemExit(0)


if __name__ == "__main__":
    _run()
