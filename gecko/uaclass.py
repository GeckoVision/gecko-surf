"""Robot/human classification for a connecting MCP client.

The hosted surface's ``gecko_events`` stream mixes real users (Claude Code, Cursor)
with a constant background of crawlers, MCP-registry probers, and security scanners.
To read the connect funnel honestly they must be distinguishable — so every
``surf.connect`` / ``surf.connect_failed`` carries a ``client_kind`` label.

This is a small, deterministic, ordered rule list over the HTTP ``User-Agent`` and the
MCP ``clientInfo`` name — NO heavy deps, NO LLM, no network. It errs toward calling a
thing a ``robot`` when both signals conflict (a crawler that fakes a client NAME in
clientInfo but connects with a ``python-requests`` UA is still a robot): the robot rules
are checked first, so they win the tie.
"""

from __future__ import annotations

from .events import ClientKind

# Substrings (case-insensitive) that mark a crawler / prober / scanner / generic HTTP
# library. Checked FIRST so a robot faking a real client name still classifies as robot.
_ROBOT_SUBSTRINGS: tuple[str, ...] = (
    "bot",
    "crawl",
    "spider",
    "prober",
    "probe",
    "scan",
    "censys",
    "verifymcp",
    "glama",
    "agent-tools",
    "aisec",
    "chiark",
    "span-pipeline",
    # MCP directory / registry probers — they embed "mcp" in clientInfo so they'd
    # otherwise slip past as real clients (seen live: pulsemcp-proctor, prsm-mcp-graph,
    # mcp-rugpull-research). Directory-scan markers → robot.
    "pulsemcp",
    "prsm",
    "proctor",
    "rugpull",
    "research",
    "registry",
    "lookup",
    "directory",
    # The MCP directory / indexer fleet (seen live: glama, agent-tools.cloud,
    # acton-skill-extractor, verifymcp-probe, mcp-indexer). These are PATTERN markers,
    # not literals, so tomorrow's ``foo-mcp-indexer`` / ``bar-probe`` is caught too. Note
    # ``indexer`` MUST precede the ``mcp-`` client rule below (robot wins), so ``mcp-indexer``
    # is a crawler, not a real client. No real MCP client name embeds these substrings.
    "indexer",
    "extractor",
    "python-requests",
    "go-http-client",
    "curl",
    "wget",
    "httpx",
    "okhttp",
    "java/",
    "node-fetch",
)

# Substrings that mark a real MCP client (a person driving an agent). Checked only after
# the robot rules, so a client-shaped name never overrides a robot-shaped UA.
_CLIENT_SUBSTRINGS: tuple[str, ...] = (
    "claude",
    "cursor",
    "cline",
    "windsurf",
    "vscode",
    "mcp-",
    "modelcontextprotocol",
)


def classify_client(user_agent: str | None, client: str | None) -> ClientKind:
    """Classify a connecting client as ``robot`` / ``client`` / ``unknown``.

    Matches the (case-insensitive) UA and clientInfo name against an ordered rule list:
    a robot substring wins first (a faked client name + a bot UA is a robot), then a
    real-MCP-client substring, else ``unknown``. Pure and side-effect-free — the whole
    classification is testable offline.
    """
    haystack = " ".join(part for part in (user_agent, client) if part).lower()
    if not haystack:
        return "unknown"
    if any(marker in haystack for marker in _ROBOT_SUBSTRINGS):
        return "robot"
    if any(marker in haystack for marker in _CLIENT_SUBSTRINGS):
        return "client"
    return "unknown"


__all__ = ["classify_client"]
