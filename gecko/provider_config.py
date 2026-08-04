"""Provider config — the config-driven backbone of the Provider Control Panel.

Turns the hand-built provider surface (``gecko/providers/*.py``) into packaged
DATA a loader reads: PDA seed recipes and program identity become JSON, not code.
Stdlib only (``json`` + ``dataclasses``) so the base install stays dep-light — no
pydantic. See docs/specs/2026-08-01-provider-control-panel.md (§B0).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources

from .pda import (
    ConstantPdaSeedNode,
    OrderedPairPdaSeedNode,
    PdaNode,
    PdaSeed,
    ResolverPdaSeedNode,
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
    "load_packaged_provider",
    "load_packaged_provider_base_url",
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
            value = (
                bytes(raw)
                if isinstance(raw, (list, bytes, bytearray))
                else str(raw).encode()
            )
            return ConstantPdaSeedNode(value, encoding="bytes")
        if encoding == "pubkey":
            # A hardcoded program/account address baked into a seed (e.g. the SPL
            # Token program id in an ATA recipe, or the Token Metadata program in a
            # Metaplex metadata PDA). The raw seed bytes are just the 32-byte pubkey;
            # decode the base58 string here. Solders is behind the [solana] extra, so
            # import it lazily to keep the module import-light (mirrors gecko.pda).
            try:
                from solders.pubkey import Pubkey
            except ImportError as exc:  # pragma: no cover - needs the [solana] extra
                raise ConfigError(
                    "constant pubkey seed needs the 'solana' extra: install with "
                    "`pip install gecko-surf[solana]` (or `uv add solders`)"
                ) from exc
            try:
                value = bytes(Pubkey.from_string(str(raw)))
            except Exception as exc:  # solders raises a ValueError-family type
                raise ConfigError(
                    f"constant pubkey seed value {raw!r} is not a valid base58 pubkey"
                ) from exc
            return ConstantPdaSeedNode(value, encoding="pubkey")
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
    if kind == "resolver":
        # A seed we cannot statically resolve (e.g. a field inside another account's
        # data, like Anchor's `bonding_curve.creator`). Honest by construction: declares
        # its dependencies + reason, never fabricates a value. Makes the node
        # non-resolvable until the value is supplied — the whole differentiator over
        # "read the IDL and hope".
        return ResolverPdaSeedNode(
            spec["name"],
            depends_on=tuple(spec.get("depends_on", ())),
            reason=spec.get("reason", "seed could not be statically resolved"),
            # Optional pure-data recipe (e.g. {"read": "bonding_curve",
            # "field_offset": 49}) telling a control-plane resolver HOW to fill this
            # seed from public on-chain metadata. Carried through verbatim.
            resolve=spec.get("resolve"),
        )
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
    deserialized to :class:`~gecko.pda.PdaNode`) + the intents it exposes.

    ``orquestra_project`` is optional: a program can be comprehended for DERIVATION
    (the first-plan-correct PDA recovery) before its execute/build URL is wired."""

    program_id: str
    pdas: dict[str, PdaNode]
    intents: tuple[str, ...]
    orquestra_project: str | None = None


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
        orquestra_project=data.get("orquestra_project"),
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


# --- packaged config loading (ships in the wheel, read via importlib.resources) ---


def _read_json(provider: str, filename: str) -> dict:
    anchor = resources.files("gecko.providers.configs").joinpath(provider, filename)
    return json.loads(anchor.read_text(encoding="utf-8"))


def load_packaged_provider(
    provider: str,
) -> tuple[ProviderConfig, dict[str, ApiConfig]]:
    """Load a provider's packaged config: ``provider.json`` + one ``<api_id>.json``
    per API, from ``gecko/providers/configs/<provider>/`` (shipped in the wheel)."""
    provider_cfg = provider_config_from_dict(_read_json(provider, "provider.json"))
    apis = {
        api_id: api_config_from_dict(_read_json(provider, f"{api_id}.json"))
        for api_id in provider_cfg.apis
    }
    return provider_cfg, apis


def load_packaged_provider_base_url(provider: str) -> str:
    """The provider's build base URL (e.g. Orquestra's API root) from its config."""
    return str(_read_json(provider, "provider.json")["base_url"])
