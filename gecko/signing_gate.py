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

WHAT ``known_tools`` USED TO ALLOW. It defaulted to ``None``, and ``None`` skipped the
unknown-tool branch entirely — so the shortest call, ``evaluate(verdict, tool)``, was the
one that disabled the module's only fail-closed check. A default that turns a safety
check off is not a default; it is a bypass with good manners. It is now keyword-only with
no default, and :func:`gate_surface_tool` is the preferred entry point precisely because
it derives the list from the same Surface that produced the verdict, so the two cannot
drift apart and neither can be left out.

THE RESIDUAL THIS MODULE DOES NOT CLOSE, stated because a half-closed control described
as closed is worse than an open one. Required arguments make the OMISSION of a verdict
impossible. They do not make a FABRICATED verdict impossible: :class:`SafetyVerdict` is a
plain frozen dataclass, so a caller can hand-build ``SafetyVerdict(total_tools=5,
quarantined=())`` next to a matching ``known_tools`` and be allowed. The zero value —
``total_tools=0`` — is refused below because it is the one forgery you reach by
*accident* (a default construction, a surface that never ran the sanitizer). The rest
needs PROVENANCE on the verdict: evidence it came from a Surface that actually ran the
sanitizer. That is not built, and it is an open item, not a solved one.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from .surface import SafetyVerdict, Surface

__all__ = [
    "SigningDecision",
    "SigningRefused",
    "evaluate",
    "gate_surface_tool",
]


@dataclass(frozen=True)
class SigningDecision:
    """Whether a downstream signer should proceed for ``tool``, and the auditable why."""

    allow: bool
    tool: str
    reason: str

    @property
    def denied(self) -> bool:
        return not self.allow


class SigningRefused(Exception):
    """A path whose next step could be a signature was refused at the gate.

    Raised — not returned — on purpose. The refusal happens BEFORE anything is built or
    simulated, so there is no Receipt and no transaction to put in a result object; a
    returned refusal at that point would have to FABRICATE a Receipt (invent a status and
    a network for a run that never happened), which is a worse lie than an exception.

    Carries the :class:`SigningDecision` and nothing else. No bytes, no receipt, no
    network claim — there is nothing honest to attach.
    """

    def __init__(self, decision: SigningDecision) -> None:
        super().__init__(f"refused to proceed for {decision.tool!r}: {decision.reason}")
        self.decision = decision


def evaluate(
    verdict: SafetyVerdict,
    tool: str,
    *,
    known_tools: Collection[str],
) -> SigningDecision:
    """Decide whether to sign/release a credential for ``tool``, given Gecko's verdict.

    Fail-closed, in this order: an empty ``known_tools`` denies (we know of no surface to
    check against), a verdict covering zero tools denies (a verdict about nothing is not
    an allow), a tool absent from ``known_tools`` denies (never sign for a tool that isn't
    in the comprehended surface), a quarantined tool denies with the sanitizer's
    already-computed reason. Only then is a tool allowed. Per-tool blast radius — a
    poisoned sibling does not deny a clean tool.

    ``known_tools`` is keyword-only with NO default: its old ``None`` default skipped the
    unknown-tool branch, making the check opt-in. Prefer :func:`gate_surface_tool`, which
    derives it from the Surface rather than trusting a caller to keep it in sync.
    """
    if not known_tools:
        return SigningDecision(
            allow=False,
            tool=tool,
            reason="no comprehended surface to check against (fail-closed)",
        )
    if verdict.total_tools <= 0:
        # The zero value of SafetyVerdict reads as `clean` (clean == not quarantined), so
        # a default-constructed verdict — or one from a surface that never ran the
        # sanitizer — would otherwise be an ALLOW for every tool. It is refused here.
        # This catches the ACCIDENTAL forgery only; see the module docstring residual.
        return SigningDecision(
            allow=False,
            tool=tool,
            reason="the verdict covers zero tools — nothing was comprehended or scanned",
        )
    if tool not in known_tools:
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
