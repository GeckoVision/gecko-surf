"""The living file's read tools on ``gecko serve --lists``: two definitions, no logic.

Read-only on purpose: a run spends money, so it is started from the CLI
(``scripts/xstock_file.py``) with its plan shown first; the agent reads what is stored.
"""

from __future__ import annotations

from typing import Any

from ..tools import tool_annotations
from . import dataset
from .deps import XstockDeps

XSTOCK_TOOLS: list[dict[str, Any]] = [
    {
        "name": dataset.FILE_TOOL,
        "description": (
            "The latest living file for a tokenized stock (xStock) on Solana: supply, "
            "authorities, token program, multiplier, token price and liquidity, the "
            "real share price, legal-terms hashes and reserves — each with its source, "
            "when it was fetched, its status (fresh / stale / no data) and its previous "
            "version — plus the checks (price gap, supply vs reserves, legal terms "
            "changed), each ok / red / not checked. Free and read-only: answers from "
            "stored runs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mint": {
                    "type": "string",
                    "description": "The xStock's Solana mint, e.g. NVDAx "
                    "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh",
                }
            },
            "required": ["mint"],
            "additionalProperties": False,
        },
        "annotations": tool_annotations(
            read_only=True, open_world=False, title="xStock living file"
        ),
    },
    {
        "name": dataset.CHANGES_TOOL,
        "description": (
            "What changed in a living file since a stored run, per field: added / "
            "changed / removed, and whether the source moved. Free and read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "e.g. 20261008-082501"}
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
        "annotations": tool_annotations(
            read_only=True, open_world=False, title="xStock changes since"
        ),
    },
]
XSTOCK_TOOL_NAMES = frozenset(t["name"] for t in XSTOCK_TOOLS)
INSTRUCTIONS = (
    f"Tokenized stocks: {dataset.FILE_TOOL} and {dataset.CHANGES_TOOL} read the stored "
    "living file for free."
)


def call_xstock_tool(deps: XstockDeps, name: str, args: dict[str, Any]) -> Any:
    if name == dataset.FILE_TOOL:
        return dataset.xstock_file(deps, str(args.get("mint", "")))
    return dataset.xstock_changes_since(deps, str(args.get("run_id", "")))
