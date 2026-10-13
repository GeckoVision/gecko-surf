"""Deterministic commenter -> customer / new / not sure. Code decides, never a model.

The rules, in order:
1. No handle on the comment, or the sheet has no handle column -> ``not_sure``.
2. Handle is in the sheet -> ``customer``.
3. Handle is one edit away from a sheet handle, or equal once ``.``/``_`` are removed,
   or the commenter's display name equals the name on a row that has no handle
   -> ``not_sure``.
4. Otherwise -> ``new``.

It matches HANDLES. It does not know who a person is, and its accuracy on a real shop
has never been measured.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from .extract import Commenter
from .sheet import CustomerSheet, normalize_handle, normalize_name

MatchClass = Literal["customer", "new", "not_sure"]
MATCH_CLASSES: tuple[MatchClass, ...] = ("customer", "new", "not_sure")

#: Shown in place of a handle the reply did not carry.
NO_HANDLE = "(no handle)"
_NEAR_MIN_LEN = 5


@dataclass(frozen=True)
class MatchRow:
    handle: str
    match: MatchClass


def _one_edit_apart(a: str, b: str) -> bool:
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    for index in range(len(long_)):
        if long_[:index] + long_[index + 1 :] == short:
            return True
    return False


def _squash(handle: str) -> str:
    return handle.replace(".", "").replace("_", "")


def classify(commenter: Commenter, sheet: CustomerSheet) -> tuple[str, MatchClass]:
    handle = normalize_handle(commenter.handle) if commenter.handle else None
    if handle is None:
        return NO_HANDLE, "not_sure"
    if not sheet.has_handle_column:
        return handle, "not_sure"
    if handle in sheet.handles:
        return handle, "customer"
    squashed = _squash(handle)
    for known in sheet.handles:
        if _squash(known) == squashed:
            return handle, "not_sure"
        if min(len(known), len(handle)) >= _NEAR_MIN_LEN and _one_edit_apart(
            known, handle
        ):
            return handle, "not_sure"
    if commenter.full_name and (
        normalize_name(commenter.full_name) in sheet.names_without_handle
    ):
        return handle, "not_sure"
    return handle, "new"


_ORDER = {"customer": 0, "not_sure": 1, "new": 2}


def match_all(commenters: Iterable[Commenter], sheet: CustomerSheet) -> list[MatchRow]:
    """One row per distinct handle (a person who commented twice counts once).

    Comments with no handle cannot be deduplicated, so each is its own ``not_sure`` row.
    Sorted customers first, then not sure, then new, each alphabetically.
    """
    best: dict[str, MatchClass] = {}
    unnamed = 0
    for commenter in commenters:
        handle, cls = classify(commenter, sheet)
        if handle == NO_HANDLE:
            unnamed += 1
            continue
        current = best.get(handle)
        if current is None or _ORDER[cls] < _ORDER[current]:
            best[handle] = cls
    rows = [MatchRow(h, c) for h, c in best.items()]
    rows.sort(key=lambda r: (_ORDER[r.match], r.handle))
    rows.extend(MatchRow(NO_HANDLE, "not_sure") for _ in range(unnamed))
    return rows


def counts(rows: Iterable[MatchRow]) -> dict[str, int]:
    tally: dict[str, int] = {cls: 0 for cls in MATCH_CLASSES}
    for row in rows:
        tally[row.match] += 1
    return tally
