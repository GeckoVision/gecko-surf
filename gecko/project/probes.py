"""ProgramGraph -> the probe cases a scorecard runs. The list nobody types by hand.

A CI scorecard for a program answers "can this instruction be built, does it survive
the chain, is the compute figure honest" — one row per instruction. Each row needs a
CASE: every account the instruction takes, with an address; every arg, with a value;
a fee payer. Written by hand that list rots the moment the IDL moves, and it rots
SILENTLY: an omitted account and a mistyped arg name both come back as an error from
the program, so the scorecard marks the SURFACE red for a defect in its own probe.
That happened here — ``delete_product`` was probed without ``system_program`` and
with ``name`` where the instruction takes ``product_name``, and both facts were
already stated correctly in the graph the scorecard was scoring.

So the shape of a case is derived, never authored:

- **which accounts** — the instruction's account list, in the IDL's own order.
- **which args, and their names** — the instruction's arg list.
- **PDA addresses** — derived from the graph's own seed recipe with
  :func:`gecko.pda.derive_pda`, in the graph's derivation order, so an account that
  seeds another is resolved before the account that needs it.
- **pinned addresses** — the IDL's ``address`` for the system/token/ATA programs is
  a fact from the spec; a caller is never asked for it.
- **the fee payer** — the first non-PDA signer the instruction declares.

``bindings`` supplies only what a caller genuinely has to CHOOSE: their wallet, the
store, the product, the numbers. It is keyed by the graph's own names, and a name it
does not carry is a refusal (:class:`MissingProbeBindingError`) — never a default,
never a guess. A probe that invents a value hands the chain the probe's invention to
judge, and the report cannot tell that apart from a defect in the surface.

Values arrive as strings and are converted using the arg's DECLARED IDL type, so a
``u8`` that will not fit is refused here rather than misencoded on the wire.

Pure: no network, no chain reads (a PDA is arithmetic), no hidden state.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, TypeAlias

from ..pda import PdaError, VariablePdaSeedNode, derive_pda
from ..program_graph import AccountRef, InstructionGraph, ProgramGraph, SeedBinding
from .errors import FdlProjectionError
from .graph_read import arg_aliases, find_instruction

__all__ = [
    "ProbeCase",
    "ProbeError",
    "ProbeValue",
    "MissingProbeBindingError",
    "UnderivableAccountError",
    "UnprobableArgTypeError",
    "probe_case",
    "probe_cases",
]

#: What an instruction argument can be once the IDL's declared type has been applied.
ProbeValue: TypeAlias = "str | int | bool"


class ProbeError(FdlProjectionError):
    """Base class for every refusal to derive a probe case.

    It descends from :class:`~gecko.project.errors.FdlProjectionError` because it is
    the same commitment one level along: a projector that cannot state a fact
    honestly raises instead of emitting a plausible one. A caller that catches the
    package's base error catches this too.
    """


class MissingProbeBindingError(ProbeError):
    """The graph needs a value the caller did not supply.

    THE refusal. Every alternative is worse: a zero pubkey builds and reverts against
    an account nobody meant, a blank string derives a real PDA belonging to a store
    that is not yours, and both come back as the program's error — which a scorecard
    then prints as a red row for the surface.
    """


class UnderivableAccountError(ProbeError):
    """A PDA account of this instruction cannot be derived from the graph — an
    unresolved seed, or a seed-dependency cycle. The graph says so; this refuses
    rather than putting a well-formed wrong address in a probe."""


class UnprobableArgTypeError(ProbeError):
    """An instruction arg's declared IDL type has no probe encoding, or the supplied
    value does not fit it (a ``u8`` given 300, an ``i64`` given ``"nine"``)."""


@dataclass(frozen=True)
class ProbeCase:
    """One instruction, ready to hand to a simulator — and every field is derived.

    ``accounts`` is in the IDL's declared order (a caller that passes it positionally
    is right by construction) and complete: PDAs derived, pinned programs literal,
    the rest bound. ``args`` is keyed by the instruction's own arg names.

    ``intent`` is the one field the graph cannot supply and does not pretend to:
    "buy a bottle of water at the bar" is not in an IDL. Absent an override it falls
    back to the identifier with its underscores removed, which is a LABEL, not a
    user's words — scoring reachability against it measures keyword echo and nothing
    else, so a REACHABLE probe wants the override.
    """

    instruction: str
    accounts: Mapping[str, str]
    args: Mapping[str, ProbeValue]
    fee_payer: str
    intent: str

    def __post_init__(self) -> None:
        # frozen protects the fields, not what they point at; a probe case that a
        # later stage can edit in place is a case the report no longer describes.
        object.__setattr__(self, "accounts", MappingProxyType(dict(self.accounts)))
        object.__setattr__(self, "args", MappingProxyType(dict(self.args)))


#: The declared IDL integer types, with the range each one actually admits. Range is
#: checked because the wire encoder would take 300 for a `u8` and write one byte.
_INT_BOUNDS: Mapping[str, tuple[int, int]] = {
    **{f"u{bits}": (0, 2**bits - 1) for bits in (8, 16, 32, 64, 128)},
    **{
        f"i{bits}": (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
        for bits in (8, 16, 32, 64, 128)
    },
}

#: The non-integer IDL arg types a probe can carry as-is.
_TEXT_TYPES: frozenset[str] = frozenset({"string", "pubkey", "bytes"})

_TRUE = frozenset({"true", "1", "yes"})
_FALSE = frozenset({"false", "0", "no"})


def probe_cases(
    graph: ProgramGraph,
    *,
    bindings: Mapping[str, str],
    intents: Mapping[str, str] | None = None,
) -> tuple[ProbeCase, ...]:
    """EVERY instruction of ``graph``, as a runnable probe case, in graph order.

    The set of instructions is the graph's and not a caller's — that is the point.
    A scorecard that deliberately does not probe one filters the RESULT and says so;
    it does not get to shorten the list by omission here.

    ``intents`` overrides the fallback intent phrase per instruction (see
    :class:`ProbeCase`). Raises :class:`ProbeError` on the first instruction it
    cannot state honestly, so a partial list is never returned: a short list reads
    as a program with fewer instructions than it has.
    """
    return tuple(
        probe_case(
            graph, ix.name, bindings=bindings, intent=(intents or {}).get(ix.name)
        )
        for ix in graph.instructions
    )


def probe_case(
    graph: ProgramGraph,
    instruction: str,
    *,
    bindings: Mapping[str, str],
    intent: str | None = None,
) -> ProbeCase:
    """One instruction's probe case. See :func:`probe_cases`."""
    ix = find_instruction(graph, instruction)
    args = _args(ix, bindings)
    accounts = _accounts(graph, ix, args, bindings)
    return ProbeCase(
        instruction=ix.name,
        accounts=accounts,
        args=args,
        fee_payer=_fee_payer(ix, accounts, bindings),
        intent=intent or ix.name.replace("_", " ").strip(),
    )


