"""Entitlements — which customer may access which surface, and how.

Day-one model (docs/decisions/2026-06-28-day-one-model.md), build slice 2. Maps
``(customer_id, surface_id)`` to an access kind: ``public`` (a public surface the
customer connected) or ``byok`` (a paid/auth surface the customer provisioned with
their own credential).

**Control-plane discipline:** an entitlement stores a ``cred_ref`` — an OPAQUE
REFERENCE to a BYOK credential (e.g. ``vault://cust_1/pegana``) — and NEVER the secret
value itself (invariant #1). That keeps this layer custody-agnostic: the reference can
point at an encrypted store or be re-resolved per session — the open store-vs-never-store
decision lives behind the reference, not here.

``surfaces_for(customer_id)`` returns the customer's own connected set — the basis for
**per-customer, intra-set discovery**. There is deliberately no cross-customer or global
view (that would be the marketplace catalog we never build).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .surfaces import safe_surface_id

Kind = Literal["public", "byok"]


class EntitlementError(Exception):
    """Raised on an invalid entitlement grant."""


def _norm_customer_id(customer_id: str) -> str:
    cid = customer_id.strip()
    if not cid:
        raise EntitlementError("customer_id cannot be empty")
    return cid


@dataclass(frozen=True)
class Entitlement:
    customer_id: str
    surface_id: str
    kind: Kind
    cred_ref: str | None = (
        None  # opaque reference to a BYOK credential — NEVER the secret
    )


class Entitlements:
    """In-memory control-plane store of (customer, surface) grants. Promote to a real DB
    only when multi-tenant scale demands it — not before."""

    def __init__(self) -> None:
        self._by: dict[tuple[str, str], Entitlement] = {}

    def grant(
        self,
        customer_id: str,
        surface_id: str,
        kind: Kind = "public",
        cred_ref: str | None = None,
    ) -> Entitlement:
        cid = _norm_customer_id(customer_id)
        sid = safe_surface_id(surface_id)
        if kind == "byok" and not (cred_ref and cred_ref.strip()):
            raise EntitlementError(
                "byok entitlement requires a cred_ref (a reference, not the secret)"
            )
        if kind == "public" and cred_ref is not None:
            raise EntitlementError("public entitlement must not carry a cred_ref")
        ent = Entitlement(cid, sid, kind, cred_ref)
        self._by[(cid, sid)] = ent
        return ent

    def get(self, customer_id: str, surface_id: str) -> Entitlement | None:
        return self._by.get(
            (_norm_customer_id(customer_id), safe_surface_id(surface_id))
        )

    def surfaces_for(self, customer_id: str) -> list[str]:
        """The customer's own connected surfaces (intra-set discovery). Never global."""
        cid = _norm_customer_id(customer_id)
        return sorted(sid for (c, sid) in self._by if c == cid)

    def revoke(self, customer_id: str, surface_id: str) -> bool:
        return (
            self._by.pop(
                (_norm_customer_id(customer_id), safe_surface_id(surface_id)), None
            )
            is not None
        )
