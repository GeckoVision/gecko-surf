"""Emit an ingested API's own agent-native discovery surface.

Given a comprehended surface (an :class:`AgentApiClient`), generate the same
agent-native artifacts Gecko publishes for its own sites — ``llms.txt``,
``gecko.json``, ``/.well-known/gecko.json``, and ``tools.md`` — so *any* API Gecko
comprehends becomes discoverable to agents without the provider writing a line.

**Control plane (invariant #1).** Every emitted string — title, description, tag,
operation summary, path, tool name, param name — is UNTRUSTED spec content and is
routed through :func:`_safe`, which (a) runs the anti-poisoning text sanitizer, (b)
redacts secret-shaped tokens with the engine's own ``looks_like_secret_value``
detector, (c) neutralizes markdown/newline structure so a malicious field cannot
forge a heading or a fake callable endpoint in an agent-facing doc, and (d) caps
length. Secret redaction is **best-effort, not a guarantee** — the detector has
finite coverage (e.g. it does not match every vendor key shape). We also emit only
the **host** of ``base_url`` (never a credential-bearing URL) and never a schema
default, an auth header, or a response payload.

The capability map and ``tools.md`` are built from the **usable** tool set (M1: never
advertise a call the agent can't satisfy). **API-agnostic (invariant #2):** the input
is the client; nothing here is specific to any one API.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import sanitize
from .client import AgentApiClient
from .tools import tool_name

# Stable relative paths — the transport layer maps these to routes / files.
ARTIFACT_PATHS: tuple[str, ...] = (
    "llms.txt",
    "gecko.json",
    ".well-known/gecko.json",
    "tools.md",
    "SKILL.md",
)

_TITLE_CAP = 120
_DESC_CAP = 400
_SUMMARY_CAP = 200
_PATH_CAP = 200
_NAME_CAP = 80
_REDACTED = "[redacted]"
_GENERATED_BY = "gecko — github.com/GeckoVision/gecko-surf"

# Chars that could forge markdown structure (headings, code fences, link syntax) in
# an agent-facing doc if an untrusted field carried them.
_MD_STRIP = {ord("#"): None, ord("`"): None, ord("*"): None, ord("|"): None}


def _safe(text: Any, cap: int) -> str:
    """Make one untrusted spec string safe to emit into a control-plane artifact.

    Anti-poisoning sanitize → collapse whitespace/newlines → strip markdown-structure
    chars → redact secret-shaped tokens (best-effort) → word-boundary cap. See the
    module docstring for the guarantee boundary.
    """
    cleaned, _poisoned = sanitize.sanitize_text(text or "")
    s = re.sub(r"\s+", " ", str(cleaned)).translate(_MD_STRIP)
    s = s.replace("[", "(").replace("]", ")")  # neutralize [text](url) link syntax
    s = " ".join(
        _REDACTED if sanitize.looks_like_secret_value(tok) else tok
        for tok in s.split(" ")
    ).strip()
    if len(s) <= cap:
        return s
    cut = s[:cap]
    space = cut.rfind(" ")
    if space > cap * 0.6:  # keep a word boundary unless it would drop too much
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


def _link(site_url: str | None, path: str) -> str:
    """Absolute against ``site_url`` when known, else a root-relative path."""
    if site_url:
        return f"{site_url.rstrip('/')}/{path}"
    return f"/{path}"


def _meta(client: AgentApiClient) -> dict[str, Any]:
    info = client.spec.get("info", {}) if isinstance(client.spec, dict) else {}
    return {
        "title": _safe(info.get("title"), _TITLE_CAP) or "API",
        "description": _safe(info.get("description"), _DESC_CAP),
        # surface_id is _host_of(base_url): host only, credentials stripped.
        "surface": client.surface_id,
        "operations": len(client.operations),
        "tools": len(client.list_tools()),
        "surface_rev": client.surface_rev,
    }


def _capability_map(client: AgentApiClient) -> str:
    """Capabilities grouped by tag — USABLE ops only (never advertise an uncallable
    call), every field routed through ``_safe``."""
    usable = {t["name"] for t in client.list_tools()}
    lines: list[str] = []
    for tag, entries in sorted(client.catalog.by_tag().items()):
        rows: list[str] = []
        for entry in entries:
            op = entry.operation
            if tool_name(op) not in usable:
                continue
            method = _safe(op.method, 10).upper()
            path = _safe(op.path, _PATH_CAP)
            summary = _safe(op.summary, _SUMMARY_CAP)
            rows.append(
                f"- {method} {path} — {summary}" if summary else f"- {method} {path}"
            )
        if rows:
            lines.append(f"## {_safe(tag, _TITLE_CAP)}")
            lines.extend(rows)
    return "\n".join(lines)


def _llms_txt(
    meta: dict[str, Any], caps: str, mcp_url: str | None, site: str | None
) -> str:
    lines = [f"# {meta['title']}", ""]
    if meta["description"]:
        lines += [f"> {meta['description']}", ""]
    lines += [
        f"Served agent-native by Gecko — {meta['operations']} operations, "
        f"{meta['tools']} usable as first-call-correct tools. Gecko is a control "
        f"plane: only the API surface, never your data, payloads, or secrets.",
        "",
        "## Agent integration",
        "",
    ]
    if mcp_url:
        lines.append(
            f"- [MCP endpoint]({mcp_url}): Streamable-HTTP — add it, then "
            f'`search_capabilities("<what you want>")` → call.'
        )
    lines += [
        f"- [gecko.json]({_link(site, 'gecko.json')}): machine-readable manifest",
        f"- [/.well-known/gecko.json]({_link(site, '.well-known/gecko.json')}): discovery manifest",
        f"- [tools.md]({_link(site, 'tools.md')}): every tool, in full",
        "",
        "## Capabilities",
        "",
        caps,
        "",
    ]
    return "\n".join(lines)


def _manifest(
    meta: dict[str, Any], mcp_url: str | None, site: str | None
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "name": meta["title"],
        "description": meta["description"],
        "surface": meta["surface"],
        "operations": meta["operations"],
        "tools": meta["tools"],
        "surface_rev": meta["surface_rev"],
    }
    if mcp_url:
        m["mcp"] = {"url": mcp_url, "transport": "streamable-http"}
    m["artifacts"] = {
        "llms_txt": _link(site, "llms.txt"),
        "tools": _link(site, "tools.md"),
        "well_known": _link(site, ".well-known/gecko.json"),
    }
    m["generated_by"] = _GENERATED_BY
    return m


def _well_known(
    meta: dict[str, Any], mcp_url: str | None, site: str | None
) -> dict[str, Any]:
    wk: dict[str, Any] = {
        "name": meta["title"],
        "description": meta["description"],
        "manifest": _link(site, "gecko.json"),
        "llms_txt": _link(site, "llms.txt"),
    }
    if mcp_url:
        wk["mcp"] = {"url": mcp_url, "transport": "streamable-http"}
    return wk


def _tools_md(client: AgentApiClient, meta: dict[str, Any]) -> str:
    lines = [
        f"# {meta['title']} — tools",
        "",
        f"{meta['tools']} first-call-correct tools. Auth is injected at call time and "
        f"never appears here.",
        "",
    ]
    seen: set[str] = set()
    for tool in client.list_tools():
        name = _safe(tool["name"], _NAME_CAP)
        if name in seen:  # dedup: two operationIds can sanitize to one name
            continue
        seen.add(name)
        invoke = tool.get("_invoke", {}) or {}
        method = _safe(invoke.get("method", ""), 10).upper()
        path = _safe(invoke.get("path", ""), _PATH_CAP)
        schema = tool.get("inputSchema", {}) or {}
        props = schema.get("properties", {}) or {}
        required = set(schema.get("required", []) or [])
        lines.append(f"## {name}")
        if tool.get("description"):
            lines.append(_safe(tool["description"], _DESC_CAP))
        lines.append("")
        if method and path:
            lines.append(f"`{method} {path}`")
            lines.append("")
        if props:
            # Names + types + required flag only — never a schema default value.
            params = ", ".join(
                f"`{_safe(pname, _NAME_CAP)}`{'*' if pname in required else ''}"
                f" ({_safe((pspec or {}).get('type', 'any'), 20)})"
                for pname, pspec in props.items()
            )
            lines.append(f"Inputs: {params}")
            lines.append("")
    return "\n".join(lines)


_TAGS_CAP = 240
# gecko-comprehended: derived from the spec, NOT a hand-authored chub doc. We deliberately
# do NOT emit chub's official|maintainer|community trust tiers — Gecko's trust signal is
# anchor.state + the poison flag (impl spec §2), so a single derivation marker is honest.
_SKILL_SOURCE = "gecko-comprehended"


def _yaml_scalar(text: str) -> str:
    """Emit a `_safe` string as a YAML scalar that always parses.

    JSON strings are a strict subset of YAML flow scalars, so ``json.dumps`` gives a
    double-quoted, fully-escaped value that survives any residue `_safe` leaves (a stray
    quote, colon, ``#``) without ever breaking the frontmatter — the load-bearing guard
    behind emitting UNTRUSTED spec text into a machine-parsed header.
    """
    return json.dumps(text)


def _skill_tags(client: AgentApiClient) -> str:
    """Comma-joined spec tags of the USABLE surface — the skill's discovery vocabulary,
    each tag routed through ``_safe``. Empty string when the spec carries no tags."""
    usable = {t["name"] for t in client.list_tools()}
    seen: list[str] = []
    for op in client.operations:
        if tool_name(op) not in usable:
            continue
        for tag in op.tags:
            clean = _safe(tag, _NAME_CAP).lower()
            if clean and clean not in seen:
                seen.append(clean)
    return _safe(",".join(seen), _TAGS_CAP)


def _skill_md(client: AgentApiClient, meta: dict[str, Any]) -> str:
    """First-call-correct BEHAVIORAL guidance for the comprehended surface, in the
    Agent-Skills YAML-frontmatter shape (installable into any Agent-Skills/chub-aware
    runtime). This is the artifact no OpenAPI dump contains: how to call the API right
    the first time, with auth kept invisible. Single-sourced from ``client``, every field
    through ``_safe``; USABLE ops only. LOCAL/BYOD — a file we write, never a publish."""
    title = meta["title"]
    n = meta["tools"]
    desc = (
        f"Call {title} correctly the first time — {n} first-call-correct agent "
        f"tool{'s' if n != 1 else ''} for a Gecko-comprehended API. Describe your intent; "
        f"auth is injected at call time and never appears here."
    )
    front = [
        "---",
        f"name: {_yaml_scalar(title)}",
        f"description: {_yaml_scalar(_safe(desc, _DESC_CAP))}",
        "metadata:",
        f"  revision: {_yaml_scalar(str(meta['surface_rev']))}",
        f"  source: {_yaml_scalar(_SKILL_SOURCE)}",
        f"  tags: {_yaml_scalar(_skill_tags(client))}",
        "---",
        "",
    ]
    body = [
        f"# {title} — first-call-correct skill",
        "",
        desc,
        "",
        "Gecko comprehended this API into first-call-correct tools — the painful/paywalled "
        "long-tail an agent does not one-shot from raw docs. Auth is injected at call time "
        "and is never in this file (control plane: no secrets, no payloads).",
        "",
        "## Call it right the first time",
        "",
        '1. Intent → tool: `search_capabilities("<what you want to do>")`.',
        '2. Full contract: `get_capability("<tool name>")` → its inputSchema.',
        "3. Call the tool by name with those inputs. Required params are marked `*` below.",
        "",
        "## Tools",
        "",
    ]
    seen: set[str] = set()
    for tool in client.list_tools():
        name = _safe(tool["name"], _NAME_CAP)
        if name in seen:  # two operationIds can sanitize to one name
            continue
        seen.add(name)
        invoke = tool.get("_invoke", {}) or {}
        method = _safe(invoke.get("method", ""), 10).upper()
        path = _safe(invoke.get("path", ""), _PATH_CAP)
        summary = _safe(tool.get("description", ""), _SUMMARY_CAP)
        schema = tool.get("inputSchema", {}) or {}
        required = set(schema.get("required", []) or [])
        params = ", ".join(
            f"{_safe(p, _NAME_CAP)}{'*' if p in required else ''}"
            for p in (schema.get("properties", {}) or {})
        )
        route = f" ({method} {path})" if method and path else ""
        line = f"- {name}{route}"
        if summary:
            line += f" — {summary}"
        body.append(line)
        if params:
            body.append(f"  - inputs: {params}")
    return "\n".join(front + body) + "\n"


def build_artifacts(
    client: AgentApiClient,
    *,
    mcp_url: str | None = None,
    site_url: str | None = None,
) -> dict[str, str]:
    """Return ``{relative_path: text}`` for every agent-native artifact.

    ``mcp_url`` is the live Streamable-HTTP endpoint the agent connects to (omit if the
    API isn't being served over MCP). ``site_url`` makes inter-artifact links absolute
    (e.g. the provider's own domain); relative when omitted.
    """
    meta = _meta(client)
    caps = _capability_map(client)
    return {
        "llms.txt": _llms_txt(meta, caps, mcp_url, site_url),
        "gecko.json": json.dumps(_manifest(meta, mcp_url, site_url), indent=2),
        ".well-known/gecko.json": json.dumps(
            _well_known(meta, mcp_url, site_url), indent=2
        ),
        "tools.md": _tools_md(client, meta),
        "SKILL.md": _skill_md(client, meta),
    }
