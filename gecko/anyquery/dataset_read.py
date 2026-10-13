"""Shared pieces of the dataset endpoint: who is asking, the argument checks, the cell
view, masking per answer, and the diff between two versions of a table.

WHO. The caller is a :class:`DatasetCaller`: the dataset named by the PATH and the
:class:`~gecko.anyquery.reader_keys.DatasetAccess` the presented key resolved to. No
argument names a customer or a dataset. A dataset that does not exist and one outside
the key's scope get the same answer (:func:`readable`), so an answer never shows
whether a slug exists.

MASKING. A value is shown through :func:`~gecko.anyquery.reader_keys.shown_value`: the
ask (``reveal_pii``) AND the key (only the owner's own key may reveal). Masked is the
default even for the owner, because an answer lands in a transcript. A SUBJECT goes
through the same rule (:func:`shown_subject`): "enrich these emails" makes the subject
itself a contact detail. A caller that cannot see a subject whole names it, in a
``subject`` filter, by the masked form it was shown, never the raw one, so the filter
is no oracle for "is this person in the dataset?".
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from gecko.orthogonal.supplier.text import untrusted_summary

from .bundles import BUNDLE_NAME_RE
from .dataset_tools import (
    DATASET_HINT,
    MAX_COLUMNS,
    TOOL_PROPS,
    TOOL_REQUIRED,
    download_paths,
    endpoint_path,
)
from .reader_keys import DatasetAccess, shown_value
from .store import Store
from .surface import FORBIDDEN_ARGUMENTS, NO_ACCOUNT
from .types import Dataset, DatasetCell, DatasetChangeKind, DatasetRow, FieldState

__all__ = [
    "DatasetCaller",
    "ReadRefusal",
    "dataset_summary",
    "diff_rows",
    "readable",
]

NOT_FOUND = "No dataset by that name for this key."
CANNOT_REVEAL = (
    "This key cannot reveal contact details; ask the dataset's owner. They are masked."
)
_VALUE_CAP = 200
ALL_ROWS = 10_000
GOOD: frozenset[FieldState] = frozenset({"fresh", "repaired"})


@dataclass(frozen=True)
class DatasetCaller:
    """What the edge resolved for one request: the path's dataset, the key's access."""

    dataset_id: str
    access: DatasetAccess | None


class ReadRefusal(Exception):
    """A refused read: ``code`` is stable, the message is our words only."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code}


def check_args(name: str, args: dict[str, Any]) -> None:
    given = set(args)
    forbidden = sorted(given & FORBIDDEN_ARGUMENTS)
    if forbidden:
        raise ReadRefusal(
            "forbidden_argument",
            f"{name} does not take {', '.join(forbidden)}: the dataset comes from the "
            "endpoint and the reader from the key.",
        )
    unknown = sorted(given - TOOL_PROPS[name])
    if unknown:
        accepted = ", ".join(sorted(TOOL_PROPS[name])) or "no arguments"
        raise ReadRefusal(
            "bad_arguments",
            f"{name} does not take {', '.join(unknown)}. It takes: {accepted}.",
        )
    missing = sorted(TOOL_REQUIRED[name] - given)
    if missing:
        raise ReadRefusal("bad_arguments", f"{name} needs {', '.join(missing)}.")


def int_arg(
    args: dict[str, Any], key: str, default: int | None, low: int, high: int
) -> int | None:
    value = args.get(key, default)
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ReadRefusal("bad_arguments", f"{key} must be an integer {low}..{high}.")
    return value


def reveal_asked(args: dict[str, Any]) -> bool:
    flag = args.get("reveal_pii", False)
    if not isinstance(flag, bool):
        raise ReadRefusal("bad_arguments", "reveal_pii must be true or false.")
    return flag


