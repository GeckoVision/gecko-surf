"""Read the few facts each supplier field needs out of an upstream reply.

CONFIRMED vs DEFENSIVE. ``/v1/details`` lists response fields for predictleads only
(``data[].attributes.*``, ``meta.parent_company_domain``, ``included[].attributes.
company_name``). For crustdata identify, akta-pro and financial-datasets it lists NONE,
so those readers search a bounded depth for the usual key names and return ``None``
when nothing fits. ``None`` reaches the table as "no data" — never as "no parent" or
"no news", which would be a claim the reply did not make.

Nothing here keeps a value beyond the run; the ledger keeps field NAMES only
(``field_names``).
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..paginate import PageRead
from ..spec import DOMAIN_RE

_MAX_DEPTH = 6
_MAX_NAMES = 80


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def data_of(payload: Any) -> Any:
    """The upstream reply inside Orthogonal's envelope."""
    return _dict(payload).get("data")


def find_first(node: Any, keys: Iterable[str], depth: int = _MAX_DEPTH) -> Any:
    """Breadth-first: the first non-empty value under any of ``keys``, or ``None``."""
    wanted = tuple(keys)
    queue: deque[tuple[Any, int]] = deque([(node, 0)])
    while queue:
        current, level = queue.popleft()
        if isinstance(current, dict):
            for key in wanted:
                value = current.get(key)
                if value not in (None, "", [], {}):
                    return value
            children: Iterable[Any] = current.values()
        elif isinstance(current, list):
            children = current[:50]
        else:
            continue
        if level < depth:
            queue.extend((child, level + 1) for child in children)
    return None


def first_list(node: Any, keys: Iterable[str] = ()) -> list[Any] | None:
    """A list under one of ``keys``, else the first list of objects found, else None."""
    if isinstance(node, list):
        return node
    named = find_first(node, keys) if keys else None
    if isinstance(named, list):
        return named
    queue: deque[tuple[Any, int]] = deque([(node, 0)])
    while queue:
        current, level = queue.popleft()
        if isinstance(current, dict) and level < _MAX_DEPTH:
            for value in current.values():
                if isinstance(value, list) and any(isinstance(v, dict) for v in value):
                    return value
                queue.append((value, level + 1))
    return None


def field_names(node: Any, prefix: str = "", depth: int = 4) -> list[str]:
    """Dotted field NAMES present (``data[].attributes.title``), capped. No values."""
    names: list[str] = []

    def walk(value: Any, path: str, level: int) -> None:
        if len(names) >= _MAX_NAMES:
            return
        if isinstance(value, dict):
            for key in list(value)[:_MAX_NAMES]:
                child = f"{path}.{key}" if path else str(key)
                names.append(child)
                if level < depth:
                    walk(value[key], child, level + 1)
        elif isinstance(value, list):
            child = f"{path}[]"
            first = next((v for v in value if isinstance(v, dict)), None)
            if first is not None and level < depth:
                walk(first, child, level + 1)

    walk(node, prefix, 0)
    return names[:_MAX_NAMES]


def row_count(payload: Any) -> int:
    rows = first_list(data_of(payload), ("data", "news", "results", "matches"))
    return len(rows) if rows is not None else 0


# --- page readers handed to ``paginate.fetch_pages`` -------------------------------


def single_page(payload: Any) -> PageRead:
    """A call that never pages: whatever came back is one page with no cursor."""
    return PageRead(items=(), cursor=None)


