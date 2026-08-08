"""The instruction↔PDA derivation graph — the join, assembled and emitted.

Phases 0–2a recover a program's ``{account: PdaNode}`` seed recipes (from IDL and
source). This module joins them to the *instructions* — which account of which
instruction is a PDA, what each PDA is derived from (other accounts / args of the
same instruction), and in what order the agent must derive them — and emits it as a
**structured graph**, not a text llms.txt.

This is the same correlation/call-graph Gecko builds for APIs, extended to
programs/PDAs: an orchestrator (Orquestra) ingests the JSON, and an agent reads a
clear plan — "to build `open_round`, derive `round` from arg `id` and `miner` from
account `authority`, then call." The seeds an IDL/llms.txt drops become explicit,
provenance-carrying edges.

Pure stdlib: consumes the model from :mod:`gecko.pda` / :mod:`gecko.pda_extract` and
produces JSON-serializable dataclasses. No derivation here (that is the caller's
step, via :func:`gecko.pda.derive_pda`).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
from typing import Any

from .pda import (
    ConstantPdaSeedNode,
    OrderedPairPdaSeedNode,
    PdaNode,
    ResolverPdaSeedNode,
    VariablePdaSeedNode,
)
from .pda_extract import from_anchor_idl, from_source, merge_pda_nodes_with_origin
from .provenance import ProgramProvenanceTier

__all__ = [
    "SeedBinding",
    "AccountRef",
    "InstructionGraph",
    "ProgramGraph",
    "build_program_graph",
    "derivation_order_for",
    "derivation_order_with_cycle",
]


@dataclass(frozen=True)
class SeedBinding:
    """One variable seed of a PDA, bound to where its value comes from *in this
    instruction*: an account or an arg of the same name, or ``None`` if the seed
    references nothing in the instruction (cross-instruction / external → flag)."""

    seed_name: str
    encoding: str
    kind: str  # "account" | "argument" | "unresolved"
    bound_to: str | None


@dataclass(frozen=True)
class AccountRef:
    """An account slot of an instruction. If it is a PDA, its derivation inputs are
    bound to this instruction's accounts/args; if a seed couldn't be resolved — or
    the account sits in (or behind) a seed-dependency cycle — the account is flagged
    ``resolvable=False`` (honest, not fabricated)."""

    name: str
    is_pda: bool
    signer: bool = False
    writable: bool = False
    resolvable: bool = True
    derive_from: tuple[SeedBinding, ...] = ()


@dataclass(frozen=True)
class InstructionGraph:
    """One instruction: its args, its account slots, and the order in which the
    agent must derive the PDA accounts (dependency-first).

    ``cycle`` is the honest gap on the ORDER itself: the PDA accounts that could
    not be topologically placed because they sit in a seed-dependency cycle, or
    depend on one. They stay in ``derivation_order`` (dropping an account is the
    worse failure — it fails at build/sign time, far from here) but their position
    there is arbitrary, they are each ``resolvable=False``, and this field names
    them. An empty tuple means the order is genuinely derivable end to end.
    """

    name: str
    args: tuple[tuple[str, str], ...]  # (name, type)
    accounts: tuple[AccountRef, ...]
    derivation_order: tuple[str, ...]
    cycle: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "args": [{"name": n, "type": t} for n, t in self.args],
            "accounts": [_account_to_json(a) for a in self.accounts],
            "derivation_order": list(self.derivation_order),
            # always emitted, so a consumer cannot mistake "no cycle key" for
            # "this producer does not report cycles"
            "cycle": list(self.cycle),
        }


@dataclass(frozen=True)
class ProgramGraph:
    """A program's full instruction↔PDA graph: the recovered PDA recipes plus every
    instruction's join. ``to_json`` is the plug-and-play payload.

    ``origins`` is metadata ABOUT this generation run — ``{account: tier}`` from
    :func:`~gecko.pda_extract.merge_pda_nodes_with_origin`, saying which input the
    kept recipe came from. It deliberately lives here and not on
    :class:`~gecko.pda.PdaNode`: origin is not part of a recipe's identity, and
    putting it there would change the frozen node's equality, which the merge rule
    and the config round-trip both depend on.
    """

    program_id: str | None
    pdas: dict[str, PdaNode]
    instructions: tuple[InstructionGraph, ...]
    origins: dict[str, ProgramProvenanceTier] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "pdas": {
                name: _pda_to_json(node, self.origins.get(name))
                for name, node in self.pdas.items()
            },
            "instructions": [ix.to_json() for ix in self.instructions],
        }


def _pda_to_json(node: PdaNode, origin: ProgramProvenanceTier | None) -> dict[str, Any]:
    """One PDA recipe as the AGENT sees it.

    ``origin`` is a load-bearing trust signal, not decoration — an agent may weigh
    these differently, so the meanings are fixed here and are a one-way commitment:

    - ``"extracted"`` — the program's own IDL states this recipe. Highest trust
      available from a machine surface.
    - ``"recovered"`` — the IDL dropped or left this recipe opaque (Anchor #4057)
      and it was reconstructed by regex-parsing program SOURCE, which is untrusted
      input. Correct in practice and better than the gap it fills, but it is a
      reconstruction: an agent doing something irreversible should treat it as a
      claim to verify (simulate), not as the program's own word.
    - ``"manual"`` — hand-supplied through an explicit overlay; cannot reach this
      emit site from :func:`build_program_graph`, which has no overlay step.
    - ``null`` — this producer does not know. Emitted, never omitted and never
      defaulted to ``"extracted"``: silence would read as "no origin reported",
      and a default would manufacture confidence nobody claimed.
    """
    return {
        "name": node.name,
        "program_id": node.program_id,
        "resolvable": node.resolvable,
        "origin": origin,
        "seeds": [_seed_to_json(s) for s in node.seeds],
    }


def _seed_to_json(seed: Any) -> dict[str, Any]:
    if isinstance(seed, ConstantPdaSeedNode):
        out: dict[str, Any] = {
            "kind": "const",
            "encoding": seed.encoding,
            "bytes_b64": base64.b64encode(seed.value).decode("ascii"),
        }
        if seed.encoding == "utf8":
            out["utf8"] = seed.value.decode("utf-8", "replace")
        return out
    if isinstance(seed, VariablePdaSeedNode):
        d: dict[str, Any] = {
            "kind": "variable",
            "name": seed.name,
            "source": seed.source,
            "encoding": seed.encoding,
        }
        if seed.width is not None:
            d["width"] = seed.width
        return d
    if isinstance(seed, OrderedPairPdaSeedNode):
        return {
            "kind": "ordered_pair",
            "select": seed.select,
            "left": seed.left,
            "right": seed.right,
            "encoding": seed.encoding,
        }
    # ResolverPdaSeedNode
    return {
        "kind": "resolver",
        "name": seed.name,
        "depends_on": list(seed.depends_on),
        "reason": seed.reason,
    }


def _account_to_json(acct: AccountRef) -> dict[str, Any]:
    d: dict[str, Any] = {
        "name": acct.name,
        "is_pda": acct.is_pda,
        "signer": acct.signer,
        "writable": acct.writable,
    }
    if acct.is_pda:
        d["resolvable"] = acct.resolvable
        d["derive_from"] = [
            {
                "seed": b.seed_name,
                "encoding": b.encoding,
                "kind": b.kind,
                "bound_to": b.bound_to,
            }
            for b in acct.derive_from
        ]
    return d


def _bind_seeds(
    node: PdaNode, account_names: set[str], arg_names: set[str]
) -> tuple[SeedBinding, ...]:
    """Bind each variable/resolver seed of a PDA to the account/arg of the same name
    in the instruction. A seed that matches nothing is flagged ``unresolved``."""
    bindings: list[SeedBinding] = []
    for seed in node.seeds:
        if isinstance(seed, VariablePdaSeedNode):
            if seed.source == "account" and seed.name in account_names:
                bindings.append(
                    SeedBinding(seed.name, seed.encoding, "account", seed.name)
                )
            elif seed.source == "argument" and seed.name in arg_names:
                bindings.append(
                    SeedBinding(seed.name, seed.encoding, "argument", seed.name)
                )
            else:
                # falls back to whichever namespace it does appear in, else external
                bound = (
                    seed.name
                    if seed.name in account_names or seed.name in arg_names
                    else None
                )
                kind = (
                    "account"
                    if seed.name in account_names
                    else ("argument" if seed.name in arg_names else "unresolved")
                )
                bindings.append(SeedBinding(seed.name, seed.encoding, kind, bound))
        elif isinstance(seed, OrderedPairPdaSeedNode):
            for operand in (seed.left, seed.right):
                kind = (
                    "account"
                    if operand in account_names
                    else ("argument" if operand in arg_names else "unresolved")
                )
                bound = operand if kind != "unresolved" else None
                bindings.append(SeedBinding(operand, seed.encoding, kind, bound))
        elif isinstance(seed, ResolverPdaSeedNode):
            for dep in seed.depends_on or (seed.name,):
                kind = (
                    "account"
                    if dep in account_names
                    else ("argument" if dep in arg_names else "unresolved")
                )
                bound = dep if kind != "unresolved" else None
                bindings.append(SeedBinding(seed.name, "", kind, bound))
    return tuple(bindings)


def _derivation_order(
    pda_accounts: dict[str, AccountRef],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Topologically order the PDA accounts so each is derived after any PDA it
    depends on (a seed bound to another PDA account in the same instruction).

    Returns ``(order, cycle)``. Kahn's algorithm; when it stalls, the residual is a
    dependency cycle plus everything downstream of it. Those names are **not
    dropped** — they are appended in declaration order so no account disappears from
    the plan — but they are returned as ``cycle`` so the caller can flag them. A
    plausible-looking order over an unorderable set is a lie, and lying here fails at
    build time, far from here.

    A self-seeded PDA (a seed bound to the account being derived) counts as a cycle:
    its own address is an input to its own derivation, so it is un-derivable.
    """
    names = list(pda_accounts)
    deps: dict[str, set[str]] = {n: set() for n in names}
    for name, acct in pda_accounts.items():
        for b in acct.derive_from:
            if b.kind == "account" and b.bound_to in pda_accounts:
                deps[name].add(b.bound_to)

    order: list[str] = []
    resolved: set[str] = set()
    # stable: iterate declaration order, emit any whose deps are all resolved
    while len(order) < len(names):
        progressed = False
        for n in names:
            if n in resolved:
                continue
            if deps[n] <= resolved:
                order.append(n)
                resolved.add(n)
                progressed = True
        if not progressed:
            blocked = [n for n in names if n not in resolved]
            order.extend(blocked)
            return tuple(order), tuple(blocked)
    return tuple(order), ()


def derivation_order_with_cycle(
    pdas: dict[str, PdaNode], accounts: tuple[str, ...] | list[str]
) -> tuple[tuple[str, ...], frozenset[str]]:
    """Dependency-order a subset of a program's PDA accounts, **with the honest gap
    on the order itself** (the public seam :mod:`gecko.find_start` uses).

    Reuses the same seed-binding + Kahn ordering as :func:`build_program_graph`:
    an account whose seed is bound to another listed account (a resolver's
    ``depends_on``, or a variable seed naming it) is derived after it. Names not
    present in ``pdas`` are kept in place (they are non-PDA slots, not dropped).

    Returns ``(order, cycle)``. ``cycle`` names the accounts whose position in
    ``order`` is arbitrary because they sit in, or behind, a seed-dependency cycle —
    a caller must report those as gaps rather than derive them in the order given.
    """
    names = [a for a in accounts]
    listed = set(names)
    refs: dict[str, AccountRef] = {}
    for name in names:
        node = pdas.get(name)
        if node is None:
            continue
        refs[name] = AccountRef(
            name=name,
            is_pda=True,
            resolvable=node.resolvable,
            derive_from=_bind_seeds(node, listed, set()),
        )
    ordered_pdas, cycle = _derivation_order(refs)
    # merge: PDA accounts in dependency order, non-PDA names in declared position
    result: list[str] = []
    pda_iter = iter(ordered_pdas)
    for name in names:
        result.append(next(pda_iter) if name in refs else name)
    return tuple(result), frozenset(cycle)


def derivation_order_for(
    pdas: dict[str, PdaNode], accounts: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    """The order alone. Prefer :func:`derivation_order_with_cycle` — a caller that
    only takes the order cannot tell a derivable plan from an unorderable one."""
    return derivation_order_with_cycle(pdas, accounts)[0]


def build_program_graph(
    idl: dict[str, Any] | None = None,
    source: str | None = None,
    *,
    program_id: str | None = None,
) -> ProgramGraph:
    """Assemble the instruction↔PDA graph from an Anchor IDL and/or program source.

    IDL provides instruction breadth (accounts + args) and its array-form seeds;
    source fills the recipes the IDL dropped (#4057). At least one of ``idl`` /
    ``source`` must be given. ``program_id`` overrides what the inputs carry.
    """
    if idl is None and source is None:
        raise ValueError("build_program_graph needs an idl and/or source")

    idl_nodes = from_anchor_idl(idl) if idl else {}
    prog = (
        program_id
        or (idl or {}).get("address")
        or ((idl or {}).get("metadata") or {}).get("address")
    )
    source_nodes = from_source(source, program_id=prog) if source else {}
    pdas, origins = merge_pda_nodes_with_origin(idl_nodes, source_nodes)
    # stamp the resolved program id onto every recipe
    pdas = {
        name: (node if node.program_id else PdaNode(node.name, node.seeds, prog))
        for name, node in pdas.items()
    }

    instructions: list[InstructionGraph] = []
    for ix in (idl or {}).get("instructions", []):
        arg_pairs = tuple(
            (a.get("name", ""), _type_str(a.get("type"))) for a in ix.get("args", [])
        )
        arg_names = {n for n, _ in arg_pairs}
        raw_accounts = ix.get("accounts", [])
        account_names = {a.get("name") for a in raw_accounts if a.get("name")}

        accounts: list[AccountRef] = []
        pda_accounts: dict[str, AccountRef] = {}
        for a in raw_accounts:
            name = a.get("name")
            if not name:
                continue
            node = pdas.get(name)
            if node is None:
                accounts.append(
                    AccountRef(
                        name=name,
                        is_pda=False,
                        signer=bool(a.get("signer")),
                        writable=bool(a.get("writable")),
                    )
                )
                continue
            bindings = _bind_seeds(node, account_names, arg_names)
            ref = AccountRef(
                name=name,
                is_pda=True,
                signer=bool(a.get("signer")),
                writable=bool(a.get("writable")),
                resolvable=node.resolvable,
                derive_from=bindings,
            )
            accounts.append(ref)
            pda_accounts[name] = ref

        order, cycle = _derivation_order(pda_accounts)
        if cycle:
            # an account that cannot be ordered cannot be derived — say so on the
            # account, not only on the instruction
            blocked = set(cycle)
            accounts = [
                replace(a, resolvable=False) if a.name in blocked else a
                for a in accounts
            ]
        instructions.append(
            InstructionGraph(
                name=ix.get("name", ""),
                args=arg_pairs,
                accounts=tuple(accounts),
                derivation_order=order,
                cycle=cycle,
            )
        )

    return ProgramGraph(
        program_id=prog,
        pdas=pdas,
        instructions=tuple(instructions),
        origins=origins,
    )


def _type_str(ty: Any) -> str:
    """Render an IDL arg type (a string or a structured ``{defined|array|...}``)."""
    if isinstance(ty, str):
        return ty
    if isinstance(ty, dict):
        if "defined" in ty:
            defined = ty["defined"]
            return (
                defined.get("name", "defined")
                if isinstance(defined, dict)
                else str(defined)
            )
        return next(iter(ty), "complex")
    return "unknown"