def columns_arg(dataset: Dataset, args: dict[str, Any]) -> list[str]:
    names = [f.name for f in dataset.schema]
    raw = args.get("columns")
    if raw is None:
        return names
    if (
        not isinstance(raw, list)
        or not all(isinstance(c, str) for c in raw)
        or len(raw) > MAX_COLUMNS
    ):
        raise ReadRefusal(
            "bad_arguments", f"columns is a list of at most {MAX_COLUMNS} field names."
        )
    unknown = [c for c in raw if c not in names]
    if unknown:
        raise ReadRefusal(
            "unknown_column",
            f"Unknown column {', '.join(sorted(set(unknown)))[:200]}. "
            f"The fields are: {', '.join(names)}.",
        )
    wanted = set(raw)
    return [n for n in names if n in wanted]


#: Subjects are never a contact FIELD, so only a value that IS an email or an
#: international phone number is masked; a person's name passes unchanged.
_SUBJECT_FIELD = "subject"


def shown_subject(
    access: DatasetAccess, subject: str, *, reveal: bool
) -> tuple[str, bool]:
    """``(subject to show, whether it was masked)``: the value rule, for a subject."""
    shown, hid = shown_value(access, _SUBJECT_FIELD, subject, reveal=reveal)
    return subject if shown is None else shown, hid


def subject_arg(
    dataset: Dataset, args: dict[str, Any], access: DatasetAccess
) -> str | None:
    """The stored subject a ``subject`` filter names. The owner's key may name it whole;
    any caller may name it as :func:`shown_subject` shows it masked."""
    subject = args.get("subject")
    if subject is None:
        return None
    unknown = ReadRefusal(
        "unknown_subject", "No such subject; call schema for the subject list."
    )
    if not isinstance(subject, str):
        raise unknown
    if access.may_reveal_pii and subject in dataset.subjects:
        return subject
    named = [
        s
        for s in dataset.subjects
        if shown_subject(access, s, reveal=False)[0] == subject
    ]
    if not named:
        raise unknown
    if len(named) > 1:
        raise ReadRefusal(
            "ambiguous_subject",
            "More than one subject masks to that; the owner's key can name it whole.",
        )
    return named[0]


def encode_cursor(payload: list[Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: Any) -> list[Any]:
    if not isinstance(cursor, str) or len(cursor) > 200:
        raise ReadRefusal("bad_arguments", "cursor is the next_cursor of a page.")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError):
        raise ReadRefusal(
            "bad_arguments", "cursor is the next_cursor of a page."
        ) from None
    if not isinstance(value, list):
        raise ReadRefusal("bad_arguments", "cursor is the next_cursor of a page.")
    return value


def subject_tag(subject: str) -> str:
    # A hash, not the subject: the cursor stays short and never carries a name.
    return hashlib.sha256(subject.encode()).hexdigest()[:12]


def json_size(value: Any) -> int:
    """Characters on the wire: the transport sends ``json.dumps(result, default=str)``."""
    return len(json.dumps(value, default=str))


def api_slug(source: str) -> str:
    """``orthogonal:fiber:POST:/people`` -> ``fiber``; the full endpoint is in sources."""
    parts = source.split(":")
    return parts[1] if len(parts) >= 2 else source


class Shower:
    """One masking decision per answer: the ask AND the key, and whether it hid any."""

    def __init__(self, access: DatasetAccess, asked: bool) -> None:
        self.access = access
        self.asked = asked
        self.hid = False

    def __call__(self, field_name: str, value: str | None) -> str | None:
        shown, hid = shown_value(self.access, field_name, value, reveal=self.asked)
        self.hid = self.hid or hid
        return None if shown is None else untrusted_summary(shown, cap=_VALUE_CAP)

    def subject(self, subject: str) -> str:
        shown, hid = shown_subject(self.access, subject, reveal=self.asked)
        self.hid = self.hid or hid
        return shown

    def extra(self) -> dict[str, Any]:
        if self.asked and not self.access.may_reveal_pii:
            return {"notice": CANNOT_REVEAL}
        return {}


