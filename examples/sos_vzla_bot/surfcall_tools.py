"""The surfcall⇄LLM seam.

Wraps surfcall's engine (ingest → tools → caller → no-auth access) and exposes it
to a Claude tool-use loop. This is the only place the bot touches surfcall, and the
only place an API response is handled — so it is where the safety boundary lives:

- **Allow-list**: only the 5 public read operations are ever exposed or callable, so
  even a prompt-injected agent can do nothing but read public humanitarian data.
- **Never raises**: ``call`` returns a typed error string, so a bad call degrades the
  reply instead of crashing the bot.
- **Sanitize + cap**: the response is length-capped and the filled URL is never
  echoed back to the agent. The API already masks cédulas / truncates coords; we add
  nothing that de-masks (control-plane discipline carried into the consumer).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from gecko.access import public_session
from gecko.client import AgentApiClient
from gecko.mcp_server import McpSurface


class ToolProvider(Protocol):
    """The seam the agent loop depends on — satisfied by both ``SurfcallTools`` and
    ``MultiSurfaceTools``."""

    def tools_for_llm(self) -> list[dict[str, Any]]: ...
    def call(self, name: str, args: dict[str, Any] | None) -> str: ...


# The SOS Venezuela 2026 public, no-auth, read-only surface — the agent's whole world.
PUBLIC_READS: set[str] = {
    "getReports",
    "searchPersons",
    "getPersonStats",
    "getRecentDamage",
    "getNews",
}

# ReportaVNZLA public, no-auth, read-only surface — a second, larger relief registry
# (~61k people + collection centers, with coordinates). Same safety boundary.
REPORTAVNZLA_READS: set[str] = {
    "searchPersonas",
    "getStats",
    "getRecentFeed",
    "listRecursos",
}


def _cap(payload: dict[str, Any], max_chars: int) -> str:
    """Serialize ``payload`` to JSON within ``max_chars``, keeping it VALID.

    For a list ``data`` (e.g. getNews, ~57KB), drop trailing items until it fits
    rather than byte-truncating mid-structure — the model needs parseable JSON, and
    the newest items lead the list. A ``truncated`` flag tells the agent the tail was
    dropped. Falls back to a byte cap only for an oversized non-list payload.
    """
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


class SurfcallTools:
    def __init__(
        self,
        spec_path: str | Path,
        *,
        mode: str = "recorded",
        allowlist: set[str] | None = None,
        max_chars: int = 6000,
    ) -> None:
        self.allowlist = set(allowlist) if allowlist is not None else set(PUBLIC_READS)
        self.max_chars = max_chars
        client = AgentApiClient(str(spec_path), session=public_session())
        self._surface = McpSurface(client, mode=mode)

    @property
    def tool_names(self) -> set[str]:
        return {t["name"] for t in self.tools_for_llm()}

    def tools_for_llm(self) -> list[dict[str, Any]]:
        """Allow-listed tool defs in the Anthropic tool shape."""
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
        """Execute an allow-listed read and return sanitized, capped JSON. Never raises."""
        if name not in self.allowlist:
            return json.dumps(
                {"error": f"tool no permitida: {name}"}, ensure_ascii=False
            )
        try:
            result = self._surface.call_tool(name, args or {})
        except Exception:  # noqa: BLE001 - degrade the reply, never crash the bot
            return json.dumps(
                {"error": "no se pudo consultar la API en este momento"},
                ensure_ascii=False,
            )
        if isinstance(result, dict):
            payload: dict[str, Any] = {
                "status": result.get("status"),
                "data": result.get("data"),
            }
        else:
            payload = {"data": result}
        return _cap(payload, self.max_chars)


class MultiSurfaceTools:
    """Aggregate several allow-listed surfaces behind ONE tool interface (duck-types
    ``SurfcallTools`` — same ``tools_for_llm`` / ``call`` / ``tool_names``).

    Tool names are unique across our humanitarian surfaces (SOS vs ReportaVNZLA), so a
    name routes unambiguously to its owning surface — no namespacing needed. Every
    surface keeps its own allow-list, never-raises, and cap, so the safety boundary is
    unchanged; the agent just sees the union and can search both registries.
    """

    def __init__(self, surfaces: list[SurfcallTools]) -> None:
        self._surfaces = surfaces
        self._owner: dict[str, SurfcallTools] = {}
        for surface in surfaces:
            for name in surface.tool_names:
                self._owner.setdefault(name, surface)

    @property
    def tool_names(self) -> set[str]:
        return set(self._owner)

    def tools_for_llm(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for surface in self._surfaces:
            out.extend(surface.tools_for_llm())
        return out

    def call(self, name: str, args: dict[str, Any] | None) -> str:
        surface = self._owner.get(name)
        if surface is None:
            return json.dumps(
                {"error": f"tool no permitida: {name}"}, ensure_ascii=False
            )
        return surface.call(name, args)
