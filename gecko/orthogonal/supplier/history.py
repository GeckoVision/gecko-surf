"""Stored runs, read back: the last good value per field, latest, history, changes.

Everything here works on the rows ``store.read_runs`` returns (the per-run Parquet
files), so it is free and makes no call. What goes to a model is ``safe_row``: code-
built summaries, sources and states — never ``value`` or ``value_summary``, which may
quote upstream text.

Rows are keyed by (subject, field). The subject column is ``supplier`` for the
supplier watch and ``mint`` for the living file (``subject_key``); everything else —
states, last good value, the diff — is the same code for both.

Runs written before the ``value`` column existed (the two live runs of 2026-10-08)
still give an entity snapshot: when one name was shown (agree / only one), the
summary IS the sanitized name. No other legacy field is read as a value.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..errors import ListJobError
from .fields import FIELD_ENTITY

ChangeKind = Literal["added", "changed", "removed"]
#: The subject column of the supplier watch; another job passes its own.
SUPPLIER_KEY = "supplier"
#: The only columns a tool result carries. Our words, never upstream text.
SAFE_KEYS = (
    "run_id",
    "mode",
    "generated_at",
    "supplier",
    "field",
    "model_summary",
    "source",
    "second_source",
    "agreement",
    "status",
    "filled_by",
    "failover",
    "note",
    "stale_since",
)
_GOOD = ("fresh", "repaired")


class RunNotFoundError(ListJobError):
    code = "run_not_found"


def state_of(row: dict[str, Any]) -> str:
    status = row.get("status")
    if isinstance(status, str) and status:
        return status
    return "no data" if row.get("agreement") == "no data" else "fresh"


def value_of(row: dict[str, Any]) -> str | None:
    value = row.get("value")
    if isinstance(value, str) and value:
        return value
    if (
        "value" not in row
        and row.get("field") == FIELD_ENTITY
        and row.get("agreement") in ("agree", "only one")
    ):
        summary = row.get("value_summary")
        return summary if isinstance(summary, str) and summary else None
    return None


def safe_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {key: row.get(key) for key in SAFE_KEYS}
    out["status"] = state_of(row)
    return out


@dataclass(frozen=True)
class Snapshot:
    """The last good value of one (supplier, field), and the row it came from."""

    run_id: str
    value: str
    row: dict[str, Any]


@dataclass(frozen=True)
class Prior:
    last_good: Snapshot | None
    #: Set when the most recent stored rows are stale: the run it went stale in.
    stale_since: str | None


def _by_run(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: str(r.get("run_id") or ""))


def priors(
    rows: list[dict[str, Any]], *, mode: str, subject_key: str = SUPPLIER_KEY
) -> dict[tuple[str, str], Prior]:
    good: dict[tuple[str, str], Snapshot] = {}
    stale: dict[tuple[str, str], str | None] = {}
    for row in _by_run([r for r in rows if r.get("mode") == mode]):
        key = (str(row.get(subject_key)), str(row.get("field")))
        state, value = state_of(row), value_of(row)
        if state in _GOOD and value:
            good[key] = Snapshot(str(row.get("run_id")), value, row)
            stale[key] = None
        elif state == "stale":
            stale[key] = stale.get(key) or row.get("stale_since") or row.get("run_id")
    keys = set(good) | set(stale)
    return {k: Prior(good.get(k), stale.get(k)) for k in keys}


def run_ids(rows: list[dict[str, Any]], mode: str | None = None) -> list[str]:
    return sorted(
        {
            str(r.get("run_id"))
            for r in rows
            if r.get("run_id") and (mode is None or r.get("mode") == mode)
        }
    )


def latest(rows: list[dict[str, Any]], mode: str | None = None) -> dict[str, Any]:
    ids = run_ids(rows, mode)
    if not ids:
        return {"run_id": None, "rows": [], "note": "No stored runs yet."}
    last = ids[-1]
    mine = [safe_row(r) for r in rows if r.get("run_id") == last]
    return {"run_id": last, "mode": mine[0]["mode"], "rows": mine}


def history(rows: list[dict[str, Any]], supplier: str, field_name: str) -> list[dict]:
    return [
        safe_row(r)
        for r in _by_run(rows)
        if r.get(SUPPLIER_KEY) == supplier and r.get("field") == field_name
    ]


@dataclass(frozen=True)
class Change:
    #: The supplier name, or the mint: whatever the job's ``subject_key`` column holds.
    subject: str
    field: str
    change: ChangeKind
    #: Which of value / source / status / agreement moved (for "changed").
    what: tuple[str, ...] = ()
    source_before: str | None = None
    source_after: str | None = None
    status_before: str | None = None
    status_after: str | None = None
    summary_before: str | None = None
    summary_after: str | None = None

    @property
    def supplier(self) -> str:
        return self.subject

    @property
    def source_changed(self) -> bool:
        return "source" in self.what


@dataclass
class ChangeSet:
    since_run: str
    against_run: str | None
    changes: list[Change] = field(default_factory=list)
    note: str = ""
    #: The name the subject goes out under (``supplier`` / ``mint``).
    subject_key: str = SUPPLIER_KEY

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        key = out.pop("subject_key")
        for change, raw in zip(self.changes, out["changes"], strict=True):
            raw[key] = raw.pop("subject")
            raw["source_changed"] = change.source_changed
        return out


def _comparable(row: dict[str, Any]) -> str:
    return value_of(row) or str(row.get("value_summary") or "")


def _diff(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, ...]:
    moved = []
    if _comparable(before) != _comparable(after):
        moved.append("value")
    if _source(before) != _source(after):
        moved.append("source")
    if state_of(before) != state_of(after):
        moved.append("status")
    if before.get("agreement") != after.get("agreement"):
        moved.append("agreement")
    return tuple(moved)


def changes_since(
    rows: list[dict[str, Any]],
    run_id: str,
    *,
    to_run: str | None = None,
    subject_key: str = SUPPLIER_KEY,
    latest_hint: str = "supplier_latest",
) -> ChangeSet:
    """``run_id`` against ``to_run`` (default: the latest later run in the same mode),
    per (subject, field): added, changed, removed, and whether the source moved."""
    base = [r for r in rows if r.get("run_id") == run_id]
    if not base:
        raise RunNotFoundError(f"No stored run {run_id!r}. Ask for {latest_hint}.")
    mode = base[0].get("mode")
    later = [i for i in run_ids(rows, str(mode)) if i > run_id]
    target = to_run or (later[-1] if later else None)
    if target is None:
        return ChangeSet(
            run_id,
            None,
            note=f"No {mode} run after {run_id} yet.",
            subject_key=subject_key,
        )
    after = [r for r in rows if r.get("run_id") == target]
    if not after:
        raise RunNotFoundError(f"No stored run {target!r}.")
    old = {(str(r[subject_key]), str(r["field"])): r for r in base}
    new = {(str(r[subject_key]), str(r["field"])): r for r in after}
    out = ChangeSet(run_id, target, subject_key=subject_key)
    for key in sorted(set(old) | set(new)):
        change = _change(key, old.get(key), new.get(key))
        if change is not None:
            out.changes.append(change)
    return out


def _source(row: dict[str, Any]) -> str | None:
    return row.get("filled_by") or row.get("source")


def _change(
    key: tuple[str, str], a: dict[str, Any] | None, b: dict[str, Any] | None
) -> Change | None:
    if a is None and b is not None:
        return Change(
            *key,
            "added",
            source_after=_source(b),
            status_after=state_of(b),
            summary_after=b.get("model_summary"),
        )
    if b is None and a is not None:
        return Change(
            *key,
            "removed",
            source_before=_source(a),
            status_before=state_of(a),
            summary_before=a.get("model_summary"),
        )
    if a is None or b is None:
        return None
    moved = _diff(a, b)
    if not moved:
        return None
    return Change(
        *key,
        "changed",
        moved,
        _source(a),
        _source(b),
        state_of(a),
        state_of(b),
        a.get("model_summary"),
        b.get("model_summary"),
    )
