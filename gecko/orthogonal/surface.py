"""The MCP face of the list jobs: the commenter list and the supplier watch, no logic.

Duck-typed for ``mcp_server.serve_stdio`` (``instructions`` / ``list_tools`` /
``call_tool``). A refusal comes back as ``{"error", "code"}`` so the transport flags it
``isError``; the model reads the sentence and tells her.

The model never sees comment text: the tools return counts, handles and match classes.
Nor does it see supplier news or trust text: the supplier tools return code-built
summaries, sources and agreement; the quoted text stays in the files.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from ..modes import CallMode
from ..tools import tool_annotations
from .errors import ListJobError
from .job import JobDeps, deps_for, plan_list, run_list
from .supplier import SupplierDeps, plan_watch, run_watch, supplier_deps_for
from .supplier import dataset

if TYPE_CHECKING:
    from ..livingfile.deps import XstockDeps

PLAN_TOOL = "plan_commenter_list"
RUN_TOOL = "build_commenter_list"
PLAN_WATCH_TOOL = "plan_supplier_watch"
RUN_WATCH_TOOL = "run_supplier_watch"
LATEST_TOOL = "supplier_latest"
HISTORY_TOOL = "supplier_history"
CHANGES_TOOL = "supplier_changes_since"
SOURCES_TOOL = "supplier_sources"
WATCH_TOOLS = (
    PLAN_WATCH_TOOL,
    RUN_WATCH_TOOL,
    LATEST_TOOL,
    HISTORY_TOOL,
    CHANGES_TOOL,
    SOURCES_TOOL,
)
_READS = (LATEST_TOOL, HISTORY_TOOL, CHANGES_TOOL, SOURCES_TOOL)

_SHEET_ARG = {
    "sheet": {
        "type": "string",
        "description": "File name of the supplier sheet (CSV with columns name, "
        "domain, ticker — ticker optional) inside ~/gecko-lists/, e.g. suppliers.csv",
    }
}

_ARGS = {
    "post_url": {
        "type": "string",
        "description": "Link to her Instagram post or reel, "
        "e.g. https://www.instagram.com/p/<code>/",
    },
    "customers": {
        "type": "string",
        "description": "File name of her customer sheet (CSV) inside ~/gecko-lists/, "
        "e.g. clientes.csv",
    },
}

_TOOLS: list[dict[str, Any]] = [
    {
        "name": PLAN_TOOL,
        "description": (
            "Who commented on her Instagram post, and who already buys from her? STEP 1: "
            "price the job. Free: reads her sheet, the Orthogonal balance and price. "
            "Show her `message` and wait for a yes before calling build_commenter_list."
        ),
        "inputSchema": {
            "type": "object",
            "properties": dict(_ARGS),
            "required": ["post_url", "customers"],
            "additionalProperties": False,
        },
        "annotations": tool_annotations(
            read_only=True, open_world=True, title="Price a commenter list"
        ),
    },
    {
        "name": RUN_TOOL,
        "description": (
            "STEP 2, only after she said yes to the plan: pays for the comments (about "
            "$0.02, capped at $0.10), marks each commenter customer / new / not sure "
            "against her sheet, and writes the list and a receipt to ~/gecko-lists/. "
            "Show the counts, the first rows and the receipt."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_ARGS,
                "confirm": {
                    "type": "boolean",
                    "description": "true only after she agreed to the plan's price.",
                },
            },
            "required": ["post_url", "customers", "confirm"],
            "additionalProperties": False,
        },
        "annotations": tool_annotations(
            read_only=False, idempotent=False, open_world=True, title="Build the list"
        ),
    },
    {
        "name": PLAN_WATCH_TOOL,
        "description": (
            "Watch suppliers: who they are, parent company, news this month, trust "
            "signals, an OFAC SDN name check and website changes, each from two sources "
            "where there are two. STEP 1, free: reads the sheet, the balance and each "
            "price. Show `message` and the per-supplier prices, and wait for a yes "
            "before calling run_supplier_watch."
        ),
        "inputSchema": {
            "type": "object",
            "properties": dict(_SHEET_ARG),
            "required": ["sheet"],
            "additionalProperties": False,
        },
        "annotations": tool_annotations(
            read_only=True, open_world=True, title="Price a supplier watch"
        ),
    },
    {
        "name": RUN_WATCH_TOOL,
        "description": (
            "STEP 2, only after the person said yes to the plan: pays for the calls "
            "(capped at $0.60 per run), checks the SDN list, and writes the table "
            "(CSV + Parquet) and a receipt to ~/gecko-lists/. Returns per field a "
            "summary, the sources and whether they agree. A sanctions hit is a "
            "'possible match' to check by hand, never a finding."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_SHEET_ARG,
                "confirm": {
                    "type": "boolean",
                    "description": "true only after the person agreed to the plan's price.",
                },
            },
            "required": ["sheet", "confirm"],
            "additionalProperties": False,
        },
        "annotations": tool_annotations(
            read_only=False, idempotent=False, open_world=True, title="Run the watch"
        ),
    },
]


def _read_tool(name: str, description: str, props: dict[str, Any]) -> dict[str, Any]:
    required = [k for k in props if k != "field" or name == HISTORY_TOOL]
    return {
        "name": name,
        "description": description + " Free and read-only: answers from stored runs.",
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": tool_annotations(
            read_only=True, open_world=False, title=name.replace("_", " ")
        ),
    }


_FIELD_ARG = {
    "type": "string",
    "description": "A field name: entity, parent_company, news_this_month, "
    "trust_signals, sanctions or website_changed.",
}
_TOOLS += [
    _read_tool(
        LATEST_TOOL,
        "The latest supplier-watch run: per supplier and field a summary, the source "
        "that filled it, its status (fresh / repaired / stale / no data) and whether "
        "a failover happened.",
        {},
    ),
    _read_tool(
        HISTORY_TOOL,
        "One supplier's one field across every stored run, oldest first.",
        {
            "supplier": {
                "type": "string",
                "description": "Supplier name as in the sheet.",
            },
            "field": _FIELD_ARG,
        },
    ),
    _read_tool(
        CHANGES_TOOL,
        "What changed since a run, per supplier and field: added / changed / removed, "
        "and whether the source moved.",
        {"run_id": {"type": "string", "description": "e.g. 20261008-082501"}},
    ),
    _read_tool(
        SOURCES_TOOL,
        "The ranked sources behind a field (entity, parent_company), their price, the "
        "field path each must still carry, the confirmed field equivalences, and what "
        "the last run saw of each. Omit field for every mapped field.",
        {"field": _FIELD_ARG},
    ),
]


class CommenterListSurface:
    surface_id = "orthogonal-lists"

    def __init__(
        self,
        deps: JobDeps,
        supplier: SupplierDeps | None = None,
        xstock: XstockDeps | None = None,
    ) -> None:
        self._deps = deps
        self._supplier = supplier
        self._xstock = xstock

    @property
    def instructions(self) -> str:
        mode = (
            "LIVE: build_commenter_list and run_supplier_watch spend real credits."
            if (self._deps.mode == "live")
            else "RECORDED: made-up data, nothing is spent."
        )
        return (
            "Builds a list of who commented on an Instagram post, marked customer / "
            "new / not sure against a customer sheet in ~/gecko-lists/. Call "
            f"{PLAN_TOOL} first, show the plan, and call {RUN_TOOL} with confirm=true "
            "only after the person agrees. Supplier watch: the same two steps with "
            f"{PLAN_WATCH_TOOL} then {RUN_WATCH_TOOL}. The stored watch is free to "
            f"read: {LATEST_TOOL}, {HISTORY_TOOL}, {CHANGES_TOOL}, {SOURCES_TOOL}. "
            f"{self._xstock_line()}Gecko sends nothing. {mode}"
        )

    def _xstock_line(self) -> str:
        if self._xstock is None:
            return ""
        from ..livingfile.surface import INSTRUCTIONS

        return INSTRUCTIONS + " "

    def list_tools(self) -> list[dict[str, Any]]:
        tools = [
            dict(t)
            for t in _TOOLS
            if self._supplier is not None or t["name"] not in WATCH_TOOLS
        ]
        if self._xstock is not None:
            from ..livingfile.surface import XSTOCK_TOOLS

            tools += [dict(t) for t in XSTOCK_TOOLS]
        return tools

    def _call_read(self, deps: SupplierDeps, name: str, args: dict[str, Any]) -> Any:
        if name == LATEST_TOOL:
            return dataset.supplier_latest(deps)
        if name == HISTORY_TOOL:
            return dataset.supplier_history(
                deps, str(args.get("supplier", "")), str(args.get("field", ""))
            )
        if name == CHANGES_TOOL:
            return dataset.supplier_changes_since(deps, str(args.get("run_id", "")))
        field = args.get("field")
        return dataset.supplier_sources(deps, str(field) if field else None)

    def _call_watch(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._supplier is None:
            return {"error": f"unknown tool {name}", "code": "unknown_tool"}
        if name in _READS:
            return self._call_read(self._supplier, name, arguments)
        sheet = str(arguments.get("sheet", ""))
        mode = self._supplier.mode
        if name == PLAN_WATCH_TOOL:
            return {"mode": mode, **asdict(plan_watch(self._supplier, sheet))}
        confirm = arguments.get("confirm") is True
        result = run_watch(self._supplier, sheet, confirm=confirm)
        return {"mode": mode, **result.to_dict()}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        post_url = str(arguments.get("post_url", ""))
        customers = str(arguments.get("customers", ""))
        try:
            if name in WATCH_TOOLS:
                return self._call_watch(name, arguments)
            if self._xstock is not None:
                from ..livingfile.surface import XSTOCK_TOOL_NAMES, call_xstock_tool

                if name in XSTOCK_TOOL_NAMES:
                    return call_xstock_tool(self._xstock, name, arguments)
            if name == PLAN_TOOL:
                plan = plan_list(self._deps, post_url, customers)
                return {"mode": self._deps.mode, **asdict(plan)}
            if name == RUN_TOOL:
                confirm = arguments.get("confirm") is True
                result = run_list(self._deps, post_url, customers, confirm=confirm)
                return {"mode": self._deps.mode, **result.to_dict()}
        except ListJobError as exc:
            return {"error": exc.message, "code": exc.code}
        return {"error": f"unknown tool {name}", "code": "unknown_tool"}


def lists_surface(mode: CallMode) -> CommenterListSurface:
    """The surface ``gecko serve --lists`` mounts: recorded by default, live on request."""
    # Imported here: ``livingfile`` imports this package's core, so a top-level import
    # would be a cycle whenever ``livingfile`` is loaded first.
    from ..livingfile.deps import xstock_deps_for

    return CommenterListSurface(
        deps_for(mode), supplier_deps_for(mode), xstock_deps_for(mode)
    )
