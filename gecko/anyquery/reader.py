"""Read one planned field out of one upstream reply, by its declared path.

The path grammar is ``readers.field_names``: dots between keys, ``[]`` after a list
(``data[].attributes.title``), relative to the upstream reply inside Orthogonal's
envelope (``readers.data_of``). The same string is what the field ledger records as
an output field name, so the drift check and the reader agree on what "the path is
there" means.

A value is turned into text ONCE, here, through ``untrusted_summary``: every stored
value and every cross-run comparison sees the sanitized, capped string.
"""

from __future__ import annotations

import math
import re
from typing import Any

from gecko.orthogonal.supplier.readers import data_of
from gecko.orthogonal.supplier.text import untrusted_summary

from .types import Compare

__all__ = ["FACT_VALUE_CAP", "read_path", "same_value", "value_for"]

#: The longest stored value (FactRow.value).
FACT_VALUE_CAP = 200
#: Items followed per list, and leaves kept per read: a bounded walk on any reply.
_MAX_ITEMS = 50
_MAX_LEAVES = 20
#: Leaves joined into one text value.
_TEXT_LEAVES = 5
_SPACE = re.compile(r"\s+")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _segments(path: str) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for part in path.split("."):
        is_list = part.endswith("[]")
        out.append((part[:-2] if is_list else part, is_list))
    return out


def _scalar(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, (str, int)) and value != ""


def read_path(node: Any, path: str) -> list[Any]:
    """The scalar leaves at ``path`` under ``node``, in order, at most 20.

    A path that ends on a list of scalars yields its items; a path that ends on an
    object or reaches nothing yields ``[]``. Never raises on an unexpected shape.
    """
    if not isinstance(path, str) or not path:
        return []
    current: list[Any] = [node]
    for key, is_list in _segments(path):
        following: list[Any] = []
        for item in current:
            if key:
                if not isinstance(item, dict):
                    continue
                item = item.get(key)
            if is_list:
                if isinstance(item, list):
                    following.extend(item[:_MAX_ITEMS])
            elif item is not None:
                following.append(item)
        current = following
        if not current:
            return []
    leaves: list[Any] = []
    for item in current:
        candidates = item[:_MAX_ITEMS] if isinstance(item, list) else [item]
        leaves.extend(c for c in candidates if _scalar(c))
        if len(leaves) >= _MAX_LEAVES:
            break
    return leaves[:_MAX_LEAVES]


def _number(leaf: Any) -> str | None:
    if isinstance(leaf, bool):
        return None
    try:
        number = float(leaf)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return str(int(number)) if number.is_integer() else repr(number)


def value_for(payloads: list[Any], path: str, compare: Compare) -> str | None:
    """The field's value from the first reply that carries ``path``; ``None`` when no
    reply does. Shaped by ``compare`` so the same fact reads the same across runs."""
    for payload in payloads:
        leaves = read_path(data_of(payload), path)
        if not leaves:
            continue
        if compare == "presence":
            return "present"
        if compare == "number":
            found = next((n for n in map(_number, leaves) if n is not None), None)
            if found is not None:
                return found
            continue
        if compare == "date":
            text = next((str(leaf) for leaf in leaves if _DATE.match(str(leaf))), None)
            if text is not None:
                return untrusted_summary(text[:10], FACT_VALUE_CAP) or None
            continue
        joined = "; ".join(str(leaf) for leaf in leaves[:_TEXT_LEAVES])
        return untrusted_summary(joined, FACT_VALUE_CAP) or None
    return None


def same_value(compare: Compare, before: str, after: str) -> bool:
    """Is ``after`` the same fact as ``before``? Text ignores case and spacing; numbers
    compare as numbers; dates and presence compare exactly."""
    if compare == "number":
        a, b = _number(before), _number(after)
        return a is not None and a == b
    if compare == "text":
        fold = [_SPACE.sub(" ", v).strip().casefold() for v in (before, after)]
        return bool(fold[0]) and fold[0] == fold[1]
    return before == after