class PageNumberReader:
    """predictleads pages by number: a FULL page means there may be another."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.page = 0

    def __call__(self, payload: Any) -> PageRead:
        self.page += 1
        data = data_of(payload)
        inner = _dict(data).get("data")
        if not isinstance(inner, list):
            return PageRead(items=None, data_fields=tuple(sorted(_dict(data))))
        cursor = str(self.page + 1) if len(inner) >= self.limit else None
        return PageRead(items=tuple(inner), cursor=cursor)


# --- facts -------------------------------------------------------------------------


@dataclass(frozen=True)
class NamedEntity:
    name: str | None
    domain: str | None

    @property
    def found(self) -> bool:
        return bool(self.name or self.domain)


def _domain(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    host = text.lower().split("://")[-1].split("/")[0].removeprefix("www.")
    return host if DOMAIN_RE.match(host) else None


def entity_from_identify(payload: Any) -> NamedEntity:
    data = data_of(payload)
    basic = find_first(data, ("basic_info",))
    node = basic if isinstance(basic, dict) else data
    name = _text(find_first(node, ("name", "company_name")))
    domain = _domain(find_first(node, ("primary_domain", "domain", "website")))
    return NamedEntity(name, domain)


def predictleads_company_name(payloads: list[Any], domain: str) -> str | None:
    """``data[0].attributes.company_name`` or the matching ``included`` company."""
    for payload in payloads:
        data = _dict(data_of(payload))
        rows = data.get("data")
        if isinstance(rows, list):
            for row in rows:
                attrs = _dict(_dict(row).get("attributes"))
                if _dict(row).get("type") == "company":
                    name = _text(attrs.get("company_name")) or _text(
                        attrs.get("friendly_company_name")
                    )
                    if name:
                        return name
        for item in data.get("included") or []:
            attrs = _dict(_dict(item).get("attributes"))
            if _dict(item).get("type") in (None, "company") and (
                _domain(attrs.get("domain")) in (None, domain)
            ):
                name = _text(attrs.get("company_name"))
                if name:
                    return name
    return None


def predictleads_parent_domain(payloads: list[Any]) -> str | None:
    for payload in payloads:
        meta = _dict(_dict(data_of(payload)).get("meta"))
        found = _domain(meta.get("parent_company_domain"))
        if found:
            return found
    return None


@dataclass(frozen=True)
class Hierarchy:
    parent: NamedEntity
    subsidiaries: int | None


_PARENT_KEYS = (
    "parent_company",
    "parentCompany",
    "parent",
    "ultimate_parent",
    "ultimate_parent_company",
)


def hierarchy_from_akta(payload: Any) -> Hierarchy:
    data = data_of(payload)
    raw = find_first(data, _PARENT_KEYS)
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if isinstance(raw, dict):
        parent = NamedEntity(
            _text(find_first(raw, ("name", "company_name", "companyName"), 2)),
            _domain(find_first(raw, ("domain", "website", "primary_domain"), 2)),
        )
    elif isinstance(raw, str):
        as_domain = _domain(raw)
        parent = NamedEntity(None if as_domain else _text(raw), as_domain)
    else:
        parent = NamedEntity(None, None)
    subs = find_first(data, ("subsidiaries", "children"))
    return Hierarchy(parent, len(subs) if isinstance(subs, list) else None)


@dataclass(frozen=True)
class Dated:
    """A dated item (news event, article, page change): its date and one line of text."""

    when: dt.date | None
    text: str | None


def _date(value: Any) -> dt.date | None:
    text = _text(value)
    if text is None or len(text) < 10:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def dated_items(
    items: Iterable[Any], date_keys: tuple[str, ...], text_keys: tuple[str, ...]
) -> list[Dated]:
    out: list[Dated] = []
    for item in items:
        node = _dict(item)
        attrs = _dict(node.get("attributes")) or node
        when = next((d for d in (_date(attrs.get(k)) for k in date_keys) if d), None)
        text = next((t for t in (_text(attrs.get(k)) for k in text_keys) if t), None)
        out.append(Dated(when, text))
    return out


NEWS_DATE_KEYS = ("found_at", "published_at", "publishedAt", "date", "datetime")
NEWS_TEXT_KEYS = ("title", "headline", "summary", "article_sentence")
CHANGE_DATE_KEYS = ("first_seen_at", "last_seen_at")
CHANGE_TEXT_KEYS = ("category", "url")


def financial_news(payload: Any) -> list[Dated] | None:
    rows = first_list(data_of(payload), ("news", "articles", "results"))
    if rows is None:
        return None
    return dated_items(rows, NEWS_DATE_KEYS, NEWS_TEXT_KEYS)


@dataclass(frozen=True)
class TrustSignals:
    count: int
    groups: tuple[str, ...]
    first_text: str | None


def trust_from_akta(payload: Any) -> TrustSignals | None:
    data = data_of(payload)
    node = _dict(data)
    if isinstance(node.get("data"), dict):
        node = node["data"]
    if not node and isinstance(data, list):
        node = {"signals": data}
    if not node:
        return None
    groups = tuple(k for k, v in node.items() if isinstance(v, list) and v)
    count = sum(len(node[k]) for k in groups)
    first = find_first(
        node, ("title", "name", "signal", "description", "text", "summary")
    )
    return TrustSignals(count, groups, _text(first))
