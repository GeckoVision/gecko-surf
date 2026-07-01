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
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

Access = Literal["public", "paid"]

# A surface's verification state. Only a ``pinned`` surface (an out-of-band trust
# anchor established at provisioning) may have the customer's auth injected toward it.
# ``unverified`` (no anchor) and ``quarantined`` (from-docs / poisoned) degrade to
# recorded-mode with NO auth injection until a human clears them.
State = Literal["pinned", "unverified", "quarantined"]

# Marker the docs_reader stamps on a draft spec's ``info`` (see docs_reader.emit).
_DRAFT_MARKER = "gecko.docs_reader"


class SurfaceError(Exception):
    """Raised on an invalid surface registration."""


def _host_of(url: str | None) -> str | None:
    """Lowercased hostname of a URL, or None if it has none."""
    if not url:
        return None
    host = urlsplit(url).hostname
    return host.lower() if host else None


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


def tools_rev(tools: list[dict[str, Any]]) -> str:
    """Stable short hash of a comprehended tool set. Serve-time integrity anchor: the
    generated tools are re-derived from the pinned spec and this rev is re-asserted, so
    an in-memory tamper of the shipped tool list is caught rather than served."""
    blob = json.dumps(tools, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(blob).hexdigest()[:12]


@dataclass(frozen=True)
class TrustAnchor:
    """Out-of-band trust context for a surface — the WHOLE point of the exfil fix.

    ``trusted_hosts`` is the set of hosts the customer's injected auth may be sent to. It
    is derived from HOW the surface was provisioned (an explicit base_url, or the host we
    ingested a spec URL from), NEVER from the served spec's ``servers[]`` (which a
    poisoned spec controls and could point at an attacker). Fail closed: no pinned host
    => no auth ever leaves the process.
    """

    trusted_hosts: frozenset[str]
    state: State

    @property
    def may_inject_auth(self) -> bool:
        """Auth may be injected ONLY toward a pinned surface with at least one anchor
        host. Unverified / quarantined surfaces get recorded-mode, no-auth behaviour."""
        return self.state == "pinned" and bool(self.trusted_hosts)


def anchor_for(
    *,
    base_url: str | None = None,
    spec_url: str | None = None,
    quarantined: bool = False,
) -> TrustAnchor:
    """Derive a surface's trust anchor from provenance — never from the served spec.

    Precedence (most-trusted provenance wins), and quarantine overrides everything:
      * ``quarantined`` (from-docs / poisoned) -> NO trusted host, state ``quarantined``.
      * explicit ``base_url`` (dev-supplied)   -> its host is the anchor (``pinned``).
      * a ``spec_url`` (the host that served the bytes) -> that host is the anchor.
      * none of the above                      -> NO anchor, state ``unverified``.

    A local FILE is deliberately NOT a pinning provenance: a file on disk (a registry
    download, a vendored-spec PR, a "save this spec") is no more trustworthy than an
    in-memory dict, and its ``servers[0]`` is attacker-controlled. Only a dev-supplied
    ``base_url`` or the URL that actually served the bytes may pin — everything else
    fails closed to ``unverified`` (no auth ever leaves the process).
    """
    if quarantined:
        return TrustAnchor(frozenset(), "quarantined")
    host = _host_of(base_url) or _host_of(spec_url)
    if host:
        return TrustAnchor(frozenset({host.lower()}), "pinned")
    return TrustAnchor(frozenset(), "unverified")


def _walk_has_flag(node: Any, keys: frozenset[str]) -> bool:
    """True if any mapping under ``node`` carries one of ``keys`` (honesty/poison flags)."""
    if isinstance(node, dict):
        if keys & node.keys():
            return True
        return any(_walk_has_flag(v, keys) for v in node.values())
    if isinstance(node, list):
        return any(_walk_has_flag(v, keys) for v in node)
    return False


def _walk_low_confidence(node: Any) -> bool:
    if isinstance(node, dict):
        if node.get("x-draft-confidence") in ("low", "medium"):
            return True
        return any(_walk_low_confidence(v) for v in node.values())
    if isinstance(node, list):
        return any(_walk_low_confidence(v) for v in node)
    return False


def spec_is_quarantined(spec: Any) -> bool:
    """True if a spec is born quarantined: recovered from human docs (a draft), or
    carrying any unreviewed / low-confidence / poison flag.

    A ``from-docs`` surface is poisoned-until-proven — the parser guessed, so no auth may
    be injected until a human clears it. Detection walks the spec for the honesty markers
    the docs_reader emits (``x-review`` / low ``x-draft-confidence`` / the generator
    stamp) plus the sanitizer's ``x-poison-flag``. Enforced by the client / registry —
    NOT bolted into docs_reader.emit (invariant: the engine decides trust, not the input).
    """
    if not isinstance(spec, dict):
        return False
    info = spec.get("info")
    if isinstance(info, dict) and info.get("x-generated-by") == _DRAFT_MARKER:
        return True
    if _walk_has_flag(spec, frozenset({"x-review", "x-poison-flag"})):
        return True
    return _walk_low_confidence(spec)


@dataclass(frozen=True)
class Surface:
    surface_id: str
    base_url: str
    access: Access
    surface_rev: str
    spec: dict[str, Any]  # the API SURFACE (control-plane) — never payloads or secrets
    # Out-of-band trust anchor (see TrustAnchor): the hosts auth may reach + the state.
    trusted_hosts: frozenset[str] = field(default_factory=frozenset)
    state: State = "unverified"


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
        *,
        from_docs: bool = False,
    ) -> Surface:
        """Register (or re-register) a comprehended surface. Takes the spec + metadata
        only — there is no parameter through which a response payload could enter.

        The trust anchor is set here, at provisioning: an explicit ``base_url`` pins the
        surface to that host; a ``from_docs`` (or otherwise poisoned) spec is born
        ``quarantined`` with no trusted host until a human clears it.
        """
        sid = safe_surface_id(surface_id)
        quarantined = from_docs or spec_is_quarantined(spec)
        anchor = anchor_for(base_url=base_url, quarantined=quarantined)
        surface = Surface(
            sid,
            base_url,
            access,
            surface_rev(spec),
            spec,
            trusted_hosts=anchor.trusted_hosts,
            state=anchor.state,
        )
        self._surfaces[sid] = surface
        return surface

    def get(self, surface_id: str) -> Surface | None:
        return self._surfaces.get(safe_surface_id(surface_id))

    def ids(self) -> list[str]:
        """Internal ops only — NOT a public catalog (see module docstring)."""
        return sorted(self._surfaces)
