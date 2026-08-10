"""Auto-comprehend-on-pick — generate a program config from an Orquestra surface.

Today every Orquestra program surface Gecko serves is a HAND-AUTHORED config
(``gecko/providers/configs/orquestra/<api>.json``). This module generates that
config automatically: point at a project → fetch its machine surface
(:mod:`gecko.orquestra_client`) → recover PDA recipes from the IDL
(:func:`~gecko.pda_extract.from_anchor_idl`) → MERGE with our source-recovered
gap seeds (:func:`~gecko.pda_extract.merge_pda_nodes`, "both, joined": source
rescues the recipes the IDL macro dropped, Anchor #4057) → emit the exact JSON
schema the packaged configs use, loadable through the same single
:mod:`gecko.provider_config` path.

Honesty rules (the differentiator — do not weaken):

- A seed the surface drops and no source recovers stays a
  :class:`~gecko.pda.ResolverPdaSeedNode` — flagged, NEVER fabricated; the
  flagged state survives the merge and is reported in the provenance.
- Provenance is recorded per PDA: ``extracted`` (from the IDL), ``recovered``
  (from source), or ``manual`` (from the overlay), plus a ``flagged`` bit for
  any recipe that still carries an unresolved seed or an unknown program id.
  The tier is EMITTED on the artifact (``program.pda_origins``, R7) so read-time
  consumers report the measured fact instead of re-deriving it by hand; the
  flagged bit stays computed, never emitted as an assertable field.
- Knowledge that CANNOT be derived from surface+source (empirically-discovered
  offsets, hidden ``remaining_accounts``, curated intents) enters only through
  an explicit ``manual_overlay`` — config-as-data, visibly hand-supplied. The
  overlay list is itself the artifact: it quantifies what comprehension beyond
  the surface adds.

SOURCE-TRUST BOUNDARY (founder-ratified): program source enters ONLY as
caller-supplied text/paths — the founder curates and allowlists sources. There
is deliberately NO automatic GitHub (or any remote) source fetching here; the
only network surface is the Orquestra catalog itself. Source text is still
treated as untrusted input (size-capped, parsed by regex, never executed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .orquestra_client import ProjectSurface
from .pda import PdaNode, ResolverPdaSeedNode
from .pda_extract import from_anchor_idl, from_source, merge_pda_nodes_with_origin
from .provenance import ProgramProvenanceTier
from .provider_config import node_from_spec, node_to_spec

__all__ = [
    "ComprehendError",
    "PdaProvenance",
    "ComprehendResult",
    "comprehend_project",
    "diff_configs",
]

# Cap on caller-supplied source text — founder-curated, but still untrusted input.
MAX_SOURCE_BYTES = 2 * 1024 * 1024

# The only keys a manual overlay may carry. An unknown key is refused loudly —
# the overlay is an explicit contract, not a junk drawer. ``why`` is purely
# documentary: {account: reason-this-knowledge-is-hand-supplied} — it makes the
# overlay self-describing as the comprehension-beyond-the-surface artifact.
_OVERLAY_KEYS = frozenset({"api_id", "intents", "notes", "include", "pdas", "why"})

# The closed program-provenance ladder, from the single source of truth
# (gecko.provenance); aliased to keep this module's public name unchanged.
ProvenanceTier = ProgramProvenanceTier


class ComprehendError(Exception):
    """Auto-comprehension failed (bad overlay, unknown account, oversized source)."""


@dataclass(frozen=True)
class PdaProvenance:
    """Where one generated recipe came from, and whether it is honestly flagged.

    ``flagged`` is True when the recipe still carries an unresolved seed or an
    unknown derivation program — the gap is surfaced, never fabricated away.
    """

    tier: ProvenanceTier
    flagged: bool
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComprehendResult:
    """The generated api-config dict (packaged-config JSON schema) + per-PDA
    provenance. ``config`` is what ``gecko.provider_config`` loads.

    Since R7 the config itself carries the per-PDA tier (``program.pda_origins``) —
    it used to be computed here and then DISCARDED from the artifact, forcing
    ``find_start`` to re-derive the tier from hand-maintained maps in a second
    place, which drifted (a hand map cannot express what the merge measured). That
    was a documented decision (byte-comparability with hand-authored configs) and
    R7 consciously retires it: hand-authored configs now carry the map too, and the
    differential proof compares both. ``provenance`` still rides alongside with the
    richer per-run detail (the flagged bit + unresolved seed names) — that part
    stays run metadata, not schema, because ``flagged`` is COMPUTED downstream and
    must never be assertable from a data file."""

    config: dict[str, Any]
    provenance: dict[str, PdaProvenance]

    @property
    def flagged(self) -> tuple[str, ...]:
        """The honestly-flagged recipe names (unresolved seed / unknown program)."""
        return tuple(n for n, p in self.provenance.items() if p.flagged)

    @property
    def manual(self) -> tuple[str, ...]:
        """The overlay-supplied recipe names — what the surface could not give."""
        return tuple(n for n, p in self.provenance.items() if p.tier == "manual")


def _validate_overlay(overlay: Mapping[str, Any]) -> None:
    unknown = set(overlay) - _OVERLAY_KEYS
    if unknown:
        raise ComprehendError(
            f"overlay has unknown key(s) {sorted(unknown)}; allowed: "
            f"{sorted(_OVERLAY_KEYS)}"
        )
    pdas = overlay.get("pdas")
    if pdas is not None and not isinstance(pdas, Mapping):
        raise ComprehendError("overlay 'pdas' must be an object of node specs")
    for key in ("intents", "include"):
        value = overlay.get(key)
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(v, str) for v in value)
        ):
            raise ComprehendError(f"overlay {key!r} must be a list of strings")
    why = overlay.get("why")
    if why is not None and (
        not isinstance(why, Mapping)
        or not all(isinstance(v, str) for v in why.values())
    ):
        raise ComprehendError("overlay 'why' must map account name -> reason string")


def _idl_nodes(surface: ProjectSurface) -> dict[str, PdaNode]:
    """Extract IDL nodes and stamp the project's program id where the IDL itself
    carries none (legacy-shaped IDLs). When the IDL *does* carry an address, a
    ``None`` program id is an explicitly-unpinnable cross-program derivation and
    is left honest (flagged downstream), never defaulted."""
    idl = surface.idl
    nodes = from_anchor_idl(idl)
    idl_has_address = bool(
        idl.get("address") or (idl.get("metadata") or {}).get("address")
    )
    if idl_has_address:
        return nodes
    return {
        name: PdaNode(node.name, node.seeds, program_id=surface.program_id)
        if node.program_id is None
        else node
        for name, node in nodes.items()
    }


def _source_nodes(surface: ProjectSurface, source: str | None) -> dict[str, PdaNode]:
    if source is None:
        return {}
    if len(source.encode("utf-8", errors="replace")) > MAX_SOURCE_BYTES:
        raise ComprehendError(
            f"source text exceeds {MAX_SOURCE_BYTES} bytes — refusing (untrusted "
            "input cap)"
        )
    return from_source(source, program_id=surface.program_id)


def _provenance_for(
    name: str,
    node: PdaNode,
    tier: ProvenanceTier,
) -> PdaProvenance:
    unresolved = tuple(s.name for s in node.seeds if isinstance(s, ResolverPdaSeedNode))
    return PdaProvenance(
        tier=tier,
        # unknown program id is as silently-wrong-address-shaped as an unresolved
        # seed — both must surface as a flag, and the flag must survive the merge.
        flagged=bool(unresolved) or node.program_id is None,
        unresolved=unresolved,
    )


def comprehend_project(
    surface: ProjectSurface,
    *,
    api_id: str,
    source: str | None = None,
    overlay: Mapping[str, Any] | None = None,
) -> ComprehendResult:
    """Generate a program api-config from an Orquestra project surface.

    - ``source``: caller-supplied program source text (the trust boundary — see
      the module docstring); recovers the recipes the IDL dropped and wins over
      opaque IDL seeds via :func:`~gecko.pda_extract.merge_pda_nodes`.
    - ``overlay``: the explicit ``manual_overlay`` — curated ``intents``/
      ``notes``, an ``include`` list (which accounts the served surface exposes),
      and ``pdas`` node specs for knowledge beyond the surface (each replaces or
      adds a recipe, provenance ``manual``).
    """
    overlay = dict(overlay or {})
    _validate_overlay(overlay)

    idl_nodes = _idl_nodes(surface)
    source_nodes = _source_nodes(surface, source)
    # the merge decides which input won, so the merge reports it — this module does
    # NOT re-derive that fact from the merged nodes (a second copy of the rule drifts).
    merged, origins = merge_pda_nodes_with_origin(idl_nodes, source_nodes)

    provenance: dict[str, PdaProvenance] = {
        name: _provenance_for(name, node, origins[name])
        for name, node in merged.items()
    }

    # manual overlay: explicit, hand-supplied knowledge — replaces or adds recipes.
    overlay_pdas = overlay.get("pdas") or {}
    for name, spec in overlay_pdas.items():
        node = node_from_spec(name, dict(spec))
        merged[name] = node
        provenance[name] = _provenance_for(name, node, "manual")

    include = overlay.get("include")
    if include is not None:
        missing = [n for n in include if n not in merged]
        if missing:
            raise ComprehendError(
                f"overlay 'include' names unknown account(s) {missing}; "
                f"comprehended: {sorted(merged)}"
            )
        ordered = list(include)
    else:
        ordered = list(merged)

    program: dict[str, Any] = {
        "program_id": surface.program_id,
        "orquestra_project": surface.slug,
        "intents": list(overlay.get("intents") or ()),
        "pdas": {name: node_to_spec(merged[name]) for name in ordered},
        # R7: the tier is decided at the merge (and stamped ``manual`` at the overlay
        # site), so it is EMITTED on the artifact here — find_start reads it back as a
        # one-way demotion cap instead of re-deriving it from hand-maintained maps.
        "pda_origins": {name: provenance[name].tier for name in ordered},
    }
    notes = overlay.get("notes")
    if notes is not None:
        program["notes"] = str(notes)

    config: dict[str, Any] = {
        "api_id": str(overlay.get("api_id") or api_id),
        "kind": "program",
        "spec_source": {"type": "program", "value": surface.program_id},
        "program": program,
    }
    return ComprehendResult(
        config=config,
        provenance={name: provenance[name] for name in ordered},
    )


def diff_configs(generated: Any, expected: Any, path: str = "$") -> list[str]:
    """Human-readable deltas between a generated config and an expected one.

    The differential proof's loud-failure surface: an empty list means EQUAL;
    otherwise each entry names exactly one field that differs — the fields that
    remain hand-supplied when run without an overlay.
    """
    if isinstance(generated, dict) and isinstance(expected, dict):
        deltas: list[str] = []
        for key in sorted(set(generated) | set(expected)):
            sub = f"{path}.{key}"
            if key not in generated:
                deltas.append(f"missing (hand-supplied): {sub}")
            elif key not in expected:
                deltas.append(f"extra (not in hand-authored): {sub}")
            else:
                deltas.extend(diff_configs(generated[key], expected[key], sub))
        return deltas
    if isinstance(generated, list) and isinstance(expected, list):
        if len(generated) != len(expected):
            return [
                f"differs: {path} (generated {len(generated)} items, "
                f"expected {len(expected)})"
            ]
        deltas = []
        for index, (g, e) in enumerate(zip(generated, expected)):
            deltas.extend(diff_configs(g, e, f"{path}[{index}]"))
        return deltas
    if generated != expected:
        return [
            f"differs: {path} (generated={generated!r:.120}, expected={expected!r:.120})"
        ]
    return []
