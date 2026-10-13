"""What the any-query tools return: the contract dataclasses as small, safe dicts.

Our words and our numbers only, except the fields named ``*_untrusted``, which carry
upstream text that was sanitized and capped again here, whatever the store held. No view
carries the customer id, a wallet_id or a key: the caller already knows who they are,
and the payer address is the only wallet detail that leaves, because the user must fund
it.
"""

from __future__ import annotations

from typing import Any

from gecko.orthogonal.supplier.history import ChangeSet
from gecko.orthogonal.supplier.text import untrusted_summary

from .pii import masked
from .types import FactRow, LedgerEntry, PlannedField, QueryPlan, Quote, RunRecord

__all__ = [
    "VALUE_CAP",
    "changes_view",
    "fact_view",
    "ledger_view",
    "plan_view",
    "price_text",
    "quote_view",
    "run_view",
    "usdc",
]

#: The contract's cap on any upstream value a tool returns.
VALUE_CAP = 200


def usdc(micro: int) -> str:
    """Micro-USDC as a decimal string with 6 places: ``1_250_000`` -> ``"1.250000"``."""
    sign = "-" if micro < 0 else ""
    whole, frac = divmod(abs(micro), 1_000_000)
    return f"{sign}{whole}.{frac:06d}"


def _untrusted(value: str | None) -> str | None:
    return None if value is None else untrusted_summary(value, cap=VALUE_CAP)


def _field_view(planned: PlannedField) -> dict[str, Any]:
    return {
        "name": planned.name,
        "meaning": planned.meaning,
        "compare": planned.compare,
        "sources": [
            {
                "rank": s.rank,
                "catalog": s.catalog,
                "api": s.api,
                "path": s.path,
                "price_cents": s.price_cents,
                "price_from": s.price_from,
                "storage": s.storage,
            }
            for s in sorted(planned.sources, key=lambda s: s.rank)
        ],
    }


def plan_view(plan: QueryPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "query": plan.query,
        "subjects": list(plan.subjects),
        "fields": [_field_view(f) for f in plan.fields],
        "unplanned": [{"field": u.field, "why": u.why} for u in plan.unplanned],
        "planner": plan.planner,
        "created_at": plan.created_at,
    }


def price_text(quote: Quote) -> str:
    """The price in words, the fee always apart from the data so a user sees what
    Gecko charges and what the sources cost."""
    text = (
        f"at most {usdc(quote.upstream_max_usdc_micro)} USDC for the data + "
        f"{usdc(quote.gecko_fee_usdc_micro)} USDC Gecko fee"
    )
    if quote.discount_usdc_micro:
        text += f" - {usdc(quote.discount_usdc_micro)} USDC credit"
    return f"{text} = {usdc(quote.total_usdc_micro)} USDC"


def quote_view(quote: Quote) -> dict[str, Any]:
    return {
        "quote_id": quote.quote_id,
        "plan_id": quote.plan_id,
        "total_usdc": usdc(quote.total_usdc_micro),
        "total_usdc_micro": quote.total_usdc_micro,
        "upstream_max_usdc": usdc(quote.upstream_max_usdc_micro),
        "gecko_fee_usdc": usdc(quote.gecko_fee_usdc_micro),
        "discount_usdc": usdc(quote.discount_usdc_micro),
        "price_text": price_text(quote),
        "max_calls": quote.max_calls,
        "price_from": quote.price_from,
        "expires_at": quote.expires_at,
        "worst_case": (
            "The total assumes every source of every field is called for every "
            "subject; fields that read one endpoint share one call. It is charged before the run; a run that delivers no fresh or "
            "repaired value becomes a credit of that charge on your next run."
        ),
    }


def run_view(run: RunRecord) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "plan_id": run.plan_id,
        "quote_id": run.quote_id,
        "mode": run.mode,
        "status": run.status,
        "payment_status": run.payment_status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "calls": run.calls,
        "skipped_calls": run.skipped_calls,
        "counts": dict(run.counts),
        "stop_code": run.stop_code,
        "notices": list(run.notices),
    }


def _shown(field: str, value: str | None, reveal: bool) -> tuple[str | None, bool]:
    """Masked contact details first (:mod:`.pii`), then the untrusted-text cap."""
    shown, hid = masked(field, value, reveal=reveal)
    return _untrusted(shown), hid


def fact_view(row: FactRow, *, reveal_pii: bool = False) -> dict[str, Any]:
    previous = row.previous
    value, hid = _shown(row.field, row.value, reveal_pii)
    before, hid_before = (
        (None, False)
        if previous is None
        else _shown(row.field, previous.value, reveal_pii)
    )
    return {
        "subject": row.subject,
        "field": row.field,
        "value_untrusted": value,
        "pii_masked": hid or hid_before,
        "source": row.source,
        "fetched_at": row.fetched_at,
        "status": row.status,
        "agreement": row.agreement,
        "failover": row.failover,
        "stale_since": row.stale_since,
        "note": row.note,
        "previous": None
        if previous is None
        else {
            "run_id": previous.run_id,
            "value_untrusted": before,
            "status": previous.status,
        },
    }


def changes_view(
    changes: ChangeSet,
    values: dict[tuple[str, str, str], str | None],
    *,
    reveal_pii: bool = False,
) -> dict[str, Any]:
    """``values`` maps (run_id, subject, field) -> stored value, so a change can show
    what the value was and is; history's own summaries are model text we do not keep."""
    out: list[dict[str, Any]] = []
    for change in changes.changes:
        before = values.get((changes.since_run, change.subject, change.field))
        after = (
            values.get((changes.against_run, change.subject, change.field))
            if changes.against_run
            else None
        )
        before_shown, hid_before = _shown(change.field, before, reveal_pii)
        after_shown, hid_after = _shown(change.field, after, reveal_pii)
        out.append(
            {
                "subject": change.subject,
                "field": change.field,
                "change": change.change,
                "what": list(change.what),
                "value_before_untrusted": before_shown,
                "value_after_untrusted": after_shown,
                "pii_masked": hid_before or hid_after,
                "source_before": change.source_before,
                "source_after": change.source_after,
                "source_changed": change.source_changed,
                "status_before": change.status_before,
                "status_after": change.status_after,
            }
        )
    return {
        "since_run": changes.since_run,
        "against_run": changes.against_run,
        "changes": out,
        "note": changes.note,
    }


def ledger_view(entry: LedgerEntry) -> dict[str, Any]:
    return {
        "kind": entry.kind,
        "status": entry.status,
        "amount_usdc": usdc(entry.amount_usdc_micro),
        "amount_usdc_micro": entry.amount_usdc_micro,
        "run_id": entry.run_id,
        "pay_with": entry.pay_with,
        "amount_lamports": entry.amount_lamports,
        "signature": entry.signature,
        "slot": entry.slot,
        "rebate_lamports": entry.rebate_lamports,
        "created_at": entry.created_at,
        "note": entry.note,
    }
