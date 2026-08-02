"""Provider config — the config-driven backbone of the Provider Control Panel.

Turns the hand-built provider surface (``gecko/providers/*.py``) into packaged
DATA a loader reads: PDA seed recipes and program identity become JSON, not code.
Stdlib only (``json`` + ``dataclasses``) so the base install stays dep-light — no
pydantic. See docs/specs/2026-08-01-provider-control-panel.md (§B0).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .pda import (
    ConstantPdaSeedNode,
    OrderedPairPdaSeedNode,
    PdaNode,
    PdaSeed,
    VariablePdaSeedNode,
)

__all__ = [
    "ConfigError",
    "seed_from_spec",
    "node_from_spec",
    "SpecSource",
    "ProgramSpec",
    "ApiConfig",
    "ProviderConfig",
    "api_config_from_dict",
    "provider_config_from_dict",
]


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


# --- config models (dataclasses, not pydantic — base install stays dep-light) ---

# A credential is always a POINTER into the secure store, never an inline value —
# so a config file is safe to commit/share (invariant #1). Enforced at load.
_POINTER_PREFIXES = ("keyring:", "env:")


def _assert_pointer(value: object, where: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.startswith(_POINTER_PREFIXES):
        raise ConfigError(
            f"{where} must be a credential POINTER (keyring:/env:), not an inline value"
        )


@dataclass(frozen=True)
class SpecSource:
    """Where the API surface comes from: an OpenAPI url, a docs url, inline, or a
    Solana program id."""

    type: str
    value: str


@dataclass(frozen=True)
class ProgramSpec:
    """A Solana program's on-chain identity + its recovered PDA recipes (already
    deserialized to :class:`~gecko.pda.PdaNode`) + the intents it exposes."""

    program_id: str
    orquestra_project: str
    pdas: dict[str, PdaNode]
    intents: tuple[str, ...]


@dataclass(frozen=True)
class ApiConfig:
    """One API a provider controls. ``program`` is set for ``kind == "program"``;
    the REST-path fields (``visibility``/``pricing_hints``/``quarantine``/
    ``drift_watch``/``metrics``) are parsed here but consumed by later PRs."""

    api_id: str
    kind: str
    spec_source: SpecSource | None
    program: ProgramSpec | None = None
    auth: dict | None = None
    drift_watch: dict | None = None
    visibility: dict = field(default_factory=dict)
    pricing_hints: dict | None = None
    quarantine: dict | None = None
    metrics: dict | None = None


@dataclass(frozen=True)
class ProviderConfig:
    """A provider (tenant) and the APIs it owns."""

    provider_id: str
    display_name: str
    apis: tuple[str, ...]


def _program_from_dict(data: dict) -> ProgramSpec:
    pdas = {name: node_from_spec(name, spec) for name, spec in data["pdas"].items()}
    return ProgramSpec(
        program_id=data["program_id"],
        orquestra_project=data["orquestra_project"],
        pdas=pdas,
        intents=tuple(data.get("intents", ())),
    )


def api_config_from_dict(data: dict) -> ApiConfig:
    auth = data.get("auth")
    if auth is not None:
        _assert_pointer(auth.get("account_ref"), "auth.account_ref")
    drift = data.get("drift_watch")
    if drift is not None:
        notify = drift.get("notify") or {}
        _assert_pointer(notify.get("target_ref"), "drift_watch.notify.target_ref")
    src = data.get("spec_source")
    program = data.get("program")
    return ApiConfig(
        api_id=data["api_id"],
        kind=data["kind"],
        spec_source=SpecSource(**src) if src else None,
        program=_program_from_dict(program) if program else None,
        auth=auth,
        drift_watch=drift,
        visibility=data.get("visibility", {}),
        pricing_hints=data.get("pricing_hints"),
        quarantine=data.get("quarantine"),
        metrics=data.get("metrics"),
    )


def provider_config_from_dict(data: dict) -> ProviderConfig:
    return ProviderConfig(
        provider_id=data["provider_id"],
        display_name=data.get("display_name", data["provider_id"]),
        apis=tuple(data.get("apis", ())),
    )
