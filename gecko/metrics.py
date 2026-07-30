"""Comprehension metrics — the a-ha numbers, measured honestly (control plane only).

The Agent-Surface Report answers "what did comprehension actually buy?" as three
numbers per spec, each **provenance-labeled** so nothing is overclaimed:

* **context-compression** — the raw OpenAPI size vs the projected agent surface (the
  generated tool defs). Bytes are MEASURED; the token count is an ESTIMATE (chars/4,
  labeled). This is the founder's "compress the calls," quantified.
* **surface-readiness** — how many operations yield a well-formed, auth-hidden,
  non-quarantined tool. Labeled ``recorded`` (a structural readiness rate), NEVER
  overclaimed as live-``verified``: a recorded pass is not a wire-confirmed 200.
* **correlation** — a HONEST count off the shipped correlation engine
  (``vindex.value_domain_index`` / ``correlate``): DECLARED value-domain entities on
  the surface, the id-shaped join surface area, self-declared value domains (the §13.1
  richer-signature yield, corroborator-only), and — with peer surfaces — the
  cross-provider joins, split plan-eligible (customer-CONFIRMED) vs quarantined
  candidate. No fabricated float; every field derives from the deterministic engine.

Pure + control-plane (invariant #1): this module reads only the SURFACE — the spec, the
generated tool defs, the deterministic graph — never a response payload, arg value, or
secret. Building an ``AgentApiClient`` from a parsed dict does no network I/O; the caller
(the ``gecko metrics`` CLI) owns the one read from disk/URL and passes the parsed spec in.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from .access import public_session
from .client import AgentApiClient
from .correlate import _id_type
from .surface import Surface

#: Documented, deliberately-simple token estimate. ~4 chars/token is the common rule of
#: thumb across GPT/Claude tokenizers for English+JSON; we LABEL it an estimate rather
#: than pull in a model-specific tokenizer (which would still only approximate any given
#: agent's tokenizer). It is used identically for raw and surface, so the RATIO is stable
#: even though the absolute token counts are approximate.
_CHARS_PER_TOKEN = 4


def _est_tokens(char_count: int) -> int:
    return round(char_count / _CHARS_PER_TOKEN)


@dataclass(frozen=True)
class CompressionMetric:
    """Raw OpenAPI vs the generated agent surface. Bytes MEASURED, tokens ESTIMATED.

    ``reduction_pct`` is the SIGNED truth (``1 - surface/raw``, as a %). A verbose spec
    comprehends SMALLER — noise stripped — so it is positive (**compressed**). A terse spec
    is too sparse to call correctly, so comprehension makes the surface *larger* by ADDING
    the question-shaped, first-call-correct tool descriptions + examples the raw spec lacked;
    ``reduction_pct`` then goes ``<= 0`` (**enriched**). Both are the same measurement — the
    interpretation (:attr:`is_enriched`, :attr:`magnitude_pct`) reads it honestly, never a
    hidden negative dressed as "smaller"."""

    raw_bytes: int
    raw_tokens_est: int
    surface_bytes: int
    surface_tokens_est: int
    reduction_pct: (
        float  # measured: 1 - surface_bytes/raw_bytes, as a percentage (signed)
    )
    token_estimate: str = f"chars/{_CHARS_PER_TOKEN} (estimate, not a tokenizer)"
    basis: str = (
        "measured bytes: raw spec source vs generated tool defs (compact JSON); "
        "% reduction is measured, token counts are estimated"
    )

    @property
    def is_enriched(self) -> bool:
        """True when the surface is LARGER than the raw spec (``reduction_pct <= 0``): a
        terse API where comprehension ADDED the calling context, not stripped noise."""
        return self.reduction_pct <= 0

    @property
    def magnitude_pct(self) -> float:
        """The unsigned size of the change — ``|reduction_pct|``. Read with
        :attr:`is_enriched`: ``+X% smaller`` (compressed) vs ``+X% richer`` (enriched)."""
        return round(abs(self.reduction_pct), 1)


@dataclass(frozen=True)
class ReadinessMetric:
    """Structural surface-readiness — well-formed, auth-hidden, non-quarantined tools.

    ``provenance`` is ``recorded``: this is a comprehension-time readiness rate, NOT a
    live wire-verified correctness claim (that is verify-docs --live's job). A recorded
    pass is never labeled ``verified``."""

    total_ops: int
    well_formed_tools: int
    quarantined_tools: int
    readiness_pct: float
    provenance: str = (
        "recorded (structural readiness: well-formed + auth-hidden + non-quarantined; "
        "NOT live-verified)"
    )


@dataclass(frozen=True)
class CorrelationMetric:
    """A honest correlation score derived from the shipped engine — never a fabricated
    float. Cross-provider joins are counted ONLY with peer surfaces present, and are
    plan-eligible ONLY when customer-CONFIRMED (§13.6); the rest are quarantined
    candidates a human confirms."""

    declared_entities: int  # DECLARED value-domain entities present on this surface
    id_shaped_join_fields: int  # id-shaped fields/params: the raw join surface area
    self_declared_domains: int  # distinct §13.1 discriminating domains (corroborator)
    cross_joins_plan_eligible: int  # customer-CONFIRMED cross-provider joins
    cross_joins_candidate: int  # quarantined cross-provider candidates
    entities: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    provenance: str = (
        "DECLARED value-domain (vindex/correlate); cross-joins DECLARED+CONFIRMED only; "
        "self-declared domains are a corroborator, never a standalone cross-API join"
    )


@dataclass(frozen=True)
class Metrics:
    """The Agent-Surface Report for one comprehended spec — three provenance-labeled
    numbers. ``to_dict`` is JSON-serializable and control-plane clean (no payloads)."""

    surface_id: str
    total_ops: int
    compression: CompressionMetric
    readiness: ReadinessMetric
    correlation: CorrelationMetric
    peers: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface_id": self.surface_id,
            "total_ops": self.total_ops,
            "peers": list(self.peers),
            "compression": asdict(self.compression),
            "readiness": asdict(self.readiness),
            "correlation": asdict(self.correlation),
        }


def _raw_char_count(spec: dict[str, Any], raw_source: str | None) -> tuple[int, int]:
    """(bytes, chars) of the raw OpenAPI. Prefer the real source text the provider ships
    (the honest denominator); fall back to a canonical serialization of the parsed dict."""
    text = (
        raw_source
        if raw_source is not None
        else json.dumps(spec, sort_keys=True, ensure_ascii=False)
    )
    return len(text.encode("utf-8")), len(text)


def _surface_char_count(client: AgentApiClient) -> tuple[int, int]:
    """(bytes, chars) of the projected agent surface — the generated tool defs as compact
    JSON. This is what comprehension HANDS the agent in place of the raw spec."""
    text = json.dumps(client.tools, ensure_ascii=False, separators=(",", ":"))
    return len(text.encode("utf-8")), len(text)


def _compression(
    spec: dict[str, Any], client: AgentApiClient, raw_source: str | None
) -> CompressionMetric:
    raw_bytes, raw_chars = _raw_char_count(spec, raw_source)
    surf_bytes, surf_chars = _surface_char_count(client)
    reduction = (1 - surf_bytes / raw_bytes) * 100 if raw_bytes else 0.0
    return CompressionMetric(
        raw_bytes=raw_bytes,
        raw_tokens_est=_est_tokens(raw_chars),
        surface_bytes=surf_bytes,
        surface_tokens_est=_est_tokens(surf_chars),
        reduction_pct=round(reduction, 1),
    )


def _is_well_formed(tool: Mapping[str, Any]) -> bool:
    """A comprehension-ready tool: a name, an object inputSchema (auth headers already
    stripped by ``tools.to_tool``), and not anti-poison quarantined."""
    schema = tool.get("inputSchema")
    return bool(
        tool.get("name")
        and isinstance(schema, dict)
        and schema.get("type") == "object"
        and not tool.get("x-poison-flag")
    )


def _readiness(client: AgentApiClient) -> ReadinessMetric:
    total = len(client.tools)
    quarantined = len(client._poisoned_tool_names)
    well_formed = sum(
        1
        for t in client.tools
        if _is_well_formed(t) and t["name"] not in client._poisoned_tool_names
    )
    pct = (well_formed / total) * 100 if total else 0.0
    return ReadinessMetric(
        total_ops=total,
        well_formed_tools=well_formed,
        quarantined_tools=quarantined,
        readiness_pct=round(pct, 1),
    )


def _self_declared_domains(surface: Surface) -> tuple[str, ...]:
    """Distinct §13.1 discriminating value domains (the format slot) carried by id-shaped
    field/param nodes — the richer-signature yield. Corroborator only: NEVER a standalone
    cross-API join basis (that gate is in ``correlate._score``)."""
    from .graph import _DISCRIMINATING_FMT

    domains: set[str] = set()
    for node in surface.graph.nodes:
        if node.kind not in ("field", "param") or not node.sig:
            continue
        parts = node.sig.split("|")
        fmt = parts[1] if len(parts) > 1 else ""
        if fmt in _DISCRIMINATING_FMT:
            domains.add(fmt)
    return tuple(sorted(domains))


def _id_shaped_join_fields(surface: Surface) -> int:
    """Count of id-shaped field/param nodes — the raw join surface area (a value-domain
    join key must be id-shaped, per ``correlate._id_type``)."""
    return sum(
        1
        for node in surface.graph.nodes
        if node.kind in ("field", "param") and _id_type(node.sig) is not None
    )


def _correlation(surface: Surface, peers: Sequence[Surface]) -> CorrelationMetric:
    from .vindex import value_domain_index

    entities = tuple(value_domain_index([surface]).entities())

    plan_eligible = 0
    candidate = 0
    if peers:
        index = value_domain_index([surface, *peers])
        peer_ids = {p.surface_id for p in peers}
        for entity in index.entities():
            for join in index.find_correlations(entity):
                # only joins this surface participates in, against a peer
                sides = {join.producer.surface_id, join.consumer.surface_id}
                if surface.surface_id not in sides or not (sides & peer_ids):
                    continue
                if join.plan_eligible:
                    plan_eligible += 1
                else:
                    candidate += 1

    domains = _self_declared_domains(surface)
    return CorrelationMetric(
        declared_entities=len(entities),
        id_shaped_join_fields=_id_shaped_join_fields(surface),
        self_declared_domains=len(domains),
        cross_joins_plan_eligible=plan_eligible,
        cross_joins_candidate=candidate,
        entities=entities,
        domains=domains,
    )


def _surface_for(
    spec: str | dict[str, Any],
    *,
    surface_id: str,
    declared_hints: Mapping[str, str] | None,
) -> Surface:
    client = AgentApiClient(
        spec,
        session=public_session(),
        surface_id=surface_id,
        declared_hints=declared_hints,
    )
    return Surface.of(client)


def compute_metrics(
    spec: dict[str, Any],
    *,
    raw_source: str | None = None,
    surface_id: str | None = None,
    declared_hints: Mapping[str, str] | None = None,
    peers: Sequence[Surface] | None = None,
) -> Metrics:
    """The Agent-Surface Report for one comprehended spec — pure, control-plane only.

    ``spec`` is a PARSED OpenAPI dict (the caller owns the one disk/URL read).
    ``raw_source`` is the original spec text, used as the honest compression denominator
    (falls back to a canonical re-serialization). ``declared_hints`` inject customer-
    CONFIRMED entity vocabulary (the join-plan-eligibility gate); ``peers`` are other
    loaded Surfaces to count cross-provider joins against.
    """
    client = AgentApiClient(
        spec,
        session=public_session(),
        surface_id=surface_id,
        declared_hints=declared_hints,
    )
    surface = Surface.of(client)
    return Metrics(
        surface_id=surface.surface_id,
        total_ops=len(client.operations),
        compression=_compression(spec, client, raw_source),
        readiness=_readiness(client),
        correlation=_correlation(surface, peers or ()),
        peers=tuple(p.surface_id for p in (peers or ())),
    )


__all__ = [
    "CompressionMetric",
    "CorrelationMetric",
    "Metrics",
    "ReadinessMetric",
    "compute_metrics",
]
