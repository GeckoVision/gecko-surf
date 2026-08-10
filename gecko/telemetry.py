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
3. **Egress is GLOBAL-only; the per-surface split is LOCAL-only.** ``aggregate`` and
   ``TelemetryPayload`` are never grouped by ``surface_id`` — an API-by-API profile of a
   consumer's stack is exactly what the control-plane promise says we do not collect.
   ``per_surface_fcc`` / ``surface_rows`` answer the local question ("which of MY APIs is
   dragging the rate down") from a local file, take no sink, and are not allowlisted.
4. **Phone-home ships DISABLED.** ``report`` is no-op by default (the default sink
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
from collections.abc import Callable, Iterator, Mapping
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
# Per-surface FCC — the LOCAL drill-down under the one global rate
# --------------------------------------------------------------------------- #
#: The corpus tier that gates every published rate. A ``Literal`` from ``corpus`` so a
#: typo is a build break and the tier is named in exactly one place.
OBSERVED_SOURCE: corpus.OutcomeSource = "observed"

#: Spelled out, not ``__name__``: run as ``python -m gecko.telemetry`` the module name is
#: ``__main__``, and a block that mislabels its own producer is worse than an unlabeled one.
_MODULE = "gecko.telemetry"

#: The single arm these numbers measure. There is NO without-Gecko control arm anywhere in
#: the corpus — every row was produced by a call Gecko prepared. So a per-surface rate is
#: "how often the first call worked WHEN GECKO PREPARED IT", never "how much Gecko helped".
#: Carried on every block so a screenshotted number keeps its own caveat.
FCC_ARM = "observed-with-gecko"

# OPEN — FOUNDER'S CALL, deliberately NOT decided here.
# The group key below is ``surface_id``, and ``surface_id`` DEFAULTS TO THE API HOSTNAME
# (``client.py``: ``surface_id or _host_of(self.base_url)``). Locally that is exactly right
# — an operator grouping their OWN corpus wants to read "api.stripe.com". It becomes a
# question the moment anything per-surface is exported or pooled, because the group key
# would then name which vendors a consumer integrates. Which is why these functions are
# local-only and not allowlisted: the question does not have to be answered to ship this.
# The options, with their costs, for whoever answers it:
#   (a) keyed hash of surface_id  — comparable across runs of ONE install, opaque to us;
#       but a hostname is low-entropy, so an unsalted digest is reversible by dictionary
#       and the key becomes a thing we must hold and rotate.
#   (b) per-surface opt-in        — honest and revocable, and it makes the corpus a
#       biased sample of exactly the surfaces people are willing to admit to.
#   (c) hold                      — keep it local forever; the split stays a product
#       feature, never a metric we can quote across installs.
# Nothing downstream of this module may treat (a) as already chosen.


@dataclass(frozen=True)
class SurfaceFcc:
    """One API's observed first-call-correct rate. Emitted ONLY when the surface has at
    least one qualifying row — absence means "not evaluated", never ``0.0``."""

    surface_id: str
    observed_calls: int  # this entry's OWN denominator
    first_call_correct: int
    first_call_correct_rate: float
    arm: str
    provenance: str
    produced_by: str


@dataclass(frozen=True)
class PerSurfaceFcc:
    """The per-surface split of the global rate — a LOCAL analysis artifact.

    Not a telemetry payload: nothing here is allowlisted for egress, and by design it never
    will be (a per-API profile of a consumer's stack is exactly the thing the control-plane
    promise says we do not collect). ``aggregate``/``TelemetryPayload`` stay GLOBAL-only.
    """

    entries: tuple[
        SurfaceFcc, ...
    ]  # qualifying surfaces only, biggest denominator first
    observed_calls: int  # the block denominator == sum of the entries'
    surfaces_not_evaluated: (
        int  # COUNT of suppressed surfaces; they are never NAMED here
    )
    arm: str
    provenance: str
    produced_by: str

    def get(self, surface_id: str) -> SurfaceFcc | None:
        """The entry for ``surface_id``, or ``None`` for "not evaluated"."""
        for entry in self.entries:
            if entry.surface_id == surface_id:
                return entry
        return None

    def rate_for(self, surface_id: str) -> float | None:
        """The rate, or ``None`` for NOT EVALUATED — deliberately distinct from ``0.0``.

        ``0.0`` is a finding (we measured this API and every observed first call failed).
        ``None`` is silence (we have no qualifying evidence). Collapsing the two is the
        zero-value bug this repo keeps re-hitting: a surface nobody ever called then reads
        as a surface that never works.
        """
        entry = self.get(surface_id)
        return None if entry is None else entry.first_call_correct_rate


@dataclass(frozen=True)
class SurfaceRow:
    """One operation's contribution to a surface's rate. Names, shapes and counts only —
    ``path_template`` is templated (``/x/{id}``), never a filled URL."""

    operation_id: str
    method: str
    path_template: str
    observed_calls: int
    first_call_correct: int
    first_call_correct_rate: float
    error_class_distribution: dict[str, int]


@dataclass(frozen=True)
class SurfaceRows:
    """The operation-level drill-down under ONE ``SurfaceFcc``. Reconciles exactly with it:
    the row denominators sum to the entry's."""

    surface_id: str
    rows: tuple[SurfaceRow, ...]  # biggest denominator first
    observed_calls: int
    first_call_correct: int
    first_call_correct_rate: float
    arm: str
    provenance: str
    produced_by: str


def _corpus_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the dict rows of ONE corpus file, and nothing else.

    Reads EXACTLY the path it is given. It must never derive and read a sibling: the
    synthetic / simulated / self-check tiers are segregated BY PATH precisely so a reader
    that has never heard of a tier cannot count it (fail closed). Walking to a sibling here
    would re-open the denominator hole that segregation closes — a ``verify-docs`` 404 on
    an id GECKO invented is evidence about our placeholder, not about an agent's first call.

    Best-effort like ``aggregate``: a malformed line is skipped, a missing file yields
    nothing.
    """
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # best-effort: tolerate a partial/corrupt append
            if isinstance(record, dict):
                yield record


def _rate(correct: int, total: int) -> float:
    return round(correct / total, 4) if total else 0.0


def per_surface_fcc(corpus_path: str | Path) -> PerSurfaceFcc:
    """Split the observed first-call-correct rate by ``surface_id`` — STRICTLY LOCAL.

    The same denominator ``aggregate`` publishes, sliced: ``source == "observed"`` rows of
    the given file only. No pooling, no egress, no network — it reads one JSONL and returns
    counts. An UNLABELED row (no ``source`` — e.g. a legacy record) fails CLOSED: excluded
    from the rate rather than silently inflating it.

    A surface with ZERO qualifying rows gets NO entry (it is only COUNTED in
    ``surfaces_not_evaluated``, never named): a thing that could not answer must not
    answer. Use ``rate_for``/``get`` to tell that ``None`` from a measured ``0.0``.

    Pure function of the file's contents. There is no request/session/run scope — the
    corpus is a permanent, append-only artifact reused across runs, and gating a static
    corpus behind a request id is how it silently disappears.
    """
    path = Path(corpus_path)
    seen: set[str] = set()
    totals: dict[str, int] = {}
    correct: dict[str, int] = {}

    for record in _corpus_records(path):
        surface_id = record.get("surface_id")
        if not isinstance(surface_id, str):
            continue
        seen.add(surface_id)
        if record.get("source") != OBSERVED_SOURCE:
            continue
        totals[surface_id] = totals.get(surface_id, 0) + 1
        if record.get("first_call_correct") is True:
            correct[surface_id] = correct.get(surface_id, 0) + 1

    produced_by = f"{_MODULE}.per_surface_fcc"
    entries = tuple(
        SurfaceFcc(
            surface_id=surface_id,
            observed_calls=total,
            first_call_correct=correct.get(surface_id, 0),
            first_call_correct_rate=_rate(correct.get(surface_id, 0), total),
            arm=FCC_ARM,
            provenance=OBSERVED_SOURCE,
            produced_by=produced_by,
        )
        # Deterministic: biggest denominator first, ties broken by id — the split leads
        # with the surface that actually moves the global number.
        for surface_id, total in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return PerSurfaceFcc(
        entries=entries,
        observed_calls=sum(entry.observed_calls for entry in entries),
        surfaces_not_evaluated=len(seen - set(totals)),
        arm=FCC_ARM,
        provenance=OBSERVED_SOURCE,
        produced_by=produced_by,
    )


def surface_rows(corpus_path: str | Path, surface_id: str) -> SurfaceRows | None:
    """The per-operation drill-down under ONE surface's rate — STRICTLY LOCAL.

    Returns ``None`` when the surface has no qualifying rows: NOT EVALUATED, deliberately
    distinct from an empty block, which a reader would render as a measured zero. Same
    file-scoped, sibling-free read as ``per_surface_fcc``, so a self-check row cannot
    appear here either.
    """
    path = Path(corpus_path)
    totals: dict[str, int] = {}
    correct: dict[str, int] = {}
    errors: dict[str, dict[str, int]] = {}
    invoke: dict[str, tuple[str, str]] = {}

    for record in _corpus_records(path):
        if record.get("surface_id") != surface_id:
            continue
        if record.get("source") != OBSERVED_SOURCE:
            continue
        operation_id = record.get("operation_id")
        if not isinstance(operation_id, str):
            continue
        totals[operation_id] = totals.get(operation_id, 0) + 1
        if record.get("first_call_correct") is True:
            correct[operation_id] = correct.get(operation_id, 0) + 1
        error_class = record.get("error_class")
        # Counted as-is (like ``aggregate``): a non-closed class can only reach here from
        # an already-corrupt local file, and this block never egresses.
        if isinstance(error_class, str) and error_class != "none":
            bucket = errors.setdefault(operation_id, {})
            bucket[error_class] = bucket.get(error_class, 0) + 1
        if operation_id not in invoke:
            method = record.get("method")
            path_template = record.get("path_template")
            invoke[operation_id] = (
                method if isinstance(method, str) else "",
                path_template if isinstance(path_template, str) else "",
            )

    if not totals:
        return None  # not evaluated — never an empty block that reads as a zero

    rows = tuple(
        SurfaceRow(
            operation_id=operation_id,
            method=invoke[operation_id][0],
            path_template=invoke[operation_id][1],
            observed_calls=total,
            first_call_correct=correct.get(operation_id, 0),
            first_call_correct_rate=_rate(correct.get(operation_id, 0), total),
            error_class_distribution=dict(errors.get(operation_id, {})),
        )
        for operation_id, total in sorted(
            totals.items(), key=lambda kv: (-kv[1], kv[0])
        )
    )
    observed_calls = sum(row.observed_calls for row in rows)
    first_call_correct = sum(row.first_call_correct for row in rows)
    return SurfaceRows(
        surface_id=surface_id,
        rows=rows,
        observed_calls=observed_calls,
        first_call_correct=first_call_correct,
        first_call_correct_rate=_rate(first_call_correct, observed_calls),
        arm=FCC_ARM,
        provenance=OBSERVED_SOURCE,
        produced_by=f"{_MODULE}.surface_rows",
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

    Creation is ATOMIC (``O_CREAT | O_EXCL``) and the winner's id is re-read, so two
    processes starting at once converge on ONE id. The read-then-write version had a
    TOCTOU race: both saw "absent", both minted a uuid, and each reported a different
    install — which is exactly how 50 phantom "installs" were recorded in a four-day
    window from a single machine.
    """
    target = Path(path) if path is not None else default_install_id_path()

    def _read() -> str | None:
        try:
            existing = target.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return existing or None

    found = _read()
    if found:
        return found

    new_id = str(uuid.uuid4())
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write the id to a private temp file FIRST, then publish it with an atomic link.
        # An exclusive-create-then-write would still race: the loser can open the file in
        # the window after creation but before the write and read it empty, then fall back
        # to its own uuid — the same split identity, just harder to reproduce. Linking a
        # fully-written file means the target is never observed incomplete. The staged
        # name is keyed on the candidate id, not the pid — concurrent THREADS share a
        # pid and would otherwise unlink each other's staging file.
        staged = target.with_name(f"{target.name}.{new_id}.tmp")
        staged.write_text(new_id + "\n", encoding="utf-8")
        try:
            os.link(staged, target)
            return new_id
        except FileExistsError:
            # Another process published first — theirs is this machine's identity.
            return _read() or new_id
        finally:
            staged.unlink(missing_ok=True)
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