# ---------------------------------------------------------------------------
# args
# ---------------------------------------------------------------------------


def _args(ix: InstructionGraph, bindings: Mapping[str, str]) -> dict[str, ProbeValue]:
    """The instruction's args, named by the graph and typed by the graph."""
    return {name: _arg_value(ix, name, ty, bindings) for name, ty in ix.args}


def _arg_value(
    ix: InstructionGraph, name: str, declared: str, bindings: Mapping[str, str]
) -> ProbeValue:
    raw = _require(bindings, name, f"arg {name!r} ({declared}) of {ix.name!r}")
    if declared in _TEXT_TYPES:
        return raw
    if declared == "bool":
        lowered = str(raw).strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise UnprobableArgTypeError(
            f"arg {name!r} of {ix.name!r} is a bool and {raw!r} is neither "
            f"{sorted(_TRUE)} nor {sorted(_FALSE)}"
        )
    bounds = _INT_BOUNDS.get(declared)
    if bounds is None:
        raise UnprobableArgTypeError(
            f"arg {name!r} of {ix.name!r} has declared type {declared!r}, which this "
            "projector has no encoding for; a probe will not widen it to a string, "
            "which changes how the instruction is encoded"
        )
    try:
        value = int(str(raw).strip(), 10)
    except ValueError as exc:
        raise UnprobableArgTypeError(
            f"arg {name!r} of {ix.name!r} is a {declared} and {raw!r} is not an integer"
        ) from exc
    low, high = bounds
    if not low <= value <= high:
        raise UnprobableArgTypeError(
            f"arg {name!r} of {ix.name!r} is a {declared} ({low}..{high}) and "
            f"{value} does not fit — the encoder would truncate it silently"
        )
    return value


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------


def _accounts(
    graph: ProgramGraph,
    ix: InstructionGraph,
    args: Mapping[str, ProbeValue],
    bindings: Mapping[str, str],
) -> dict[str, str]:
    """Every account slot -> an address, resolved in dependency order.

    Non-PDA slots first (a pinned literal, or a binding), because a PDA seed may read
    one of them; then the PDAs in the graph's own ``derivation_order``, so an account
    that seeds another is already an address by the time it is needed.
    """
    resolved: dict[str, str] = {}
    for account in ix.accounts:
        if account.is_pda:
            continue
        resolved[account.name] = account.address or _require(
            bindings,
            account.name,
            f"account {account.name!r} of {ix.name!r}"
            + (" (signer)" if account.signer else ""),
        )
    for name in ix.derivation_order:
        resolved[name] = _derive(graph, ix, _account(ix, name), resolved, args)
    # the IDL's order, not the resolution order — a positional caller must be right
    return {account.name: resolved[account.name] for account in ix.accounts}


