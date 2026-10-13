"""Has a source changed under us since its last run? Read from the field ledger only.

Two fingerprints, both built from ledger rows of EARLIER runs in the same mode:

* per endpoint — the output field NAMES its replies carried, with the last run each
  name was seen in. A reply that no longer carries the source's declared
  ``field_path``, when an earlier run did, has drifted: "field path missing". The
  comparison is against the last run the path was SEEN, not the last run: otherwise
  one drifted run would become the new normal, and the run after it would trust a
  reader that is reading the wrong shape. It stays drifted until the path is back
  or the source map changes.
* per (endpoint, supplier) — whether that supplier's call answered last run. A 4xx or
  an empty reply where it used to answer has drifted too.

A first run (no baseline) can fail, but it cannot drift: drift is a claim about a
change, and there is nothing to change from. 401/402/403/429 are key, credit and rate
problems, not a changed contract, so they never count as drift.

Same idea as ``paysh_catalog.RefreshDiff`` (previous vs current, per item), applied to
the shape of one reply rather than to a catalog.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

DriftKind = Literal["path missing", "status", "empty"]
_NOT_DRIFT_STATUSES = frozenset({401, 402, 403, 429})


@dataclass(frozen=True)
class Fingerprint:
    api: str
    path: str
    run_id: str
    fields: frozenset[str]


@dataclass
class Baseline:
    #: The fields of the last run each endpoint answered (for reporting).
    prints: dict[tuple[str, str], Fingerprint] = field(default_factory=dict)
    #: endpoint -> field name -> the last run it was seen in (for the drift check).
    seen: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    #: (api, path, subject) -> (run_id, answered with a non-empty reply)
    answered: dict[tuple[str, str, str], tuple[str, bool]] = field(default_factory=dict)


@dataclass(frozen=True)
class Drift:
    kind: DriftKind
    #: Short, for the one-line story: "field path missing".
    short: str
    #: Full, for the ledger and the table note.
    reason: str


def _is_first_page_call(row: dict[str, Any]) -> bool:
    return row.get("kind") in (None, "call") and int(row.get("page") or 1) == 1


def _answered(row: dict[str, Any]) -> bool:
    return row.get("status") == "ok" and bool(row.get("output_fields"))


def baseline_from(
    rows: Iterable[dict[str, Any]], *, mode: str, before_run: str
) -> Baseline:
    """The fingerprints as of the runs before ``before_run`` (run ids sort by time)."""
    baseline = Baseline()
    for row in rows:
        run_id = str(row.get("run_id") or "")
        if row.get("mode") != mode or not run_id or run_id >= before_run:
            continue
        if not _is_first_page_call(row):
            continue
        key = (str(row.get("api")), str(row.get("path")))
        if _answered(row):
            seen = baseline.prints.get(key)
            names = frozenset(str(n) for n in row.get("output_fields") or [])
            if seen is None or run_id > seen.run_id:
                baseline.prints[key] = Fingerprint(*key, run_id, names)
            elif run_id == seen.run_id:
                baseline.prints[key] = Fingerprint(*key, run_id, seen.fields | names)
            last = baseline.seen.setdefault(key, {})
            for name in names:
                if run_id > last.get(name, ""):
                    last[name] = run_id
        subject = str(row.get("subject") or "")
        if subject:
            prior = baseline.answered.get((*key, subject))
            if prior is None or run_id >= prior[0]:
                baseline.answered[(*key, subject)] = (run_id, _answered(row))
    return baseline


def check(row: dict[str, Any], field_path: str, baseline: Baseline) -> Drift | None:
    """``row``: this run's first-page ledger row for the source's call."""
    key = (str(row.get("api")), str(row.get("path")))
    prior = baseline.answered.get((*key, str(row.get("subject") or "")))
    prior_run, used_to_answer = prior if prior is not None else ("", False)
    status = row.get("http_status")
    if row.get("status") != "ok":
        if (
            used_to_answer
            and isinstance(status, int)
            and 400 <= status < 500
            and status not in _NOT_DRIFT_STATUSES
        ):
            return Drift(
                "status",
                f"HTTP {status} where it answered",
                f"answered HTTP {status} where it answered in run {prior_run}",
            )
        return None
    if not row.get("output_fields"):
        if used_to_answer:
            return Drift(
                "empty",
                "empty reply where it answered",
                f"answered empty where it answered in run {prior_run}",
            )
        return None
    last_seen = baseline.seen.get(key, {}).get(field_path)
    if last_seen and field_path not in row["output_fields"]:
        return Drift(
            "path missing",
            "field path missing",
            f"field path missing: {field_path} (present in run {last_seen})",
        )
    return None
