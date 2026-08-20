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
from typing import Any, Mapping, Sequence

from .pda import (
    ConstantPdaSeedNode,
    OrderedPairPdaSeedNode,
    PdaNode,
    ResolverPdaSeedNode,
    VariablePdaSeedNode,
)
from .pda_extract import (
    blocked_only_on_itself,
    instruction_pdas,
    only_untyped_seeds,
    from_anchor_idl,
    from_source,
    merge_pda_nodes_with_origin,
)
from .provenance import ProgramProvenanceTier

__all__ = [
    "SeedBinding",
    "AccountRef",
    "InstructionGraph",
    "ProgramGraph",
    "build_program_graph",
    "chain_order_with_cycle",
    "derivation_order_for",
    "derivation_order_with_cycle",
    "topological_order",
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
    ``resolvable=False`` (honest, not fabricated).

    ``address`` is the IDL's own ``address`` pin for a constant account (the system,
    token and associated-token programs, a sysvar). It is a *fact from the spec*, not
    a derivation: a consumer that has it does not have to ask a caller for a value
    the program already fixed — and asking is how a flow ends up parameterising the
    token program. ``None`` means the IDL pinned nothing, never "unknown constant".
    """

    name: str
    is_pda: bool
    signer: bool = False
    writable: bool = False
    resolvable: bool = True
    derive_from: tuple[SeedBinding, ...] = ()
    address: str | None = None

    @property
    def satisfiable(self) -> bool:
        """True iff a CALLER of this instruction could actually bind every seed.

        NOT the same question as :attr:`resolvable`, and conflating them was a real
        defect. `resolvable` asks "was a recipe recovered for this account" — a fact about
        extraction. This asks "can the recipe be SATISFIED from what this instruction
        offers" — a fact about the caller.

        jurassic_fi's `dust_token_account` is the case that proved they differ: its recipe
        extracts cleanly — no flagged seed, nothing unresolved in the RECIPE — and building
        it still fails, because one seed names `dust_authority`, which this instruction
        does not carry. Comprehension reported "zero flagged gaps" and the build then
        failed with a missing binding. A surface that says "no gaps" about something that
        cannot be built from what it offers is making exactly the confident wrong claim
        this project exists to remove.
        """
        return self.resolvable and not any(
            binding.kind == "unresolved" for binding in self.derive_from
        )

    @property
    def caller_must_supply(self) -> tuple[str, ...]:
        """Seed names this instruction does not bind — the caller passes them in.

        Named for what a caller DOES with them rather than for what is missing. These are
        exactly the keys `prepare_instruction` expects in `values`, and they are usually
        read off-chain first (`launch.admin` lives inside the account being derived). Not
        "impossible": a value with nowhere to come from and a value that comes from a
        chain read look identical here, and calling both unbindable would overstate the
        first and understate the second.
        """
        return tuple(
            binding.seed_name
            for binding in self.derive_from
            if binding.kind == "unresolved"
        )


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
    and the config round-trip both depend on. The packaged program config carries
    the same fact the same way — a sibling ``program.pda_origins`` map (R7), never
    a node field — so the artifact and this run metadata stay one shape.
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
    if acct.address:
        d["address"] = acct.address
    if acct.is_pda:
        d["resolvable"] = acct.resolvable
        # Emitted BESIDE `resolvable`, never instead of it: one says a recipe was
        # recovered, the other says a caller of THIS instruction can satisfy it. A
        # consumer that reads only the first will call an unbuildable account clean.
        d["satisfiable"] = acct.satisfiable
        if not acct.satisfiable:
            d["caller_must_supply"] = list(acct.caller_must_supply)
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
            elif seed.source == "argument" and seed.name.partition(".")[0] in arg_names:
                # A FIELD OF AN ARGUMENT STRUCT, e.g. `params.launch_id`. The caller builds
                # `params`, so it holds the field — the binding is to the argument it came
                # from, and the seed keeps the full path so nobody has to guess which field.
                bindings.append(
                    SeedBinding(
                        seed.name,
                        seed.encoding,
                        "argument",
                        seed.name.partition(".")[0],
                    )
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


def topological_order(
    names: Sequence[str], deps: Mapping[str, set[str]]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The ONE ordering engine of this module: Kahn over ``names``, dependencies
    first, returning ``(order, cycle)``.

    Extracted so the account level (:func:`_derivation_order`, seeds) and the
    instruction level (:func:`chain_order_with_cycle`, lifecycle chains) share a
    single implementation of the contract that matters: **nothing is ever dropped**.
    When the walk stalls, the residual (the cycle plus everything downstream of it)
    is appended in declaration order AND returned as ``cycle``, so the caller reports
    a gap instead of deriving in an order that is merely plausible. A second copy of
    this loop would be a second place for that contract to rot.

    ``deps`` may name entries outside ``names``; those references are ignored here —
    the caller decides whether a dangling reference is a gap (the account level scopes
    seeds to the listed accounts; the chain level reports an unknown step itself).
    """
    known = set(names)
    order: list[str] = []
    resolved: set[str] = set()
    # stable: iterate declaration order, emit any whose deps are all resolved
    while len(order) < len(names):
        progressed = False
        for n in names:
            if n in resolved:
                continue
            if (deps.get(n, set()) & known) <= resolved:
                order.append(n)
                resolved.add(n)
                progressed = True
        if not progressed:
            blocked = [n for n in names if n not in resolved]
            order.extend(blocked)
            return tuple(order), tuple(blocked)
    return tuple(order), ()


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
    return topological_order(names, deps)


