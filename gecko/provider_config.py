"""Provider config — the config-driven backbone of the Provider Control Panel.

Turns the hand-built provider surface (``gecko/providers/*.py``) into packaged
DATA a loader reads: PDA seed recipes and program identity become JSON, not code.
Stdlib only (``json`` + ``dataclasses``) so the base install stays dep-light — no
pydantic. See docs/specs/2026-08-01-provider-control-panel.md (§B0).
"""

from __future__ import annotations

from .pda import (
    ConstantPdaSeedNode,
    OrderedPairPdaSeedNode,
    PdaNode,
    PdaSeed,
    VariablePdaSeedNode,
)

__all__ = ["ConfigError", "seed_from_spec", "node_from_spec"]


class ConfigError(Exception):
    """A provider/API config is malformed or unsafe (e.g. an inline secret)."""


def seed_from_spec(spec: dict) -> PdaSeed:
    """Deserialize one seed spec (the wire form in ``<api>.json``) into a pda node."""
    kind = spec.get("kind")
    if kind == "constant":
        encoding = spec.get("encoding", "bytes")
        raw = spec["value"]
        if encoding == "utf8":
            return ConstantPdaSeedNode(str(raw).encode("utf-8"), encoding="utf8")
        if encoding == "bytes":
            value = bytes(raw) if isinstance(raw, (list, bytes, bytearray)) else str(raw).encode()
            return ConstantPdaSeedNode(value, encoding="bytes")
        raise ConfigError(f"constant seed encoding {encoding!r} unsupported")
    if kind == "variable":
        width = int(spec["width"]) if "width" in spec else None
        return VariablePdaSeedNode(
            spec["name"],
            source=spec["source"],
            encoding=spec.get("encoding", "pubkey"),
            width=width,
        )
    if kind == "ordered_pair":
        return OrderedPairPdaSeedNode(spec["left"], spec["right"], spec["select"])
    raise ConfigError(f"unknown seed kind {kind!r}")


def node_from_spec(name: str, spec: dict) -> PdaNode:
    """Deserialize a PDA recipe (``{"program_id", "seeds": [...]}``) into a PdaNode."""
    seeds = tuple(seed_from_spec(s) for s in spec["seeds"])
    return PdaNode(name, seeds, program_id=spec["program_id"])