def cell_view(cell: DatasetCell, show: Shower) -> dict[str, Any]:
    out: dict[str, Any] = {
        "value_untrusted": show(cell.field, cell.value),
        "status": cell.status,
        "source": api_slug(cell.source) if cell.source else None,
        "fetched_at": cell.fetched_at,
    }
    if cell.status == "stale":
        out["stale_since"] = cell.stale_since
    if cell.status in ("stale", "no data") and cell.note:
        out["note"] = cell.note
    return {k: v for k, v in out.items() if v is not None and v != ""}


def dataset_summary(dataset: Dataset) -> dict[str, Any]:
    """One dataset as its owner's lists show it. No run_id, plan_id or snapshot URI."""
    latest = dataset.versions[-1] if dataset.versions else None
    return {
        "dataset_id": dataset.dataset_id,
        "endpoint": endpoint_path(dataset.dataset_id),
        "downloads": download_paths(dataset.dataset_id),
        "query": dataset.query,
        "fields": [f.name for f in dataset.schema],
        "subjects": list(dataset.subjects),
        "version": latest.n if latest else None,
        "materialized_at": latest.materialized_at if latest else None,
        "snapshot_status": latest.snapshot_status if latest else None,
    }


def latest_version(dataset: Dataset) -> int:
    if not dataset.versions:
        raise ReadRefusal(
            "no_version_yet",
            "This dataset has no version yet; the owner fills it with refresh_dataset.",
        )
    return dataset.versions[-1].n


def readable(
    store: Store, caller: DatasetCaller | None
) -> tuple[Dataset, DatasetAccess]:
    """The caller's dataset, or the one not-found answer for missing AND out of scope."""
    access = None if caller is None else caller.access
    if caller is None or access is None:
        raise ReadRefusal(NO_ACCOUNT, DATASET_HINT)
    dataset_id = caller.dataset_id
    if not BUNDLE_NAME_RE.match(dataset_id or "") or not access.may_read(
        access.customer, dataset_id
    ):
        raise ReadRefusal("dataset_not_found", NOT_FOUND)
    dataset = store.dataset(access.customer, dataset_id)
    if dataset is None:
        raise ReadRefusal("dataset_not_found", NOT_FOUND)
    return dataset, access


def ordered_cells(row: DatasetRow, columns: Sequence[str]) -> list[DatasetCell]:
    by_name = {c.field: c for c in row.cells}
    return [by_name[c] for c in columns if c in by_name]


def change_kind(before: DatasetCell, after: DatasetCell) -> DatasetChangeKind | None:
    if before.value is None and after.value is not None:
        return "added"
    if before.value is not None and after.value is None:
        return "removed"
    if before.status != "stale" and after.status == "stale":
        return "went_stale"
    if before.status == "stale" and after.status in GOOD:
        return "recovered"
    if before.value != after.value or before.source != after.source:
        return "changed"
    return None


def diff_rows(
    before: Sequence[DatasetRow],
    after: Sequence[DatasetRow],
    *,
    columns: Sequence[str] | None = None,
    subject: str | None = None,
) -> list[tuple[str, DatasetChangeKind, DatasetCell, DatasetCell]]:
    """Per (subject, field), oldest table to newest: what kind of change, if any.
    Sorted by subject, then the newer table's field order."""
    empty = DatasetCell("", None, "", None, None, "no data", None, False, "", None)
    held = {(r.subject, c.field): c for r in before for c in r.cells}
    wanted = None if columns is None else set(columns)
    out: list[tuple[str, DatasetChangeKind, DatasetCell, DatasetCell]] = []
    for row in sorted(after, key=lambda r: r.subject):
        if subject is not None and row.subject != subject:
            continue
        for cell in row.cells:
            if wanted is not None and cell.field not in wanted:
                continue
            old = held.get((row.subject, cell.field), empty)
            kind = change_kind(old, cell)
            if kind is not None:
                out.append((row.subject, kind, old, cell))
    return out