def chain_order_with_cycle(
    instructions: Sequence[str], edges: Sequence[tuple[str, str]]
) -> tuple[tuple[str, ...], frozenset[str]]:
    """Order the INSTRUCTIONS of a lifecycle chain, with the honest gap on the order.

    The account-level rail one level up: ``edges`` are ``(produces, consumes)`` pairs —
    the consumer runs after the producer — and the return is the same
    ``(order, cycle)`` contract :func:`derivation_order_with_cycle` gives for seeds.
    An instruction inside (or behind) a cycle stays in ``order`` and is named in
    ``cycle``; a caller must report those as gaps rather than execute them in the
    order given. An edge naming an instruction outside ``instructions`` orders nothing
    here — the caller reports the dangling endpoint, because silently ignoring it
    would turn a broken chain declaration into a confident sequence.
    """
    deps: dict[str, set[str]] = {name: set() for name in instructions}
    for produces, consumes in edges:
        if consumes in deps:
            deps[consumes].add(produces)
    order, cycle = topological_order(list(instructions), deps)
    return order, frozenset(cycle)


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


def _importable_here(
    node: PdaNode, account_names: set[str], arg_names: set[str]
) -> PdaNode | None:
    """A sibling instruction's recipe, but only if THIS instruction can bind it.

    An account name is not a recipe key. When an instruction does not declare a slot as a
    PDA, the program-wide map still holds whatever a *sibling* declared under that name,
    and importing it unconditionally states something about this instruction that nothing
    checked. Two measured cases, both live in the catalogue:

    - ``main::close_old_collateral_signatures`` takes ``args: []`` and a plain
      ``collateral`` slot. ``create_collateral`` seeds that name on the arg field
      ``new_collateral.id``. Imported, the plan asks this caller for
      ``new_collateral.id`` — a field of a struct the instruction has no argument for.
      ``new_collateral.id`` (36 IDLs) and ``new_coordinator.id`` (52) are the two most
      common unhinted needed values in the whole catalogue, and both are this artifact.
    - ``escrow::cancel_order`` takes ``user_token_account`` as an OPTIONAL refund
      destination with no ``pda`` block; ``post_sell_order`` declares that name as the ATA
      of ``user`` + ``token``. Imported, the slot is marked derivable and the instruction
      is reported blocked on ``user`` — an account ``cancel_order`` does not take at all,
      while the address it actually wants is the *initiator's* ATA. Wrong, and plausible.

    So the import is gated on the only thing this join can actually verify: every seed of
    the imported recipe binds to an account or an argument of THIS instruction. Measured
    across 196 cached catalogue IDLs — 413 imports, 243 refused, 170 kept, and **zero**
    that a caller could satisfy today were refused. The refused ones stop being "a PDA you
    cannot derive" and become "an account you supply", which is true and actionable.

    Deliberately gated on the BINDINGS, not on :attr:`AccountRef.satisfiable`, which also
    folds in ``node.resolvable``. Resolvability is a fact about the extraction and is the
    same for the sibling that declared it; it is already reported on its own, and refusing
    on it would collapse the two questions this module keeps apart. That distinction is
    worth 27 recipes: gating on ``satisfiable`` would refuse 270 instead of 243.

    Nothing is destroyed either way — the recipe stays in :attr:`ProgramGraph.pdas` with
    its origin, where it is true. Only the claim about this instruction stops.
    """
    bindings = _bind_seeds(node, account_names, arg_names)
    if any(binding.kind == "unresolved" for binding in bindings):
        return None
    return node


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

    # The recipe an account has is a property of the (instruction, account) pair, not of
    # the account name — measured across the catalogue: Orca declares `whirlpool` from a
    # `tick_spacing` ARG in `initialize_pool` and from an adaptive-fee-tier ACCOUNT READ in
    # `initialize_pool_with_adaptive_fee`; stableswap declares `token_vault` from the
    # `mint` account in `deposit` and from `token_state.mint` in `withdraw`. Both members
    # of each pair are correct, and `pdas` above can only hold one of them (it keeps the
    # conservative one). Here we have the instruction, so each one gets what it declares.
    idl_type_defs = {
        str(t.get("name")): t
        for t in (idl or {}).get("types", [])
        if isinstance(t, dict)
    }

    instructions: list[InstructionGraph] = []
    for ix in (idl or {}).get("instructions", []):
        declared_here = instruction_pdas(
            ix,
            program_id=prog,
            type_defs=idl_type_defs,
            # The IDL itself, so a dotted account seed can carry WHERE to read its
            # value from — the `account` key Anchor already declares and we used to
            # discard. Optional everywhere else, so nothing that omits it changes.
            layout_idl=idl,
        )
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
            pinned = a.get("address")
            pinned = str(pinned) if pinned else None
            # This instruction's own declaration first; the merged/source-recovered map
            # is the fallback for accounts it does not declare (the #4057 gap included).
            #
            # The one case where the instruction's own word is NOT the best answer is a
            # self-referential dead end — `launch` seeded on `launch.admin`. No caller can
            # ever open that, and a sibling's derivable recipe is provably the same
            # address, because the program re-derives it from the stored fields and
            # rejects the transaction if it differs. So a dead end defers to the merged
            # recipe; a seed reading ANOTHER account does not, and stays flagged here.
            declared = declared_here.get(name)
            merged = pdas.get(name)
            node = declared
            if node is None and merged is not None:
                node = _importable_here(merged, account_names, arg_names)
            #
            # An EXTRACTION GAP defers the same way, for a different reason: when this
            # instruction spells a seed differently from its own arg (`mark_as_delivered`
            # seeds on `store_name`, its arg is `_store_name`), the seed could not be
            # typed here and a sibling typed the identical seed. That is one recipe badly
            # extracted, not two recipes.
            if (
                declared is not None
                and merged is not None
                and merged.resolvable
                and (blocked_only_on_itself(declared) or only_untyped_seeds(declared))
            ):
                node = merged
            if node is not None and node.program_id is None:
                node = PdaNode(node.name, node.seeds, prog)
            if node is None:
                accounts.append(
                    AccountRef(
                        name=name,
                        is_pda=False,
                        signer=bool(a.get("signer")),
                        writable=bool(a.get("writable")),
                        address=pinned,
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
                address=pinned,
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
