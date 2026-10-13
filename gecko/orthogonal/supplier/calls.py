"""The calls one supplier costs, as data: which target, which inputs, how it pages.

Order matters for money: the free identify first, then the predictleads calls (whose
replies also carry the entity's second source and the parent domain, so a separate
``/v3/companies/{domain}`` call is only a fallback), then akta-pro, then the ticker news.

PATH PARAMETERS — an assumption, not a confirmed fact: Orthogonal's ``/v1/run`` docs
show ``{"api", "path", "query", "body"}`` and no path-parameter field, so the
placeholder is filled into ``path`` (``/v3/companies/jabil.com/news_events``). The
allowlist accepts exactly that shape, with a normalized domain in the slot.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from ..paginate import BodyFor, ReadPage
from ..spec import (
    AKTA_HIERARCHY,
    AKTA_TRUST,
    CORESIGNAL_CLEAN,
    CRUST_IDENTIFY,
    FD_NEWS,
    PL_COMPANY,
    PL_NEWS,
    PL_WEBSITE,
    Target,
)
from .readers import PageNumberReader, single_page
from .sheet import Supplier

#: Items per predictleads page; a full page means "maybe more", followed for free.
PAGE_LIMIT = 25
#: predictleads follow-up pages per call. Free by contract, capped anyway.
MAX_FOLLOW_PAGES = 3
FD_NEWS_LIMIT = 10

KEY_IDENTIFY = "identify"
KEY_PL_NEWS = "pl_news"
KEY_PL_WEBSITE = "pl_website"
KEY_PL_COMPANY = "pl_company"
KEY_HIERARCHY = "akta_hierarchy"
KEY_TRUST = "akta_trust"
KEY_FD_NEWS = "fd_news"
KEY_CORESIGNAL = "coresignal_company"


def subject_of(domain: str) -> str:
    """A stable key for one supplier in the field ledger, without the domain in clear:
    the drift check asks "did THIS supplier's call answer last run?"."""
    return hashlib.sha256(domain.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CallSpec:
    key: str
    target: Target
    path: str
    query: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    max_pages: int = 1
    #: ``subject_of(domain)``: which supplier the call was for, as the ledger keeps it.
    subject: str = ""

    @property
    def endpoint(self) -> str:
        return self.target.label

    def input_fields(self) -> list[str]:
        """NAMES of the inputs sent: path slots, query keys, body keys. No values."""
        slots = re.findall(r"\{([a-z_]+)\}", self.target.path)
        return [*slots, *sorted(self.query), *sorted(self.body or {})]

    def body_for(self) -> BodyFor:
        def build(cursor: str | None, parent: str | None) -> dict[str, Any]:
            run: dict[str, Any] = {"api": self.target.api, "path": self.path}
            # Orthogonal's /v1/run rejects non-string query values with HTTP 400
            # ("Expected string, received number"), measured live 2026-10-08.
            query = {name: str(value) for name, value in self.query.items()}
            if cursor is not None:
                query["page"] = str(int(cursor))
            if query:
                run["query"] = query
            if self.body is not None:
                run["body"] = self.body
            if parent is not None:
                run["parentRequestId"] = parent
            return run

        return build

    def reader(self) -> ReadPage:
        if self.target.paginates and self.max_pages > 1:
            return PageNumberReader(int(self.query.get("limit", PAGE_LIMIT)))
        return single_page


def planned_calls(
    supplier: Supplier, *, month_start: dt.date, website_since: dt.date
) -> list[CallSpec]:
    """The calls a supplier costs when every reply carries what it should."""
    domain = supplier.domain
    who = subject_of(domain)
    calls = [
        CallSpec(
            KEY_IDENTIFY,
            CRUST_IDENTIFY,
            CRUST_IDENTIFY.path,
            body={"domains": [domain], "fields": ["basic_info"]},
            subject=who,
        ),
        CallSpec(
            KEY_PL_NEWS,
            PL_NEWS,
            PL_NEWS.fill(company_id_or_domain=domain),
            query={"found_at_from": month_start.isoformat(), "limit": PAGE_LIMIT},
            max_pages=MAX_FOLLOW_PAGES,
            subject=who,
        ),
        CallSpec(
            KEY_PL_WEBSITE,
            PL_WEBSITE,
            PL_WEBSITE.fill(company_id_or_domain=domain),
            query={
                "first_seen_at_from": website_since.isoformat(),
                "limit": PAGE_LIMIT,
            },
            max_pages=MAX_FOLLOW_PAGES,
            subject=who,
        ),
        CallSpec(
            KEY_HIERARCHY,
            AKTA_HIERARCHY,
            AKTA_HIERARCHY.fill(company=domain),
            subject=who,
        ),
        CallSpec(KEY_TRUST, AKTA_TRUST, AKTA_TRUST.fill(company=domain), subject=who),
    ]
    if supplier.ticker:
        calls.append(
            CallSpec(
                KEY_FD_NEWS,
                FD_NEWS,
                FD_NEWS.path,
                query={"ticker": supplier.ticker, "limit": FD_NEWS_LIMIT},
                subject=who,
            )
        )
    return calls


def company_call(supplier: Supplier) -> CallSpec:
    """The fallback: only when neither predictleads reply named the company."""
    return CallSpec(
        KEY_PL_COMPANY,
        PL_COMPANY,
        PL_COMPANY.fill(id_or_domain=supplier.domain),
        subject=subject_of(supplier.domain),
    )


def coresignal_call(supplier: Supplier) -> CallSpec:
    """A failover source only: the entity field's third choice (``sources.py``)."""
    return CallSpec(
        KEY_CORESIGNAL,
        CORESIGNAL_CLEAN,
        CORESIGNAL_CLEAN.path,
        query={"website": supplier.domain},
        subject=subject_of(supplier.domain),
    )


#: The calls a failover may add, by key. Planned calls are made by ``planned_calls``.
FAILOVER_CALLS = {KEY_PL_COMPANY: company_call, KEY_CORESIGNAL: coresignal_call}
