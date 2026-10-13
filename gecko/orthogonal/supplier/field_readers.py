"""One reader per (source, field): the replies in, one canonical ``Reading`` out.

A reader returns ``None`` when it cannot read the field from the replies it was given
(no reply, unknown shape, nothing named). That is a FAILED source, and the field moves
to the next one in ``sources.SOURCE_MAP``. A ``Reading`` whose ``value`` is ``None``
means the source ANSWERED and named nothing — only fields that allow it (the parent:
"no parent") treat that as a value.

Values are passed through ``untrusted_summary`` here, once, so every comparison and
every stored ``value`` is the sanitized, capped text.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .fields import NO_PARENT
from .readers import (
    _dict,
    _domain,
    _text,
    data_of,
    entity_from_identify,
    hierarchy_from_akta,
    predictleads_company_name,
)
from .sheet import Supplier
from .text import untrusted_summary


@dataclass(frozen=True)
class Reading:
    #: The canonical value (sanitized, capped). ``None`` = answered, named nothing.
    value: str | None
    #: The domain the source says the value belongs to, when it says one: the
    #: same-entity check compares it with the supplier's domain.
    domain: str | None = None


#: (the replies this source produced for one supplier, the supplier) -> a reading.
Reader = Callable[[list[Any], Supplier], "Reading | None"]


def _clean(text: str | None) -> str | None:
    return untrusted_summary(text or "") or None


def read_identify(payloads: list[Any], supplier: Supplier) -> Reading | None:
    for payload in payloads:
        entity = entity_from_identify(payload)
        if entity.name:
            return Reading(_clean(entity.name), entity.domain)
    return None


def read_predictleads_company(
    payloads: list[Any], supplier: Supplier
) -> Reading | None:
    """``data[].attributes.company_name`` (company endpoint) or the matching
    ``included`` company in any predictleads reply already in hand."""
    name = predictleads_company_name(payloads, supplier.domain)
    if name is None:
        return None
    domain = None
    for payload in payloads:
        for row in _dict(data_of(payload)).get("data") or []:
            attrs = _dict(_dict(row).get("attributes"))
            if _dict(row).get("type") == "company" and attrs.get("domain"):
                domain = _domain(attrs.get("domain"))
                break
    return Reading(_clean(name), domain)


def read_coresignal_company(payloads: list[Any], supplier: Supplier) -> Reading | None:
    """``name`` and ``websites_main``: the response fields ``/v1/details`` lists for
    ``/v2/company_clean/enrich``. Declared, not yet seen live."""
    for payload in payloads:
        node = _dict(data_of(payload))
        name = _text(node.get("name"))
        if name:
            return Reading(_clean(name), _domain(node.get("websites_main")))
    return None


def read_akta_parent(payloads: list[Any], supplier: Supplier) -> Reading | None:
    """akta answered with an object: its parent, or ``NO_PARENT`` when it names none."""
    for payload in payloads:
        data = data_of(payload)
        if not isinstance(data, dict) or not data:
            continue
        parent = hierarchy_from_akta(payload).parent
        key = parent.domain or parent.name
        return Reading(_clean(key) or NO_PARENT, parent.domain)
    return None
