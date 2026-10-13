"""One row per fact and per check: the value, where it came from, when, and the last
version — in the supplier watch's status vocabulary (``fresh / repaired / stale /
no data``, from ``supplier.fields``; this job has no failover, so never ``repaired``).

A ``Reading`` is what this run saw. ``settle`` turns it into a ``FactRow`` against the
last good stored value (``history.priors``): fresh with ``changed`` set when the value
moved; stale with the last good value kept and marked when the source gave nothing
this run; ``no data`` when there was never a value. A check is never carried stale: a
stale verdict over fresh inputs would be a claim the run did not make.

``value`` is always code-built (a number, a base58 address, a hex digest, our words),
so it may reach a model. ``value_summary`` may quote sanitized page text and stays in
the files.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from ..orthogonal.supplier.fields import FieldState
from ..orthogonal.supplier.history import Prior

FactKind = Literal["fact", "check"]
CheckResult = Literal["ok", "red", "not checked"]

SUPPLY_RAW = "supply_raw"
SUPPLY_UI = "supply_ui"
SUPPLY_SCALED = "supply_scaled"
DECIMALS = "decimals"
MINT_AUTHORITY = "mint_authority"
FREEZE_AUTHORITY = "freeze_authority"
TOKEN_PROGRAM = "token_program"
EXTENSIONS = "extensions"
MULTIPLIER = "multiplier"
TOKEN_PRICE = "token_price_usd"
TOKEN_LIQUIDITY = "token_liquidity_usd"
SHARE_PRICE = "share_price_usd"
RESERVES = "reserves_shares"
CHECK_PRICE_GAP = "check_price_gap_pct"
CHECK_RESERVES = "check_supply_vs_reserves"
CHECK_LEGAL = "check_legal_terms_changed"


@dataclass(frozen=True)
class Reading:
    field: str
    value: str | None
    source: str
    fetched_at: str | None
    model_summary: str
    value_summary: str = ""
    note: str = ""
    kind: FactKind = "fact"
    check: CheckResult | None = None


@dataclass(frozen=True)
class FactRow:
    mint: str
    symbol: str
    field: str
    kind: FactKind
    value: str | None
    value_summary: str
    model_summary: str
    source: str
    fetched_at: str | None
    status: FieldState
    previous_value: str | None
    previous_run_id: str | None
    changed: bool
    check: CheckResult | None
    note: str
    stale_since: str | None = None


def settle(
    reading: Reading, prior: Prior | None, *, mint: str, symbol: str, run_id: str
) -> FactRow:
    last = prior.last_good if prior else None
    base = FactRow(
        mint=mint,
        symbol=symbol,
        field=reading.field,
        kind=reading.kind,
        value=None,
        value_summary="",
        model_summary=reading.model_summary,
        source=reading.source,
        fetched_at=reading.fetched_at,
        status="no data",
        previous_value=last.value if last else None,
        previous_run_id=last.run_id if last else None,
        changed=False,
        check=reading.check,
        note=reading.note,
    )
    if reading.value is not None:
        changed = last is not None and last.value != reading.value
        moved = f" (changed since run {last.run_id})" if changed and last else ""
        return replace(
            base,
            value=reading.value,
            value_summary=reading.value_summary or reading.value,
            model_summary=reading.model_summary + moved,
            status="fresh",
            changed=changed,
        )
    if last is not None and reading.kind == "fact":
        kept_at = last.row.get("fetched_at")
        return replace(
            base,
            value=last.value,
            value_summary=str(last.row.get("value_summary") or last.value),
            model_summary=f"stale: kept the value from run {last.run_id}",
            fetched_at=kept_at if isinstance(kept_at, str) else None,
            status="stale",
            note=f"{reading.note or 'no value this run'}; kept from run {last.run_id}",
            stale_since=(prior.stale_since if prior else None) or run_id,
        )
    return base
