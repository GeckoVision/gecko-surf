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
from typing import Literal, cast, get_args

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

#: WHOSE WORD a config is — the loader stance, not a provenance tier.
#:
#: Deliberately not on the provenance ladder and deliberately not derived from one. A tier
#: answers *how do we know this account's recipe*; this answers *did we write this file*.
#: Conflating them is how a provider's assertion would arrive wearing our evidence.
#:
#: * ``packaged`` — read out of our own wheel, from an IDL/source we comprehended. A PDA
#:   node here IS evidence, and the mechanics may earn ``extracted``.
#: * ``external`` — anything else, above all a config fetched from the provider who
#:   hosts it. The same bytes are then an ASSERTION, and every account caps at
#:   ``flagged`` until something independent verifies it.
#:
#: THE THIRD CASE IS NOT DECIDED, and deliberately has no value here: a config our own
#: auto-comprehend path GENERATED from a provider's artifact (``orquestra_comprehend``).
#: Its tiers are our measurement, not the provider's claim, so ``external`` under-claims —
#: but nothing loads such a config back today, so there is no call site to be wrong. Adding
#: a value before there is a caller would be guessing at a rule instead of deriving one.
ConfigTrust = Literal["packaged", "external"]

#: The fail-closed default. A call site that has not thought about trust gets the answer
#: that under-claims; the confident one has to be asked for by name.
DEFAULT_CONFIG_TRUST: ConfigTrust = "external"

