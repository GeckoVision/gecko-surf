"""A resolution, written down: the table row and the field-ledger row.

Every mapped field leaves one ``kind="field"`` row in the field ledger (which source
filled it, whether a failover happened, why) and shows in the table's ``status`` /
``filled by`` / ``note`` columns. All of it is our own words; the only upstream text
is a value that already passed ``untrusted_summary``, and it goes to the table only.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .calls import KEY_HIERARCHY, KEY_IDENTIFY, subject_of
from .fields import FIELD_ENTITY, NO_PARENT, FieldRow, SupplierFacts
from .repair import RepairContext, Reply, Resolution, resolve
from .runner import Runner
from .sheet import Supplier
from .sources import SOURCE_MAP


def row_for(res: Resolution, default: FieldRow, run_id: str) -> FieldRow:
    """The table row for a resolved field. ``default`` is what ``build_rows`` made."""
    if res.trouble is None:
        return default
    note = f"{res.reason}; {res.check or '; '.join(res.skipped) or 'no other source'}"
    first = res.first.provider
    if res.state == "repaired" and res.filled_by and res.reading:
        snap = res.snapshot
        shown = res.reading.value or ""
        return FieldRow(
            default.supplier,
            res.field_name,
            "no parent named" if shown == NO_PARENT else shown,
            res.filled_by.name,
            f"last snapshot {snap.run_id}" if snap else "none (no last snapshot)",
            "agree" if snap else "only one",
            f"{first} {res.trouble} ({res.short}); filled from "
            f"{res.filled_by.provider}, {res.check}",
            value=res.reading.value,
            filled_by=res.filled_by.name,
            failover=True,
            note=note,
            status="repaired",
        )
    if res.state == "stale" and res.snapshot is not None:
        old = res.snapshot.row
        return FieldRow(
            default.supplier,
            res.field_name,
            f"{old.get('value_summary') or res.snapshot.value} "
            f"(stale since {res.stale_since})",
            str(old.get("source") or ""),
            str(old.get("second_source") or ""),
            old.get("agreement") or "only one",
            f"stale since {res.stale_since}: kept the value from run "
            f"{res.snapshot.run_id}; {first} {res.trouble} ({res.short})",
            value=res.snapshot.value,
            filled_by=str(old.get("filled_by") or old.get("source") or ""),
            failover=True,
            note=note,
            status="stale",
            stale_since=res.stale_since,
        )
    if res.refused:
        return FieldRow(
            default.supplier,
            res.field_name,
            "no data: the failover value did not pass the checks",
            res.first.name,
            default.second_source,
            "no data",
            f"{first} {res.trouble} ({res.short}); {res.check}",
            failover=True,
            note=note,
            status="no data",
        )
    return replace(
        default,
        failover=bool(res.tried),
        note=note,
        model_summary=f"{default.model_summary}; {first} {res.trouble} ({res.short})",
    )


def ledger_row(res: Resolution, run: Runner, supplier: Supplier) -> dict[str, Any]:
    return {
        **run.ledger_base(),
        "kind": "field",
        "subject": subject_of(supplier.domain),
        "field": res.field_name,
        "filled_by": res.filled_by.name if res.filled_by else "",
        "failover": res.trouble is not None,
        "drift": res.reason,
        "outcome": res.state,
    }


def clear_first(facts: SupplierFacts, res: Resolution) -> None:
    """A drifted or failed first source's facts must not reach the default rows."""
    if res.trouble is None:
        return
    if res.first.call_key == KEY_IDENTIFY:
        facts.identify = None
    elif res.first.call_key == KEY_HIERARCHY:
        facts.hierarchy = None


def repair_supplier(
    ctx: RepairContext,
    facts: SupplierFacts,
    replies: dict[str, Reply],
    harvest: list[Any],
) -> list[Resolution]:
    """Resolve each mapped field (entity first: the parent lookup needs its name),
    then set the names the sanctions check screens."""
    s = facts.supplier
    entity_name = s.name
    out = []
    for fs in SOURCE_MAP.values():
        res = resolve(ctx, fs, s, replies, harvest, entity_name)
        clear_first(facts, res)
        if fs.field == FIELD_ENTITY and res.reading and res.reading.value:
            entity_name = res.reading.value
        ctx.run.ledger_rows.append(ledger_row(res, ctx.run, s))
        out.append(res)
    names = [s.name, facts.identify.name if facts.identify else None, facts.pl_name]
    names.append(entity_name)
    facts.sanctions_names = list(dict.fromkeys(n for n in names if n))
    return out
