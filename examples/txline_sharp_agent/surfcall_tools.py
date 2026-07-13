"""The Gecko⇄LLM seam for the Sharp agent — TxLINE odds reads only.

Wraps Gecko's engine (comprehend → tools → caller → access) and exposes a small,
allow-listed set of TxLINE **read** operations to a Claude tool-use loop. The same
safety discipline as the other examples:

- **Allow-list**: only odds/fixtures reads are ever exposed — never the guest/purchase/
  activate auth operations. A prompt-injected agent can read odds, nothing else.
- **Never raises**: ``call`` returns a typed error string so a bad call degrades the
  reply instead of crashing the loop.
- **Auth stays invisible**: in recorded mode a ``stub_session`` unlocks the gated tools
  with no real token ($0, offline); in live mode the real two tokens are injected at
  call time and never appear in the tool defs the agent sees.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gecko.access import stub_session
from gecko.client import AgentApiClient
from gecko.mcp_server import McpSurface

# The Sharp agent's whole world: read the fixtures list + a fixture's odds. Nothing
# that writes, pays, or authenticates — those tools are never handed to the model.
ODDS_READS: set[str] = {
    "getApiFixturesSnapshot",
    "getApiOddsSnapshotFixtureid",
    "getApiOddsUpdatesFixtureid",
}


def _cap(payload: dict[str, Any], max_chars: int) -> str:
    """Serialize to JSON within ``max_chars``, keeping it valid (drop list tail first)."""
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    data = payload.get("data")
    if isinstance(data, list) and data:
        keep = list(data)
        while keep:
            trial = json.dumps(
                {**payload, "data": keep, "truncated": True},
                ensure_ascii=False,
                default=str,
            )
            if len(trial) <= max_chars:
                return trial
            keep = keep[:-1]
    return text[:max_chars]


class TxlineTools:
    """Allow-listed TxLINE read tools, exposed to a Claude loop. Duck-types the
    ``ToolProvider`` seam the other examples use (``tools_for_llm`` / ``call``)."""

    def __init__(
        self,
        spec_path: str | Path,
        *,
        mode: str = "recorded",
        session: Any = None,
        allowlist: set[str] | None = None,
        max_chars: int = 6000,
    ) -> None:
        # recorded demos default to the stub session (auth headers present, no real
        # token) so the gated odds tools are visible offline; pass a real Session for live.
        self.allowlist = set(allowlist) if allowlist is not None else set(ODDS_READS)
        self.max_chars = max_chars
        client = AgentApiClient(str(spec_path), session=session or stub_session())
        self._surface = McpSurface(client, mode=mode)

    @property
    def tool_names(self) -> set[str]:
        return {t["name"] for t in self.tools_for_llm()}

    def tools_for_llm(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in self._surface.list_tools():
            if t["name"] not in self.allowlist:
                continue
            out.append(
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["inputSchema"],
                }
            )
        return out

    def call(self, name: str, args: dict[str, Any] | None) -> str:
        """Execute an allow-listed read; return sanitized, capped JSON. Never raises."""
        if name not in self.allowlist:
            return json.dumps({"error": f"tool not allowed: {name}"})
        try:
            result = self._surface.call_tool(name, args or {})
        except Exception:  # noqa: BLE001 — degrade the reply, never crash the loop
            return json.dumps({"error": "could not reach the TxLINE surface"})
        if isinstance(result, dict):
            payload: dict[str, Any] = {
                "status": result.get("status"),
                "data": result.get("data"),
            }
        else:
            payload = {"data": result}
        return _cap(payload, self.max_chars)
