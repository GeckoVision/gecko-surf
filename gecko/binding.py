"""Per-customer binding — the join that makes the day-one model work.

Build slice 3 (docs/decisions/2026-06-28-day-one-model.md). Given a ``customer_id`` and
a surface they've connected, ``CustomerBinder.bind`` resolves their entitlement, builds
the right ``AuthSession`` (``public_session`` for a public connection, or a BYOK session
from the entitlement's ``cred_ref``), and returns an ``McpSurface`` bound to THAT session.

The result is the whole thesis in one call: the customer's ``/mcp`` shows exactly the
surfaces they connected, and within each, exactly the tools their session can satisfy
(``client._usable_tool_names`` hides auth-gated ops for a no-auth session). This replaces
``http_server``'s single ``surface_id‖server_name`` keying with a customer-keyed view.

The BYOK resolver (``cred_ref -> AuthSession``) is INJECTED, so this slice is
custody-agnostic and testable offline — slice 4 supplies the real one (which decides the
open store-vs-never-store question behind the reference).
"""

from __future__ import annotations

from collections.abc import Callable

from .access import AuthSession, public_session
from .client import AgentApiClient
from .entitlements import Entitlement, Entitlements
from .mcp_server import McpSurface
from .surfaces import SurfaceRegistry

# Turns a BYOK entitlement (carrying an opaque cred_ref) into a live, auth-injecting
# session. The reference -> real-credential resolution is the custody-dependent step.
ByokResolver = Callable[[Entitlement], AuthSession]


class BindingError(Exception):
    """Raised when a customer cannot be bound to a surface."""


def _missing_resolver(ent: Entitlement) -> AuthSession:
    raise BindingError(
        f"surface {ent.surface_id!r} is connected via BYOK but no byok_resolver is configured"
    )


class CustomerBinder:
    def __init__(
        self,
        surfaces: SurfaceRegistry,
        entitlements: Entitlements,
        *,
        byok_resolver: ByokResolver | None = None,
        mode: str = "recorded",
    ) -> None:
        self._surfaces = surfaces
        self._entitlements = entitlements
        self._byok_resolver = byok_resolver or _missing_resolver
        self._mode = mode

    def connected_surfaces(self, customer_id: str) -> list[str]:
        """The customer's own connected set — intra-set discovery, never global."""
        return self._entitlements.surfaces_for(customer_id)

    def bind(self, customer_id: str, surface_id: str) -> McpSurface | None:
        """Bind a customer to one connected surface. Returns None if they haven't
        connected it or the surface isn't comprehended."""
        ent = self._entitlements.get(customer_id, surface_id)
        if ent is None:
            return None
        surface = self._surfaces.get(surface_id)
        if surface is None:
            return None
        session: AuthSession = (
            public_session() if ent.kind == "public" else self._byok_resolver(ent)
        )
        client = AgentApiClient(
            surface.spec, base_url=surface.base_url, session=session
        )
        return McpSurface(client, mode=self._mode)
