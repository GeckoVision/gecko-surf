"""The planned calls for one supplier, and the facts their replies carry.

Every reply is kept by call key (with its ledger rows) for ``repair.py``, which
judges the first source of each mapped field and may read or add more.
"""

from __future__ import annotations

from typing import Any

from ..errors import UpstreamError
from ..paginate import FetchResult
from . import calls as C
from .calls import company_call, planned_calls
from .fields import SupplierFacts
from .plan import windows
from .readers import (
    CHANGE_DATE_KEYS,
    CHANGE_TEXT_KEYS,
    NEWS_DATE_KEYS,
    NEWS_TEXT_KEYS,
    dated_items,
    entity_from_identify,
    financial_news,
    hierarchy_from_akta,
    predictleads_company_name,
    predictleads_parent_domain,
    trust_from_akta,
)
from .repair import Reply
from .deps import SupplierDeps
from .runner import Runner


def _items_or_none(result: FetchResult | None) -> list[Any] | None:
    """The items, or ``None`` when no reply came back or its list was unreadable —
    so the field says "no data" rather than "nothing happened"."""
    if result is None or not result.calls:
        return None
    if isinstance(result.abort, UpstreamError) and not result.items:
        return None
    return result.items


def _call(run: Runner, spec: C.CallSpec, replies: dict[str, Reply]) -> Reply:
    start = len(run.ledger_rows)
    reply = Reply(run.call(spec), run.ledger_rows[start:])
    replies[spec.key] = reply
    return reply


def watch_one(
    run: Runner[SupplierDeps], facts: SupplierFacts, skip: frozenset[str]
) -> tuple[dict[str, Reply], list[Any]]:
    """The planned calls for one supplier. Returns every reply by call key and the
    predictleads replies (which several fields read)."""
    s = facts.supplier
    month_start, since = windows(run.deps.today(), run.deps.website_lookback_days)
    replies: dict[str, Reply] = {}
    pl_payloads: list[Any] = []
    for spec in planned_calls(s, month_start=month_start, website_since=since):
        if spec.key in skip:
            continue
        result = _call(run, spec, replies).result
        first = result.payloads[0] if result and result.payloads else None
        if spec.key == C.KEY_IDENTIFY and first is not None:
            facts.identify = entity_from_identify(first)
        elif spec.key in (C.KEY_PL_NEWS, C.KEY_PL_WEBSITE):
            pl_payloads.extend(result.payloads if result else [])
            items = _items_or_none(result)
            if items is not None and spec.key == C.KEY_PL_NEWS:
                facts.pl_news = dated_items(items, NEWS_DATE_KEYS, NEWS_TEXT_KEYS)
            elif items is not None:
                facts.website = dated_items(items, CHANGE_DATE_KEYS, CHANGE_TEXT_KEYS)
            if spec.key == C.KEY_PL_WEBSITE and pl_payloads:
                if predictleads_company_name(pl_payloads, s.domain) is None:
                    pl_payloads.extend(_call(run, company_call(s), replies).payloads)
        elif spec.key == C.KEY_HIERARCHY and first is not None:
            facts.hierarchy = hierarchy_from_akta(first)
        elif spec.key == C.KEY_TRUST and first is not None:
            facts.trust = trust_from_akta(first)
        elif spec.key == C.KEY_FD_NEWS and first is not None:
            facts.fd_news = financial_news(first)
    facts.pl_name = predictleads_company_name(pl_payloads, s.domain)
    facts.pl_parent_domain = predictleads_parent_domain(pl_payloads)
    return replies, pl_payloads
