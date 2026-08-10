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
from typing import cast, get_args

from .pda import (
    ConstantPdaSeedNode,
    OrderedPairPdaSeedNode,
    PdaNode,
    PdaSeed,
    ResolverPdaSeedNode,
    VariablePdaSeedNode,
    b58_encode,
)
from .provenance import ProgramProvenanceTier

# The closed tier ladder's values, derived from the single source of truth
# (gecko.provenance) — never redeclared here, so the two cannot drift.
_PROGRAM_TIER_VALUES: frozenset[str] = frozenset(get_args(ProgramProvenanceTier))

__all__ = [
    "ConfigError",
    "seed_from_spec",
    "node_from_spec",
    "seed_to_spec",
    "node_to_spec",
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


def seed_to_spec(seed: PdaSeed) -> dict:
    """Serialize one pda seed node back to the wire form ``seed_from_spec`` reads.

    The exact inverse — ``seed_from_spec(seed_to_spec(s)) == s`` — so a GENERATED
    config (the auto-comprehend path) is byte-comparable with a packaged
    hand-authored one and loads through the same single code path.
    """
    if isinstance(seed, ConstantPdaSeedNode):
        if seed.encoding == "utf8":
            return {
                "kind": "constant",
                "value": seed.value.decode("utf-8"),
                "encoding": "utf8",
            }
        if seed.encoding == "pubkey":
            return {
                "kind": "constant",
                "value": b58_encode(seed.value),
                "encoding": "pubkey",
            }
        return {"kind": "constant", "value": list(seed.value), "encoding": "bytes"}
    if isinstance(seed, VariablePdaSeedNode):
        spec: dict = {
            "kind": "variable",
            "name": seed.name,
            "source": seed.source,
            "encoding": seed.encoding,
        }
        if seed.width is not None:
            spec["width"] = seed.width
        return spec
    if isinstance(seed, OrderedPairPdaSeedNode):
        return {
            "kind": "ordered_pair",
            "left": seed.left,
            "right": seed.right,
            "select": seed.select,
        }
    if isinstance(seed, ResolverPdaSeedNode):
        spec = {
            "kind": "resolver",
            "name": seed.name,
            "depends_on": list(seed.depends_on),
            "reason": seed.reason,
        }
        if seed.resolve is not None:
            spec["resolve"] = dict(seed.resolve)
        return spec
    raise ConfigError(f"cannot serialize seed of type {type(seed).__name__}")


def node_to_spec(node: PdaNode) -> dict:
    """Serialize a PdaNode to the recipe wire form (inverse of ``node_from_spec``)."""
    return {
        "program_id": node.program_id,
        "seeds": [seed_to_spec(s) for s in node.seeds],
    }


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
    # The config's curated prose (gotchas, cross-program warnings, hidden-account
    # facts) — surfaced by find_start's router; empty when the config carries none.
    notes: str = ""
    # R7: the per-account ``ProgramProvenanceTier`` comprehension computed, carried on
    # the artifact (the ``program.pda_origins`` sibling map) instead of re-derived by
    # hand at read time. ``None`` marks an entry that was PRESENT but not a valid tier;
    # find_start treats it as a fail-closed cap at ``flagged`` for that one account.
    # Deliberately a SIBLING map, never a field on the frozen PdaNode: origin is not
    # part of a recipe's identity, and putting it on the node would change equality —
    # which the merge rule and the exact config round-trip both depend on.
    pda_origins: dict[str, ProgramProvenanceTier | None] = field(default_factory=dict)


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


def _pda_origins_from_dict(
    data: dict, pdas: dict[str, PdaNode]
) -> dict[str, ProgramProvenanceTier | None]:
    """Load the R7 ``pda_origins`` sibling map, fail-closed (the defi-security gate):

    - absent key → empty map — read time behaves exactly as before R7;
    - an entry naming an account not in ``pdas`` → loud refusal (mirrors the overlay
      ``include`` unknown-account check) — a typoed or poisoned assertion never loads;
    - a value outside the CLOSED tier ladder (null, non-string, ``cross_surface``,
      ``flagged``, anything else) → kept as ``None``, the per-account cap-at-flagged
      marker: never coerced, never dropped, never defaulted to a confident tier. The
      raw value is deliberately discarded — no text channel from an untrusted file
      may ride into agent-facing output.
    """
    raw = data.get("pda_origins")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("program.pda_origins must be an object of {account: tier}")
    unknown = sorted(set(raw) - set(pdas))
    if unknown:
        raise ConfigError(
            f"program.pda_origins names unknown account(s) {unknown}; "
            f"known: {sorted(pdas)}"
        )
    origins: dict[str, ProgramProvenanceTier | None] = {}
    for name, value in raw.items():
        if isinstance(value, str) and value in _PROGRAM_TIER_VALUES:
            origins[name] = cast(ProgramProvenanceTier, value)
        else:
            origins[name] = None
    return origins


def _program_from_dict(data: dict) -> ProgramSpec:
    pdas = {name: node_from_spec(name, spec) for name, spec in data["pdas"].items()}
    return ProgramSpec(
        program_id=data["program_id"],
        orquestra_project=data.get("orquestra_project"),
        pdas=pdas,
        intents=tuple(data.get("intents", ())),
        notes=str(data.get("notes", "")),
        pda_origins=_pda_origins_from_dict(data, pdas),
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
    # One segment per joinpath call, deliberately. Inside a PyInstaller binary the
    # package resolves to a `MultiplexedPath`, whose `joinpath` accepts exactly ONE
    # segment — the multi-arg form works from source and raises
    # `TypeError: takes 2 positional arguments but 3 were given` once frozen. Every
    # packaged-config read goes through here, so the multi-arg form broke `gecko prove`
    # in the published binary while every test passed from source.
    anchor = (
        resources.files("gecko.providers.configs").joinpath(provider).joinpath(filename)
    )
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