def _account(ix: InstructionGraph, name: str) -> AccountRef:
    for account in ix.accounts:
        if account.name == name:
            return account
    raise ProbeError(
        f"instruction {ix.name!r} orders account {name!r} for derivation but does "
        "not declare it — the graph is internally inconsistent"
    )


def _derive(
    graph: ProgramGraph,
    ix: InstructionGraph,
    account: AccountRef,
    resolved: Mapping[str, str],
    args: Mapping[str, ProbeValue],
) -> str:
    """One PDA address, from the graph's recipe and nothing else."""
    node = graph.pdas.get(account.name)
    if node is None:
        raise UnderivableAccountError(
            f"account {account.name!r} of {ix.name!r} is flagged as a PDA but the "
            "graph carries no seed recipe for it"
        )
    if not account.resolvable:
        raise UnderivableAccountError(
            f"account {account.name!r} of {ix.name!r} cannot be derived — the graph "
            "flags it unresolvable (an unresolved seed, or a seed-dependency cycle). "
            "A probe will not carry a well-formed address it cannot stand behind"
        )
    aliases = arg_aliases(ix)
    bound = {b.seed_name: b for b in account.derive_from}
    seed_values: dict[str, str | int | bytes] = {}
    for seed in node.seeds:
        if not isinstance(seed, VariablePdaSeedNode):
            continue  # constants carry their own bytes; anything else derive_pda refuses
        seed_values[seed.name] = _seed_value(
            ix, account, seed, bound.get(seed.name), resolved, args, aliases
        )
    program = node.program_id or graph.program_id
    if not program:
        raise UnderivableAccountError(
            f"PDA {account.name!r} of {ix.name!r} has no program id, and a plausible "
            "default is exactly the silent-wrong-address failure"
        )
    try:
        return derive_pda(node, seed_values, program_id=program).address
    except PdaError as exc:
        raise UnderivableAccountError(
            f"PDA {account.name!r} of {ix.name!r} did not derive: {exc}"
        ) from exc


def _seed_value(
    ix: InstructionGraph,
    account: AccountRef,
    seed: VariablePdaSeedNode,
    binding: SeedBinding | None,
    resolved: Mapping[str, str],
    args: Mapping[str, ProbeValue],
    aliases: Mapping[str, str],
) -> str | int | bytes:
    """Where this seed's value comes from — the SAME account map and the SAME args
    the instruction is being probed with, so a seed and the slot it mirrors cannot
    disagree."""
    kind = binding.kind if binding is not None else "unresolved"
    bound_to = binding.bound_to if binding is not None else None
    if kind == "account":
        target = bound_to or seed.name
        address = resolved.get(target)
        if address is None:
            raise UnderivableAccountError(
                f"PDA {account.name!r} of {ix.name!r} seeds on account {target!r}, "
                "which this instruction does not declare"
            )
        return address
    name = bound_to if kind == "argument" else aliases.get(seed.name)
    if name is None:
        raise UnderivableAccountError(
            f"PDA {account.name!r} of {ix.name!r} seeds on {seed.name!r}, which is "
            "neither one of this instruction's accounts nor one of its args. A value "
            "invented here derives a real address belonging to somebody else"
        )
    value = args.get(name)
    if value is None:
        raise UnderivableAccountError(
            f"PDA {account.name!r} of {ix.name!r} seeds on arg {name!r}, which the "
            "instruction does not declare"
        )
    return value


def _fee_payer(
    ix: InstructionGraph, accounts: Mapping[str, str], bindings: Mapping[str, str]
) -> str:
    """The instruction's first declared non-PDA signer pays.

    A signer is by definition a key the caller controls, so it is the honest fee
    payer. An instruction that declares none asks for one explicitly rather than
    borrowing an account that happens to be lying around.
    """
    for account in ix.accounts:
        if account.signer and not account.is_pda:
            return accounts[account.name]
    return _require(
        bindings, "fee_payer", f"fee payer ({ix.name!r} declares no signer account)"
    )


def _require(bindings: Mapping[str, str], key: str, subject: str) -> str:
    value = bindings.get(key)
    if not value:
        raise MissingProbeBindingError(
            f"no binding for {key!r} — the graph needs it as the {subject}. Supply it "
            "in `bindings`; nothing here substitutes a default, because a made-up "
            "value is judged by the chain and reported as a defect in the surface"
        )
    return value
