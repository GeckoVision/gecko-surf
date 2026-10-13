"""The hosted xStock file: ``xstock_file`` and ``xstock_changes_since`` over stored runs.

A separate surface rather than ``orthogonal.surface.CommenterListSurface`` with the
lists switched off, because that surface's paid tools would then be one constructor
argument away from a public host. This one has no path to a run: it lists the two read
tools, refuses every other name, and its deps cannot spend even if something reached
them. It has a no-auth session (no Orthogonal key), a zero cap, a transport, RPC and
web reader that refuse, and in-memory ledgers, so nothing it does writes a file.

It mounts only when ``GECKO_XSTOCK_ROOT`` holds xStock runs (see
:func:`build_xstock_read_surface`). An empty folder would answer "no stored file yet"
for every mint. That reads like a correct answer and is actually a broken deploy.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ..access import public_session
from ..caller import PreparedRequest
from ..modes import CallMode
from ..orthogonal.errors import ListJobError
from ..orthogonal.spend import MemoryLedger
from ..orthogonal.supplier.store import MemoryFieldLedger
from . import store
from .deps import XstockDeps, xstock_deps_for
from .errors import LivingFileConfigError
from .surface import XSTOCK_TOOL_NAMES, XSTOCK_TOOLS, call_xstock_tool

logger = logging.getLogger(__name__)

#: The folder of stored runs this host serves. The image sets it; nothing else does.
XSTOCK_ROOT_ENV = "GECKO_XSTOCK_ROOT"

#: Said on every answer, because "mode: live" on a stored file is easy to misread as
#: "fetched just now".
SERVED_FROM = (
    "stored runs on this server; nothing is fetched when you ask. "
    "generated_at says when this file was taken."
)


class HostedReadOnlyError(LivingFileConfigError):
    """Raised by the hosted surface's wire stand-ins. Reaching one is a bug: the two
    read tools answer from Parquet and never call a source."""


def _refuse_transport(_request: PreparedRequest) -> tuple[int, Any]:
    raise HostedReadOnlyError("The hosted xStock file never calls Orthogonal.")


def _refuse_rpc(_url: str, _method: str, _params: list[Any]) -> dict[str, Any]:
    raise HostedReadOnlyError("The hosted xStock file never calls Solana.")


def _refuse_fetch(_url: str) -> tuple[int, str]:
    raise HostedReadOnlyError("The hosted xStock file never fetches a page.")


def read_only_deps(folder: Path, mode: CallMode) -> XstockDeps:
    """Deps that can read ``folder`` in ``mode`` and can reach no source or key."""
    return xstock_deps_for(
        mode,
        folder=folder,
        ledger=MemoryLedger(),
        field_ledger=MemoryFieldLedger(),
        transport=_refuse_transport,
        session=public_session(),
        rpc_call=_refuse_rpc,
        public_fetch=_refuse_fetch,
        per_run_cap=0,
        total_cap=0,
    )


class XstockReadSurface:
    """Duck-typed for ``http_server`` (``instructions`` / ``list_tools`` / ``call_tool``)."""

    surface_id = "gecko:xstock"

    def __init__(self, deps: XstockDeps) -> None:
        self.deps = deps

    @property
    def instructions(self) -> str:
        return (
            "The living file for tokenized stocks (xStocks) on Solana: supply, "
            "authorities, token price, the real share price, legal-terms hashes and "
            "the checks, each with its source, time and status. Call "
            f"{XSTOCK_TOOLS[0]['name']} with a mint, then {XSTOCK_TOOLS[1]['name']} "
            "with an earlier run_id to see what moved. Read-only and free: this "
            f"mount answers from {SERVED_FROM}"
        )

    def list_tools(self) -> list[dict[str, Any]]:
        return [dict(t) for t in XSTOCK_TOOLS]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in XSTOCK_TOOL_NAMES:
            return {"error": f"unknown tool {name}", "code": "unknown_tool"}
        try:
            result = call_xstock_tool(self.deps, name, arguments)
        except ListJobError as exc:
            return {"error": exc.message, "code": exc.code}
        return {**result, "served_from": SERVED_FROM}


def xstock_root() -> Path | None:
    raw = os.environ.get(XSTOCK_ROOT_ENV, "").strip()
    return Path(raw) if raw else None


def _stored_mode(rows: list[dict[str, Any]]) -> CallMode | None:
    """Live runs win over recorded ones: the snapshot a host ships is live, and a
    surface filtered to the other mode would answer "no stored file" for every mint."""
    modes = {r.get("mode") for r in rows}
    if "live" in modes:
        return "live"
    if "recorded" in modes:
        return "recorded"
    return None


def build_xstock_read_surface(root: Path | None = None) -> XstockReadSurface | None:
    """The surface, or ``None`` when there are no xStock runs to serve."""
    folder = root if root is not None else xstock_root()
    if folder is None or not folder.is_dir():
        return None
    if not any(folder.glob(f"*{store.SUFFIXES[1]}")):
        return None
    try:
        rows = store.stored_rows(folder)
    except (ListJobError, OSError, ValueError) as exc:
        # pyarrow missing (ListJobError) or a file it cannot parse (ArrowInvalid is a
        # ValueError, ArrowIOError an OSError): every call would fail the same way, so
        # the mount stays absent and the deploy log says why. Not fatal: one bad file
        # must not take the other mounts down with it.
        logger.error("xstock runs at %s not served: %s", folder, type(exc).__name__)
        return None
    mode = _stored_mode(rows)
    if mode is None:
        return None
    return XstockReadSurface(read_only_deps(folder, mode))