__all__ = [
    "ConfigError",
    "ConfigTrust",
    "DEFAULT_CONFIG_TRUST",
    "seed_from_spec",
    "node_from_spec",
    "seed_to_spec",
    "node_to_spec",
    "SpecSource",
    "ArgConstraint",
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
class ArgConstraint:
    """What is known about the VALUES one instruction argument accepts.

    An Anchor IDL types an argument (``string``, ``u64``) and stops there — it has no
    vocabulary for "and it may not contain an underscore". So a constraint here is either
    read out of the program's source (``established_from_source`` true, origin
    ``extracted``/``recovered``) or it is an OBSERVATION: values seen accepted and values
    seen rejected, at the ``manual`` tier, with the rule explicitly not claimed.

    The distinction is the whole point. ``observed_rejected`` is a fact; the rule behind
    it is an inference, and two data points is not enough to make one. ``note`` is
    agent-facing text, so it may contain only what one of the two lists can support.
    """

    arg: str
    #: The program-surface tier this claim is carried at. ``None`` marks an entry that was
    #: PRESENT but not a valid member of the closed ladder — never coerced to a confident
    #: tier, and read downstream as "do not rely on this".
    origin: ProgramProvenanceTier | None
    #: True only when the RULE itself was read out of the program's source or artifact.
    #: False means the lists below are all that is known. Fail-closed: a non-bool is False.
    established_from_source: bool
    observed_rejected: tuple[str, ...] = ()
    observed_accepted: tuple[str, ...] = ()
    note: str = ""


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
    #: Whose word this config is (see :data:`ConfigTrust`). Carried on the artifact so a
    #: reader downstream cannot lose it: by the time a plan is built, "we comprehended
    #: this" and "a provider sent us this" look identical in every other field.
    trust: ConfigTrust = DEFAULT_CONFIG_TRUST
    #: What is known about instruction ARGUMENT values, keyed by argument name. Empty
    #: when the config states nothing — which is the honest default: an argument with no
    #: entry is one nobody has established anything about, not one with no constraint.
    arg_constraints: dict[str, ArgConstraint] = field(default_factory=dict)


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
    data: dict, pdas: dict[str, PdaNode], trust: ConfigTrust = DEFAULT_CONFIG_TRUST
) -> dict[str, ProgramProvenanceTier | None]:
    """Load the R7 ``pda_origins`` sibling map, fail-closed (the defi-security gate).

    UNDER ``external`` TRUST THE MAP IS NOT READ AS EVIDENCE — every account caps at
    ``flagged`` regardless of what the file says, including what it does NOT say. That
    second half is the actual hole: ``_apply_origin_cap`` is demotion-only, so a config
    that supplies ``pdas`` and omits ``pda_origins`` entirely used to earn the top tier by
    staying silent. A provider declaring ``extracted`` about their own file is the same
    unverified assertion, only louder, so it is capped too. Validation below still runs in
    full: capping is not a reason to stop reading a malformed map.

    Under ``packaged`` trust (our own wheel, comprehended from an IDL we read):

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
    if raw is not None:
        if not isinstance(raw, dict):
            raise ConfigError(
                "program.pda_origins must be an object of {account: tier}"
            )
        unknown = sorted(set(raw) - set(pdas))
        if unknown:
            raise ConfigError(
                f"program.pda_origins names unknown account(s) {unknown}; "
                f"known: {sorted(pdas)}"
            )
    if trust != "packaged":
        # Nothing here was established by us, so nothing here may claim a tier. Every
        # account gets the cap-at-flagged marker, including the ones the file never
        # mentioned — silence is what this closes.
        return dict.fromkeys(pdas)
    if raw is None:
        return {}
    origins: dict[str, ProgramProvenanceTier | None] = {}
    for name, value in raw.items():
        if isinstance(value, str) and value in _PROGRAM_TIER_VALUES:
            origins[name] = cast(ProgramProvenanceTier, value)
        else:
            origins[name] = None
    return origins


def _string_tuple(raw: object) -> tuple[str, ...]:
    """Only a list of strings survives. Anything else contributes NOTHING — an
    observation list is evidence, and a malformed one must shrink the claim, never
    widen it with coerced junk."""
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def _arg_constraints_from_dict(data: dict) -> dict[str, ArgConstraint]:
    """Load ``program.arg_constraints``, fail-closed:

    - absent key → empty map (an argument nobody has studied claims nothing);
    - not an object, or an entry that is not an object → loud ``ConfigError``;
    - a tier outside the CLOSED ladder → kept as ``None``, never coerced to a confident
      tier (same rule as ``pda_origins``);
    - ``established_from_source`` that is not a bool → ``False``;
    - the CONTRADICTION — ``established_from_source`` true at the ``manual`` tier, or an
      observation-only entry claiming ``extracted``/``recovered`` — is a loud refusal.
      "Two values were tried on chain" and "the program's source says so" are different
      claims, and the config may not let the weaker one wear the stronger one's label.
    """
    raw = data.get("arg_constraints")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            "program.arg_constraints must be an object of {arg: constraint}"
        )
    constraints: dict[str, ArgConstraint] = {}
    for arg, spec in raw.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"program.arg_constraints[{arg!r}] must be an object")
        tier_raw = spec.get("origin")
        origin = (
            cast(ProgramProvenanceTier, tier_raw)
            if isinstance(tier_raw, str) and tier_raw in _PROGRAM_TIER_VALUES
            else None
        )
        established = spec.get("established_from_source")
        established = established if isinstance(established, bool) else False
        if established and origin not in ("extracted", "recovered"):
            raise ConfigError(
                f"program.arg_constraints[{arg!r}] claims established_from_source with "
                f"origin {tier_raw!r}: only `extracted`/`recovered` may claim the rule "
                "was read from the program's own artifact or source"
            )
        if not established and origin in ("extracted", "recovered"):
            raise ConfigError(
                f"program.arg_constraints[{arg!r}] carries origin {tier_raw!r} but does "
                "not claim established_from_source: an observation may not wear a tier "
                "that implies source derivation (use `manual`)"
            )
        constraints[arg] = ArgConstraint(
            arg=str(arg),
            origin=origin,
            established_from_source=established,
            observed_rejected=_string_tuple(spec.get("observed_rejected")),
            observed_accepted=_string_tuple(spec.get("observed_accepted")),
            note=str(spec.get("note", "")),
        )
    return constraints


def _program_from_dict(
    data: dict, trust: ConfigTrust = DEFAULT_CONFIG_TRUST
) -> ProgramSpec:
    pdas = {name: node_from_spec(name, spec) for name, spec in data["pdas"].items()}
    return ProgramSpec(
        program_id=data["program_id"],
        orquestra_project=data.get("orquestra_project"),
        pdas=pdas,
        intents=tuple(data.get("intents", ())),
        notes=str(data.get("notes", "")),
        pda_origins=_pda_origins_from_dict(data, pdas, trust),
        arg_constraints=_arg_constraints_from_dict(data),
        trust=trust,
    )


def api_config_from_dict(
    data: dict, *, trust: ConfigTrust = DEFAULT_CONFIG_TRUST
) -> ApiConfig:
    """``trust`` says whose word this config is — see :data:`ConfigTrust`. It defaults to
    ``external`` so that forgetting to think about it under-claims rather than over-claims;
    only a loader that knows the bytes are ours may ask for ``packaged``."""
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
        program=_program_from_dict(program, trust) if program else None,
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
        # `packaged` by name: these bytes come out of our own wheel, comprehended
        # from an IDL/source we read, so a PDA node here is evidence rather than a claim.
        api_id: api_config_from_dict(
            _read_json(provider, f"{api_id}.json"), trust="packaged"
        )
        for api_id in provider_cfg.apis
    }
    return provider_cfg, apis


def load_packaged_provider_base_url(provider: str) -> str:
    """The provider's build base URL (e.g. Orquestra's API root) from its config."""
    return str(_read_json(provider, "provider.json")["base_url"])
