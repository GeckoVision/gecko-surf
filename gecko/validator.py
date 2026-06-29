"""Correctness validator + outcome log (the flywheel seed).

For every generated tool, synthesize valid inputs from its schema and confirm the
comprehension layer produces a *well-formed* request (catching the silent
first-call failure the agent can't see). Each outcome is appended to a JSONL log
— in live mode this accumulates the "how API X is actually called correctly"
corpus that compounds into the moat.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .caller import CallError, build_request
from .client import AgentApiClient
from .sample import example_from_schema


def example_args(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema", {}) or {}
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    return {
        name: example_from_schema(props[name]) for name in props if name in required
    }


def validate_tool(client: AgentApiClient, tool: dict[str, Any]) -> dict[str, Any]:
    args = example_args(tool)
    try:
        build_request(tool, args, client.base_url, client.session.auth_headers())
        return {"tool": tool["name"], "ok": True, "reason": ""}
    except CallError as exc:
        return {"tool": tool["name"], "ok": False, "reason": str(exc)}


def validate_all(client: AgentApiClient, log_path: str | None = None) -> dict[str, Any]:
    results = [validate_tool(client, t) for t in client.list_tools()]
    if log_path:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps(r) + "\n")
    return {
        "total": len(results),
        "ok": sum(1 for r in results if r["ok"]),
        "failed": [r for r in results if not r["ok"]],
        "results": results,
    }
