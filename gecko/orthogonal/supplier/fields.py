"""One row per (supplier, field): the value, where it came from, and whether two sources
agree. Agreement is computed HERE, in code, from typed facts — never asked of a model.

``agree`` / ``differ`` — both sources gave a value, and the rule says same / not same.
``only one``           — one source gave a value (or the field has one source by design).
``no data``            — no source gave a value (call failed, skipped, shape unreadable).

A row's ``state`` says how this run's value came about: ``fresh`` (its first source),
``repaired`` (the first source drifted or failed and a ranked alternative passed the
checks in ``repair.py``), ``stale`` (none passed, so the last good value is kept and
marked) or ``no data``.

Each row carries two summaries. ``value_summary`` may quote upstream text (sanitized,
capped) and goes to the files the person reads. ``model_summary`` is built from counts
and our own words, and is the only one a tool result carries.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypeVar

from .readers import Dated, Hierarchy, NamedEntity, TrustSignals
from .sanctions import SdnHit, SdnList
from .sheet import Supplier
from .text import domain_root, normalize_company, untrusted_summary

Agreement = Literal["agree", "differ", "only one", "no data"]
AGREEMENTS: tuple[Agreement, ...] = ("agree", "differ", "only one", "no data")
FieldState = Literal["fresh", "repaired", "stale", "no data"]
FIELD_STATES: tuple[FieldState, ...] = ("fresh", "repaired", "stale", "no data")
#: The parent field's canonical value when a source ANSWERED and named no parent.
NO_PARENT = "(no parent named)"

FIELD_ENTITY = "entity"
FIELD_PARENT = "parent_company"
FIELD_NEWS = "news_this_month"
FIELD_TRUST = "trust_signals"
FIELD_SANCTIONS = "sanctions"
FIELD_WEBSITE = "website_changed"
FIELDS = (
    FIELD_ENTITY,
    FIELD_PARENT,
    FIELD_NEWS,
    FIELD_TRUST,
    FIELD_SANCTIONS,
    FIELD_WEBSITE,
)

SRC_IDENTIFY = "crustdata /company/identify"
SRC_PL_COMPANY = "predictleads company name"
SRC_HIERARCHY = "akta-pro company_hierarchy"
SRC_PL_PARENT = "predictleads meta.parent_company_domain"
SRC_PL_NEWS = "predictleads news_events"
SRC_FD_NEWS = "financial-datasets /news"
SRC_TRUST = "akta-pro trust_signal"
SRC_WEBSITE = "predictleads website_evolution"
NO_SECOND = "none"

T = TypeVar("T")


def agreement(
    first: T | None, second: T | None, same: Callable[[T, T], bool]
) -> Agreement:
    if first is None and second is None:
        return "no data"
    if first is None or second is None:
        return "only one"
    return "agree" if same(first, second) else "differ"


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def single(value: object | None) -> Agreement:
    return "no data" if value is None else "only one"


@dataclass(frozen=True)
class FieldRow:
    supplier: str
    field: str
    value_summary: str
    source: str
    second_source: str
    agreement: Agreement
    model_summary: str
    #: The canonical value compared across runs (sanitized, capped); ``None`` = none.
    value: str | None = None
    filled_by: str = ""
    failover: bool = False
    #: Our words: why the first source was passed over, and what the check found.
    note: str = ""
    status: FieldState | None = None
    stale_since: str | None = None

    @property
    def state(self) -> FieldState:
        if self.status is not None:
            return self.status
        return "no data" if self.agreement == "no data" else "fresh"


@dataclass
class SupplierFacts:
    """Everything the calls produced for one supplier. ``None`` = no usable reply."""

    supplier: Supplier
    identify: NamedEntity | None = None
    pl_name: str | None = None
    pl_parent_domain: str | None = None
    hierarchy: Hierarchy | None = None
    pl_news: list[Dated] | None = None
    fd_news: list[Dated] | None = None
    trust: TrustSignals | None = None
    website: list[Dated] | None = None
    sanctions_names: list[str] = field(default_factory=list)


def _same_name(a: str, b: str) -> bool:
    return bool(normalize_company(a)) and normalize_company(a) == normalize_company(b)


def entity_row(f: SupplierFacts) -> FieldRow:
    first = f.identify.name if f.identify and f.identify.name else None
    second = f.pl_name
    agree = agreement(first, second, _same_name)
    shown = untrusted_summary(first or second or "")
    if agree == "differ":
        shown = untrusted_summary(f"{first} vs {second}")
    model = {
        "agree": "both sources name the same company",
        "differ": "the two sources give different names",
        "only one": "one source named it",
        "no data": "no source named it",
    }[agree]
    return FieldRow(
        f.supplier.name,
        FIELD_ENTITY,
        shown or "no name found",
        SRC_IDENTIFY,
        SRC_PL_COMPANY,
        agree,
        model,
        value=untrusted_summary(first or second or "") or None,
        filled_by=SRC_IDENTIFY if first else (SRC_PL_COMPANY if second else ""),
    )


def _parent_key(parent: NamedEntity) -> str | None:
    if parent.domain:
        return parent.domain
    return parent.name


def _same_parent(first: str, second_domain: str) -> bool:
    if "." in first and first == second_domain:
        return True
    root = domain_root(second_domain)
    name = normalize_company(first)
    return bool(root) and root in (name, name.replace(" ", ""), domain_root(first))


def parent_row(f: SupplierFacts) -> FieldRow:
    first = _parent_key(f.hierarchy.parent) if f.hierarchy else None
    second = f.pl_parent_domain
    agree = agreement(first, second, _same_parent)
    if first and agree == "differ":
        shown = untrusted_summary(f"{first} vs {second}")
    else:
        shown = untrusted_summary(first or second or "")
    if f.hierarchy and f.hierarchy.subsidiaries is not None:
        subs = _n(f.hierarchy.subsidiaries, "subsidiary", "subsidiaries")
        shown = f"{shown or 'no parent named'}; {subs}"
    model = "parent named" if (first or second) else "no parent named by either source"
    value: str | None = None
    if f.hierarchy is not None:
        value, filled = untrusted_summary(first or "") or NO_PARENT, SRC_HIERARCHY
    else:
        value, filled = (untrusted_summary(second or "") or None), SRC_PL_PARENT
    return FieldRow(
        f.supplier.name,
        FIELD_PARENT,
        shown or "no parent named",
        SRC_HIERARCHY,
        SRC_PL_PARENT,
        agree,
        f"{model} ({agree})",
        value=value,
        filled_by=filled if value else "",
    )


def _in_month(items: list[Dated] | None, since: dt.date) -> int | None:
    if items is None:
        return None
    if items and all(item.when is None for item in items):
        return None  # dates unreadable: no claim either way
    return sum(1 for item in items if item.when is not None and item.when >= since)


def news_row(f: SupplierFacts, since: dt.date) -> FieldRow:
    first = _in_month(f.pl_news, since)
    second = _in_month(f.fd_news, since)
    agree = agreement(first, second, lambda a, b: (a > 0) == (b > 0))
    counts = []
    if first is not None:
        counts.append(f"{first} predictleads")
    if second is not None:
        counts.append(f"{second} financial-datasets")
    titles = [
        item.text
        for item in (f.pl_news or []) + (f.fd_news or [])
        if item.text and item.when is not None and item.when >= since
    ]
    shown = f"since {since.isoformat()}: " + (", ".join(counts) or "no data")
    if titles:
        shown += " | " + untrusted_summary(titles[0], 100)
    second_source = SRC_FD_NEWS if f.supplier.ticker else "none (no ticker)"
    return FieldRow(
        f.supplier.name,
        FIELD_NEWS,
        untrusted_summary(shown),
        SRC_PL_NEWS,
        second_source,
        agree,
        "news counts: " + (", ".join(counts) or "no data"),
    )


def trust_row(f: SupplierFacts) -> FieldRow:
    t = f.trust
    if t is None:
        shown, model = "no data", "no data"
    else:
        groups = untrusted_summary(", ".join(t.groups), 60)
        shown = _n(t.count, "signal") + (f" ({groups})" if groups else "")
        if t.first_text:
            shown += " | " + untrusted_summary(t.first_text, 80)
        model = _n(t.count, "signal")
    return FieldRow(
        f.supplier.name,
        FIELD_TRUST,
        untrusted_summary(shown),
        SRC_TRUST,
        NO_SECOND,
        single(t),
        model,
    )


def sanctions_row(
    f: SupplierFacts, sdn: SdnList | None, hit: SdnHit | None
) -> FieldRow:
    if sdn is None:
        return FieldRow(
            f.supplier.name,
            FIELD_SANCTIONS,
            "not checked: the SDN list was not available",
            "OFAC SDN list",
            NO_SECOND,
            "no data",
            "not checked",
        )
    if hit is None:
        shown, model = f"no name match on the {sdn.label}", "no name match"
    else:
        shown = untrusted_summary(
            f"possible match ({hit.kind} name) with SDN entry '{hit.listed_name}'. "
            "A name match is not a finding: check it by hand."
        )
        model = f"possible match ({hit.kind} name), check by hand"
    return FieldRow(
        f.supplier.name, FIELD_SANCTIONS, shown, sdn.label, NO_SECOND, "only one", model
    )


def website_row(f: SupplierFacts, since: dt.date) -> FieldRow:
    changed = _in_month(f.website, since) if f.website is not None else None
    if f.website is not None and changed is None:
        changed = len(f.website)  # undated changes still count as changes seen
    if changed is None:
        shown, model = "no data", "no data"
    elif changed == 0:
        shown = model = f"no change seen since {since.isoformat()}"
    else:
        kinds = sorted({i.text for i in f.website or [] if i.text})
        shown = f"yes: {_n(changed, 'page change')} since {since.isoformat()}"
        if kinds:
            shown += " (" + untrusted_summary(", ".join(kinds), 80) + ")"
        model = f"{_n(changed, 'page change')} since {since.isoformat()}"
    return FieldRow(
        f.supplier.name,
        FIELD_WEBSITE,
        untrusted_summary(shown),
        SRC_WEBSITE,
        NO_SECOND,
        single(changed),
        model,
    )


def build_rows(
    f: SupplierFacts,
    *,
    month_start: dt.date,
    website_since: dt.date,
    sdn: SdnList | None,
    hit: SdnHit | None,
) -> list[FieldRow]:
    return [
        entity_row(f),
        parent_row(f),
        news_row(f, month_start),
        trust_row(f),
        sanctions_row(f, sdn, hit),
        website_row(f, website_since),
    ]
