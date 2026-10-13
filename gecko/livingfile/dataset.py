"""The living file as an API: the latest file for a mint, and what changed since a run.

Free and read-only: every answer comes from the stored Parquet runs (``store``) in the
surface's own mode — a recorded surface never answers from live runs. Rows go through
``job.safe_row``: code-built values and our words, never page text.
"""

from __future__ import annotations

from typing import Any

from ..orthogonal.supplier.history import changes_since, run_ids
from . import store
from .deps import XstockDeps
from .job import safe_row
from .xstocks import xstock_for

CHANGES_TOOL = "xstock_changes_since"
FILE_TOOL = "xstock_file"


def _rows(deps: XstockDeps, mint: str | None = None) -> list[dict[str, Any]]:
    return [
        r
        for r in store.stored_rows(deps.base.folder)
        if r.get("mode") == deps.mode and (mint is None or r.get("mint") == mint)
    ]


def xstock_file(deps: XstockDeps, mint: str) -> dict[str, Any]:
    xstock = xstock_for(mint)
    rows = _rows(deps, xstock.mint)
    ids = run_ids(rows)
    out: dict[str, Any] = {
        "mode": deps.mode,
        "mint": xstock.mint,
        "symbol": xstock.symbol,
    }
    if not ids:
        return {
            **out,
            "run_id": None,
            "rows": [],
            "note": "No stored file yet for this mint.",
        }
    last = ids[-1]
    mine = [r for r in rows if r.get("run_id") == last]
    checks = {
        str(r["field"]): r.get("check") or "" for r in mine if r.get("kind") == "check"
    }
    return {
        **out,
        "run_id": last,
        "generated_at": mine[0].get("generated_at"),
        "runs_stored": len(ids),
        "checks": checks,
        "rows": [safe_row(r) for r in mine],
    }


def xstock_changes_since(deps: XstockDeps, run_id: str) -> dict[str, Any]:
    rows = _rows(deps)
    base = [r for r in rows if r.get("run_id") == run_id]
    # Run ids are per second, not per mint: compare only the mint that run was about.
    mint = base[0].get("mint") if base else None
    mine = [r for r in rows if mint is None or r.get("mint") == mint]
    diff = changes_since(
        mine, run_id, subject_key=store.SUBJECT_KEY, latest_hint=FILE_TOOL
    )
    return {"mode": deps.mode, **diff.to_dict()}
