"""The PDA seed-graph model — the on-chain twin of Gecko's call graph.

A Solana program-derived address (PDA) is a deterministic address computed from
an ordered list of *seeds* under a program id. The join an agent needs — "which
account is a PDA, and from what is it derived?" — is exactly what an Anchor IDL
(and therefore an ``llms.txt`` built from one) *loses*: the IDL macro silently
omits the ``pda`` block whenever a seed is a helper-function output (Anchor issue
#4057), and non-Anchor frameworks (Steel, native) never had an IDL at all. The
seed *recipe* lives in the program source, not the spec.

This module re-models the subset of `Codama <https://github.com/codama-idl/codama>`_'s
node graph that expresses that recipe, as pure-stdlib Python dataclasses, plus a
deterministic :func:`derive_pda`. Extraction (source/IDL → these nodes) is Phase
1; this module is the contract everything else is written against.

The model is honest by construction: a seed we cannot statically resolve becomes
a :class:`ResolverPdaSeedNode` (a typed placeholder that still declares its
dependencies) — never fabricated, never silently dropped. That flag is the whole
differentiator over "read the IDL and hope."

Derivation (:func:`derive_pda`) needs ed25519 curve math to reject on-curve
candidates, so it uses ``solders`` behind the optional ``[solana]`` extra; the
model and (later) the extraction are dep-light. See
``docs/specs/2026-07-30-program-surface-pda-graph.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Union

__all__ = [
    "SeedEncoding",
    "SeedSource",
    "ConstantPdaSeedNode",
    "VariablePdaSeedNode",
    "ResolverPdaSeedNode",
    "PdaSeed",
    "PdaNode",
    "DerivedPda",
    "PdaError",
    "PdaDerivationError",
    "UnresolvedSeedError",
    "MissingBindingError",
    "derive_pda",
]

# How a seed's value is encoded into the raw bytes fed to find_program_address.
# This is provenance carried from the source (``authority.to_bytes()`` is a
# pubkey; ``id.to_le_bytes()`` is a little-endian integer) — it tells the
# deriver how to turn a bound value into bytes, and a human how to read the seed.
SeedEncoding = Literal["utf8", "bytes", "le", "be", "pubkey"]

# Where a variable seed's value comes from at call time: an account passed to the
# instruction, or an argument of the instruction. Mirrors Codama's
# AccountValueNode / ArgumentValueNode split.
SeedSource = Literal["account", "argument"]


@dataclass(frozen=True)
class ConstantPdaSeedNode:
    """A fixed seed known at comprehension time — e.g. ``b"config"`` or a
    hardcoded program address.

    ``value`` is the *raw bytes* that enter ``find_program_address``; ``encoding``
    records how the source expressed them (a utf8 string literal, a le integer, a
    pubkey) so the seed round-trips to a human-readable form without re-guessing.
    """

    value: bytes
    encoding: SeedEncoding = "bytes"

    def __post_init__(self) -> None:
        if not isinstance(self.value, (bytes, bytearray)):
            raise PdaError(
                f"ConstantPdaSeedNode.value must be bytes, got {type(self.value).__name__}"
            )


@dataclass(frozen=True)
class VariablePdaSeedNode:
    """A seed slot filled at derivation time from an instruction account or
    argument — e.g. ``authority.to_bytes()`` →
    ``VariablePdaSeedNode("authority", "account", "pubkey")``, or
    ``id.to_le_bytes()`` for a ``u64`` →
    ``VariablePdaSeedNode("id", "argument", "le", width=8)``.

    ``name`` is the source identifier the caller binds a value to. ``width`` is
    the byte width for integer encodings (``u64`` → 8); it is ignored for
    ``pubkey`` / ``utf8`` / ``bytes``.
    """

    name: str
    source: SeedSource
    encoding: SeedEncoding
    width: int | None = None

    def __post_init__(self) -> None:
        if self.encoding in ("le", "be") and self.width is None:
            raise PdaError(
                f"variable seed {self.name!r} is {self.encoding}-encoded but has no width "
                "(integer seeds need a byte width, e.g. u64 -> 8)"
            )


@dataclass(frozen=True)
class ResolverPdaSeedNode:
    """The honest escape hatch — a seed we could *not* statically resolve to a
    constant or a simple account/argument binding.

    Anchor's ``max_key(a, b)`` pool-pair ordering, a hashed seed, a seed read
    from another account's runtime data, a cross-program PDA to a closed-source
    dependency: all of these become a ResolverPdaSeedNode. It still declares
    ``depends_on`` (the identifiers it is derived from) and a human ``reason``, so
    a downstream resolver or person can fill it — but Gecko never fabricates the
    value and never silently drops the account (which is Anchor's own bug). A
    :class:`PdaNode` containing one is not :attr:`~PdaNode.resolvable`.
    """

    name: str
    depends_on: tuple[str, ...] = ()
    reason: str = "seed could not be statically resolved"


# A single seed in a PDA recipe.
PdaSeed = Union[ConstantPdaSeedNode, VariablePdaSeedNode, ResolverPdaSeedNode]


@dataclass(frozen=True)
class PdaNode:
    """A program-derived-address recipe: the ordered seeds + the program the
    address is derived under.

    This is the join the IDL/llms.txt loses. ``program_id`` (base58) is the
    program the PDA belongs to; it may be ``None`` on a node recovered before the
    program id is known and supplied at :func:`derive_pda` time instead.
    """

    name: str
    seeds: tuple[PdaSeed, ...]
    program_id: str | None = None

    @property
    def resolvable(self) -> bool:
        """True iff every seed is a constant or a bindable account/argument.

        A non-resolvable node (one carrying a :class:`ResolverPdaSeedNode`) is
        honestly flagged here rather than dropped, so callers can surface the gap
        instead of deriving a wrong address.
        """
        return not any(isinstance(s, ResolverPdaSeedNode) for s in self.seeds)

    @property
    def variable_seeds(self) -> tuple[VariablePdaSeedNode, ...]:
        """The seed slots a caller must bind (accounts/args) to derive this PDA,
        in seed order. The derivation DAG's inbound edges."""
        return tuple(s for s in self.seeds if isinstance(s, VariablePdaSeedNode))

    @property
    def unresolved_seeds(self) -> tuple[ResolverPdaSeedNode, ...]:
        """The seeds we could not statically resolve — the honest gaps."""
        return tuple(s for s in self.seeds if isinstance(s, ResolverPdaSeedNode))


@dataclass(frozen=True)
class DerivedPda:
    """The result of deriving a :class:`PdaNode`: the base58 address, the canonical
    bump seed, and the node it came from (provenance)."""

    address: str
    bump: int
    node: PdaNode


class PdaError(Exception):
    """Base class for PDA model/derivation errors.

    Messages carry only public data (account names, seed identifiers, base58
    addresses) — never a secret; a PDA recipe has no secret material by
    construction (control-plane invariant #1)."""


class PdaDerivationError(PdaError):
    """Raised when a :class:`PdaNode` cannot be derived."""


class UnresolvedSeedError(PdaDerivationError):
    """Raised when a node carries a :class:`ResolverPdaSeedNode` — Gecko refuses to
    guess an unresolved seed rather than derive a wrong address."""


class MissingBindingError(PdaDerivationError):
    """Raised when a variable seed has no value in the supplied bindings."""


def _encode_seed(
    seed: ConstantPdaSeedNode | VariablePdaSeedNode,
    bindings: Mapping[str, str | int | bytes],
) -> bytes:
    """Turn one resolvable seed into the raw bytes fed to find_program_address."""
    if isinstance(seed, ConstantPdaSeedNode):
        return bytes(seed.value)

    if seed.name not in bindings:
        raise MissingBindingError(
            f"variable seed {seed.name!r} ({seed.source}) has no binding; "
            f"provide bindings[{seed.name!r}]"
        )
    value = bindings[seed.name]

    if seed.encoding == "pubkey":
        return _pubkey_bytes(seed.name, value)
    if seed.encoding == "utf8":
        if not isinstance(value, str):
            raise PdaDerivationError(
                f"seed {seed.name!r} is utf8-encoded; binding must be a str, "
                f"got {type(value).__name__}"
            )
        return value.encode("utf-8")
    if seed.encoding == "bytes":
        if not isinstance(value, (bytes, bytearray)):
            raise PdaDerivationError(
                f"seed {seed.name!r} is bytes-encoded; binding must be bytes, "
                f"got {type(value).__name__}"
            )
        return bytes(value)
    # le / be integer
    if not isinstance(value, int) or isinstance(value, bool):
        raise PdaDerivationError(
            f"seed {seed.name!r} is {seed.encoding}-encoded; binding must be an int, "
            f"got {type(value).__name__}"
        )
    assert seed.width is not None  # guaranteed by VariablePdaSeedNode.__post_init__
    try:
        return value.to_bytes(seed.width, "little" if seed.encoding == "le" else "big")
    except OverflowError as exc:
        raise PdaDerivationError(
            f"seed {seed.name!r} value {value} does not fit in {seed.width} bytes"
        ) from exc


def _pubkey_bytes(name: str, value: str | int | bytes) -> bytes:
    """The 32 on-chain bytes for a pubkey seed, from a base58 str or raw bytes."""
    if isinstance(value, (bytes, bytearray)):
        if len(value) != 32:
            raise PdaDerivationError(
                f"seed {name!r} pubkey bytes must be length 32, got {len(value)}"
            )
        return bytes(value)
    if isinstance(value, str):
        pubkey_cls = _load_pubkey()
        try:
            return bytes(pubkey_cls.from_string(value))
        except Exception as exc:  # solders raises a ValueError-family type
            raise PdaDerivationError(
                f"seed {name!r} is not a valid base58 pubkey"
            ) from exc
    raise PdaDerivationError(
        f"seed {name!r} is pubkey-encoded; binding must be a base58 str or 32 bytes, "
        f"got {type(value).__name__}"
    )


def derive_pda(
    node: PdaNode,
    bindings: Mapping[str, str | int | bytes] | None = None,
    *,
    program_id: str | None = None,
) -> DerivedPda:
    """Compute a PDA's address + canonical bump from its recipe.

    - Constant seeds contribute their bytes directly.
    - Variable seeds are filled from ``bindings`` by name and encoded per the
      seed (a base58 str for ``pubkey``, an int for ``le``/``be``, a str for
      ``utf8``).
    - A :class:`ResolverPdaSeedNode` raises :class:`UnresolvedSeedError` — we do
      not guess.

    ``program_id`` overrides ``node.program_id``; one of the two must be set.
    Requires the optional ``[solana]`` extra (``solders``).
    """
    bindings = bindings or {}

    unresolved = node.unresolved_seeds
    if unresolved:
        names = ", ".join(s.name for s in unresolved)
        raise UnresolvedSeedError(
            f"PDA {node.name!r} has unresolved seed(s) [{names}] and cannot be derived; "
            "resolve them before deriving (Gecko will not fabricate a seed)"
        )

    prog = program_id or node.program_id
    if not prog:
        raise PdaDerivationError(
            f"PDA {node.name!r} has no program_id; pass program_id= or set it on the node"
        )

    seed_bytes = [
        _encode_seed(seed, bindings)
        for seed in node.seeds
        if isinstance(seed, (ConstantPdaSeedNode, VariablePdaSeedNode))
    ]

    pubkey_cls = _load_pubkey()
    try:
        program_pubkey = pubkey_cls.from_string(prog)
    except Exception as exc:
        raise PdaDerivationError(
            f"program_id {prog!r} is not a valid base58 pubkey"
        ) from exc

    address, bump = pubkey_cls.find_program_address(seed_bytes, program_pubkey)
    return DerivedPda(address=str(address), bump=bump, node=node)


def _load_pubkey():  # type: ignore[no-untyped-def]
    """Import solders lazily so the model stays usable without the [solana] extra.

    Raises a clear, actionable error if solders is absent — derivation is the only
    path that needs on-chain crypto; extraction and the model do not.
    """
    try:
        from solders.pubkey import Pubkey
    except ImportError as exc:  # pragma: no cover - exercised via the [solana] extra
        raise PdaDerivationError(
            "PDA derivation needs the 'solana' extra: install with "
            "`pip install gecko-surf[solana]` (or `uv add solders`)"
        ) from exc
    return Pubkey
