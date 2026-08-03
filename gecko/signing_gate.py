"""The verdict → signing-gate seam.

Gecko detects poison at **comprehension time** (an ingested spec is untrusted; a tool
whose text trips the sanitizer is quarantined). A downstream **custody / signing layer**
— one that holds the keys and signs transactions — can gate on that verdict: refuse to
release a credential or sign an intent for a tool Gecko quarantined.

This module is the thin, generic adapter that seam needs: a pure decision function over a
:class:`~gecko.surface.SafetyVerdict`. Gecko never holds keys and never signs — it hands
over a verdict; the custody layer decides. Fail-closed by construction: an unknown tool,
or a quarantined one, is denied. Any custody/policy engine (a signing proxy, a
human-approval inbox, a spend-policy) can consume it — Gecko stays the comprehension +
detection layer, it does not become the firewall.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from .surface import SafetyVerdict, Surface

__all__ = ["SigningDecision", "evaluate", "gate_surface_tool"]


@dataclass(frozen=True)
class SigningDecision:
    """Whether a downstream signer should proceed for ``tool``, and the auditable why."""

    allow: bool
    tool: str
    reason: str

    @property
    def denied(self) -> bool:
        return not self.allow


def evaluate(
    verdict: SafetyVerdict,
    tool: str,
    *,
    known_tools: Collection[str] | None = None,
) -> SigningDecision:
    """Decide whether to sign/release a credential for ``tool``, given Gecko's verdict.

    Fail-closed: if ``known_tools`` is supplied and ``tool`` is not among them, deny (never
    sign for a tool that isn't in the comprehended surface). A quarantined tool is denied
    with the sanitizer's already-computed reason; a clean tool is allowed. Per-tool blast
    radius — a poisoned sibling does not deny a clean tool.
    """
    if known_tools is not None and tool not in known_tools:
        return SigningDecision(
            allow=False,
            tool=tool,
            reason="unknown tool — not in the comprehended surface (fail-closed)",
        )
    if tool in verdict.quarantined:
        why = verdict.reasons.get(tool) or "untrusted-spec quarantine"
        return SigningDecision(
            allow=False, tool=tool, reason=f"quarantined at comprehension: {why}"
        )
    return SigningDecision(
        allow=True, tool=tool, reason="clean — no poison detected at comprehension"
    )


def gate_surface_tool(surface: Surface, tool: str) -> SigningDecision:
    """Convenience: evaluate ``tool`` against a live Surface's verdict + known tools."""
    known = {str(t["name"]) for t in surface.tools()}
    return evaluate(surface.safety, tool, known_tools=known)
