"""Operator-authored agent governance — the ``AgentPolicy`` record.

Distinct from ``risk.RiskPolicy`` on purpose. ``RiskPolicy`` is AUTO-DERIVED from the
comprehended surface (allowed_tools / trusted_hosts / score thresholds) — the operator
only tunes numbers. ``AgentPolicy`` is the OPERATOR's explicit governance intent for a
value-moving agent: a per-call spend cap and a recipient allow-list. It is the narrow,
opt-in, high-precision predicate that — intersected with a comprehension-derived
``tier == transfer`` — turns a steered over-cap / off-allowlist transfer into a BLOCK.

Comprehension (the tier) is one input; policy (this record) is the other. A block needs
BOTH — that seam is what keeps governance from drifting into a generic agent firewall.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class AgentPolicy:
    """The operator's governance record for one agent/session.

    ``spend_cap`` — the maximum amount a single ``transfer``-tier call may move; ``None``
    means "no cap authored" (the cap predicate never fires). ``recipient_allowlist`` — the
    exact set of allowed recipient identifiers (addresses / accounts); empty means "no
    allow-list authored" (the recipient predicate never fires). Both are opt-in: an unset
    field is a no-op, so a plain governed session with no policy behaves exactly as today.
    """

    spend_cap: Decimal | None = None
    recipient_allowlist: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        # Ergonomic coercion so callers may pass an int/str cap or a list/tuple allow-list;
        # the stored shape stays a Decimal and a frozenset (hashable, comparable).
        cap = self.spend_cap
        if cap is not None and not isinstance(cap, Decimal):
            object.__setattr__(self, "spend_cap", Decimal(str(cap)))
        allow = self.recipient_allowlist
        if not isinstance(allow, frozenset):
            coerced: Iterable[str] = allow if isinstance(allow, Iterable) else ()
            object.__setattr__(self, "recipient_allowlist", frozenset(coerced))


__all__ = ["AgentPolicy"]
