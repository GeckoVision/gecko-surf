"""SurfaceDoc — the per-operation embed target (control-plane-safe surface projection).

This is the dense arm's storage record. It is a PURE projection of the API *surface*
(method/path/summary/description/param NAMES/tags) plus the §4 situating blurb — never a
response payload, never a param/body VALUE, never a token (invariant #1).

The control-plane guarantee is STRUCTURAL, mirroring ``corpus.outcome_from``: the only
builder, ``surfacedoc_from_operation(op, blurb)``, derives ``content`` from ``Operation``
fields *internally*. There is no ``content``/``result``/``body`` parameter through which a
response body could enter the write path — a free ``content: str`` field would happily
accept a response-body string, so we forbid it by construction (see the control-plane test).

``operation_id`` holds the agent-facing ``tool_name`` (== the lexical arm's join key), so the
dense and lexical arms fuse on one stable key.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .enrich import safe_blurb
from .ingest import Operation
from .tools import _agent_params, tool_name


@dataclass(frozen=True)
class SurfaceDoc:
    """One operation's embed record. Frozen so it cannot accrete fields at runtime; the
    field set IS the persisted schema. ``content`` is derived, never accepted from a caller.

    - ``content``: deterministic surface text (method/path/summary/description/param NAMES).
    - ``contextualized_content``: the §4 Haiku blurb (or ``""`` when it failed the
      sanitizer — fail-closed to content-only).
    - ``embed_text_hash``: hash of ``embed_text`` (content + blurb); the re-embed key so a
      spec edit re-embeds ONLY ops whose own text changed (materialized-view discipline).
    """

    surface_id: str
    operation_id: str
    content: str
    contextualized_content: str
    embed_text_hash: str

    @property
    def embed_text(self) -> str:
        """The exact string the dense index embeds — content situated by its blurb.

        NOT a stored field: derived so no caller can inject an alternate embed body.
        """
        if self.contextualized_content:
            return f"{self.content}\n\n{self.contextualized_content}"
        return self.content


def _content_from_operation(op: Operation) -> str:
    """Derive the deterministic surface text from an ``Operation`` — NAMES and structure
    only, never a param/body VALUE and never ``op.responses`` (a response payload channel).

    Auth-plumbing headers are dropped exactly as they are hidden from the agent
    (``_agent_params``), so the embed text carries decision-relevant surface, not secrets.
    """
    params: list[str] = []
    for p in _agent_params(op):
        typ = p.schema.get("type") if isinstance(p.schema, dict) else None
        params.append(
            f"{p.name}:{typ or 'string'}:{'required' if p.required else 'optional'}"
        )
    lines = [
        f"{op.method} {op.path}",
        op.summary,
        op.description,
        ("tags: " + ", ".join(op.tags)) if op.tags else "",
        f"operation: {op.operation_id}",
        ("params: " + ", ".join(params)) if params else "",
    ]
    return "\n".join(line for line in lines if line and line.strip())


def _embed_text_hash(content: str, contextualized_content: str) -> str:
    digest = hashlib.sha256(
        f"{content}\n\n{contextualized_content}".encode()
    ).hexdigest()
    return f"sha256:{digest}"


def surfacedoc_from_operation(
    op: Operation, blurb: str, *, surface_id: str
) -> SurfaceDoc:
    """The ONLY way to build a ``SurfaceDoc`` — the narrow, control-plane-safe boundary.

    ``content`` is derived from ``op`` internally (no body/response can reach it). ``blurb``
    is run through the sanitizer and FAILS CLOSED to content-only on any hit (a poisoned
    blurb is dropped; the deterministic surface text is still indexed).
    """
    content = _content_from_operation(op)
    contextualized = safe_blurb(blurb)
    return SurfaceDoc(
        surface_id=surface_id,
        operation_id=tool_name(op),
        content=content,
        contextualized_content=contextualized,
        embed_text_hash=_embed_text_hash(content, contextualized),
    )
