"""Surface store: named surface documents with rev + entitlement tier.

A surface document is the same JSON that ships in ``gecko/examples`` today —
the registry makes it fetchable so a schema fix is a rev bump, not a release.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gecko.surfaces import surface_rev

TIERS = ("free", "premium")


class RegistryError(Exception):
    """Raised for unknown surfaces or invalid registry configuration."""


@dataclass(frozen=True)
class RegistrySurface:
    name: str
    spec: dict[str, Any] = field(repr=False)
    tier: str = "free"

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise RegistryError(f"unknown tier {self.tier!r}; expected one of {TIERS}")


class SurfaceStore:
    def __init__(self, surfaces: list[RegistrySurface]) -> None:
        self._by_name = {s.name: s for s in surfaces}

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def get(self, name: str) -> RegistrySurface | None:
        return self._by_name.get(name)

    def manifest(self, name: str) -> dict[str, Any]:
        s = self.get(name)
        if s is None:
            raise RegistryError(f"unknown surface: {name}")
        return {
            "name": s.name,
            "surface_rev": surface_rev(s.spec),
            "tier": s.tier,
            "spec": s.spec,
        }
