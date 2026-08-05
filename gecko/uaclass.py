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

from collections.abc import Mapping
from typing import Any, cast

from .events import CLIENT_KINDS, ClientKind

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
    # Crawler / CI-tooling verbs seen live landing in the `client` bucket:
    # mcp-scraper, liner-mcp-verifier, mcp-server-validator, mcp-apps-validator. These
    # are unambiguous tooling signals — a real interactive MCP client is never named a
    # scraper/verifier/validator. (Deliberately NOT added: `inspector` — `mcp-inspector`
    # is a legitimate human-driven dev tool, in fact one of our own self-clients; and
    # bare `monitor`/`health`, which can plausibly appear in a real client name. When
    # unsure we leave a marker out: a missed bot is recoverable, a hidden adopter is not.)
    "scraper",
    "verifier",
    "validator",
    "python-requests",
    "go-http-client",
    "curl",
    "wget",
    "httpx",
    "okhttp",
    "java/",
    "node-fetch",
)

#: Named agent PRODUCTS that connect from a server, so their UA is a generic HTTP
#: library while their clientInfo carries a real product name. Checked against the
#: DECLARED client name only (never the UA), and only these exact known products —
#: a substring here outranks the robot rules, so the bar for adding one is "we can
#: name the company." Keep it short; when unsure, leave it out (a missed bot is
#: recoverable, a hidden adopter is not).
_AGENT_PRODUCT_SUBSTRINGS: tuple[str, ...] = ("manus",)

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
    a NAMED agent product wins first, then a robot substring (a faked client name + a
    bot UA is a robot), then a real-MCP-client substring, else ``unknown``. Pure and
    side-effect-free — the whole classification is testable offline.
    """
    haystack = " ".join(part for part in (user_agent, client) if part).lower()
    if not haystack:
        return "unknown"
    # A hosted agent product declares itself in clientInfo but connects from a server,
    # so its UA is a generic HTTP library — and the UA-first rule then buried it as a
    # robot. Manus was misfiled exactly this way: a real product, a real person behind
    # it, dropped from every "non-robot" report we ever ran. The declared product name
    # wins over a generic-library UA.
    #
    # NOT a security relaxation: the WAF floors anything it BLOCKS to ``robot``
    # independently (waf.py `_BLOCKED_CLIENT_KIND`), so a hostile prober that spoofs
    # `manus` still lands as a robot. This label is a metrics classification only.
    if any(marker in (client or "").lower() for marker in _AGENT_PRODUCT_SUBSTRINGS):
        return "client"
    if any(marker in haystack for marker in _ROBOT_SUBSTRINGS):
        return "robot"
    if any(marker in haystack for marker in _CLIENT_SUBSTRINGS):
        return "client"
    return "unknown"


def reclassify_client(row: Mapping[str, Any]) -> ClientKind:
    """Re-derive ``client_kind`` for a STORED event row on READ.

    The persisted ``client_kind`` was frozen at EMIT under whatever classifier rules
    shipped then, so a historical row can carry a STALE label — e.g. an ``mcp-scraper``
    connect stored as ``client`` before ``scraper`` was a robot marker. Any read that
    groups/counts by client kind must therefore re-derive from the raw signals every
    time, never trust the frozen value; this keeps ``classify_client`` the single source
    of truth for both emit and read.

    Re-classifies from the raw ``user_agent`` + ``client`` (clientInfo) via
    ``classify_client``. Falls back to the stored ``client_kind`` ONLY when BOTH raw
    fields are absent (a pre-instrumentation row with nothing to re-derive from); an
    absent/unusable stored value then degrades to ``"unknown"``. Read-only — it derives
    a value and never mutates ``row``.
    """
    user_agent = row.get("user_agent")
    client = row.get("client")
    if user_agent is not None or client is not None:
        return classify_client(
            user_agent if isinstance(user_agent, str) else None,
            client if isinstance(client, str) else None,
        )
    stored = row.get("client_kind")
    if isinstance(stored, str) and stored in CLIENT_KINDS:
        return cast(ClientKind, stored)
    return "unknown"


__all__ = ["classify_client", "reclassify_client"]
