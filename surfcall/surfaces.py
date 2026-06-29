"""Surface Registry — the internal control-plane store of comprehended API surfaces.

Day-one model (docs/decisions/2026-06-28-day-one-model.md), build slice 1. Keyed by a
stable ``surface_id``, the registry holds ONLY the API **surface** (its OpenAPI spec) +
metadata (base_url, access tier, revision) — NEVER response payloads, user data, or
secrets (invariant #1). ``build_tools`` stays a pure function of the spec; caching the
spec per ``surface_rev`` means comprehension is computed once per provider and reused
across every customer.

Deliberately, there is **no public/browsable "list all surfaces" route** for agents —
discovery is per-customer (a customer only ever sees the surfaces they provisioned).
``ids()`` exists for internal ops only; exposing it to agents would make us a marketplace.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

Access = Literal["public", "paid"]


class SurfaceError(Exception):
    """Raised on an invalid surface registration."""


def safe_surface_id(name: str) -> str:
    """Normalize a human name into a stable slug usable as a surface_id."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug:
        raise SurfaceError("surface_id cannot be empty after normalization")
    return slug[:64]


def surface_rev(spec: dict[str, Any]) -> str:
    """Stable short content hash of the spec: same spec -> same rev; an edit bumps it.
    Lets corrections/cache attribute to a spec version (pure surface metadata)."""
    blob = json.dumps(spec, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(blob).hexdigest()[:12]


@dataclass(frozen=True)
class Surface:
    surface_id: str
    base_url: str
    access: Access
    surface_rev: str
    spec: dict[str, Any]  # the API SURFACE (control-plane) — never payloads or secrets


class SurfaceRegistry:
    """In-memory control-plane store. Promote to a real DB only when multi-tenant scale
    demands it (justify in the promotion PR) — not before."""

    def __init__(self) -> None:
        self._surfaces: dict[str, Surface] = {}

    def register(
        self,
        surface_id: str,
        spec: dict[str, Any],
        base_url: str,
        access: Access = "public",
    ) -> Surface:
        """Register (or re-register) a comprehended surface. Takes the spec + metadata
        only — there is no parameter through which a response payload could enter."""
        sid = safe_surface_id(surface_id)
        surface = Surface(sid, base_url, access, surface_rev(spec), spec)
        self._surfaces[sid] = surface
        return surface

    def get(self, surface_id: str) -> Surface | None:
        return self._surfaces.get(safe_surface_id(surface_id))

    def ids(self) -> list[str]:
        """Internal ops only — NOT a public catalog (see module docstring)."""
        return sorted(self._surfaces)
