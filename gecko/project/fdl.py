"""ProgramGraph -> one Orquestra FDL document (the Flow Definition Language).

Orquestra executes a *flow*: a JSON DAG of typed nodes ending in one unsigned
transaction. Gecko already knows the thing a flow author has to get right and
usually cannot see — which account of which instruction is a PDA, and from which
arg/account each of its seeds is filled. This module renders that join into HIS
vocabulary, so a flow is generated from the recovered recipe instead of hand-typed
from an IDL that dropped it.

Document assembly lives here; the seed translation (the part that decides whether
the flow derives the right address) lives in :mod:`gecko.project.seeds`, and every
refusal in :mod:`gecko.project.errors`. This module's own refusals are about the
document: an instruction that is not there, an arg FDL cannot carry, a slug his
schema rejects.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..pda import PdaNode
from ..program_graph import AccountRef, InstructionGraph, ProgramGraph
from .errors import (
    FdlProjectionError,
    InvalidSlugError,
    UnknownInstructionError,
    UnprojectableArgError,
    UnprojectableSeedError,
    UnresolvedSeedProjectionError,
)
from .graph_read import arg_aliases, find_instruction
from .seeds import seed_entries

__all__ = [
    "FDL_VERSION",
    "FdlProjectionError",
    "InvalidSlugError",
    "UnknownInstructionError",
    "UnprojectableArgError",
    "UnprojectableSeedError",
    "UnresolvedSeedProjectionError",
    "to_fdl",
]

FDL_VERSION = "1.0"

# His `FLOW_INPUT_TYPES` (fdl-schema.ts). A flow input MUST be one of these — there
# is no bytes/u128/i8 input type, so an arg needing one is unprojectable.
_FLOW_INPUT_TYPES: frozenset[str] = frozenset(
    {"pubkey", "u64", "u32", "u16", "u8", "i64", "i32", "string", "bool", "bps"}
)

# IDL arg type -> flow input type. Deliberately partial: an arg type with no entry
# raises rather than being widened to "string", which would silently change how the
# instruction is encoded.
_ARG_INPUT_TYPES: dict[str, str] = {
    "bool": "bool",
    "i32": "i32",
    "i64": "i64",
    "pubkey": "pubkey",
    "string": "string",
    "u8": "u8",
    "u16": "u16",
    "u32": "u32",
    "u64": "u64",
}

_KEBAB_CASE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# His node-id / input-key grammar (NODE_ID_RE and the `$ref` path charset).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The flow input carrying Orquestra's own project handle. NOT the program address
# and NOT derivable from the graph: it is a row id in his catalog, so it is declared
# as a runtime input exactly as his own examples do.
_PROJECT_ID_INPUT = "projectId"


def to_fdl(
    graph: ProgramGraph,
    instruction: str,
    *,
    slug: str,
    intent: str,
    inputs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Project ONE instruction of ``graph`` into one FDL document.

    The document is: a ``resolve.pda@1`` node per PDA account (in the graph's own
    derivation order), one ``orquestra.build_instruction@1`` node wiring every
    account and arg, and exactly one terminal ``solana.compose_transaction@1`` —
    the two-things-every-real-flow-ends-with shape his lint requires.

    ``inputs`` overrides or adds flow-input types by name (values must be one of his
    ``FLOW_INPUT_TYPES``); every input a projection needs is derived from the graph
    without it. Node ids are a pure function of the account/instruction names, so
    re-projecting the same graph yields byte-identical JSON and a stable content hash.

    Raises :class:`~gecko.project.errors.UnresolvedSeedProjectionError`,
    :class:`~gecko.project.errors.UnprojectableSeedError`,
    :class:`~gecko.project.errors.UnprojectableArgError`,
    :class:`~gecko.project.errors.UnknownInstructionError` or
    :class:`~gecko.project.errors.InvalidSlugError` rather than emitting a document
    it cannot stand behind.
    """
    if not _KEBAB_CASE_RE.match(slug):
        raise InvalidSlugError(
            f"meta.slug {slug!r} must be kebab-case (lowercase alphanumerics "
            "separated by single hyphens) — his schema rejects anything else"
        )
    if not intent:
        raise FdlProjectionError("meta.intent must be a non-empty string")

    ix = find_instruction(graph, instruction)
    arg_types = dict(ix.args)
    aliases = arg_aliases(ix)

    flow_inputs: dict[str, dict[str, str]] = {
        _PROJECT_ID_INPUT: {
            "type": "string",
            "description": "Orquestra project id for this program's registered IDL.",
        }
    }
    account_values = _account_values(ix, flow_inputs)
    _declare_arg_inputs(ix, flow_inputs)
    _apply_input_overrides(flow_inputs, inputs)

    fee_payer = _fee_payer_ref(ix, account_values, flow_inputs)

    nodes: list[dict[str, Any]] = []
    for name in ix.derivation_order:
        account = _account(ix, name)
        nodes.append(
            {
                "id": _pda_node_id(name),
                "type": "resolve.pda@1",
                "in": _resolve_pda_input(
                    graph, account, account_values, arg_types, aliases
                ),
            }
        )

    build_id = _build_node_id(ix.name)
    nodes.append(
        {
            "id": build_id,
            "type": "orquestra.build_instruction@1",
            "in": {
                "projectId": f"$inputs.{_PROJECT_ID_INPUT}",
                "instruction": ix.name,
                "accounts": {a.name: account_values[a.name] for a in ix.accounts},
                "args": {name: f"$inputs.{name}" for name, _ in ix.args},
                "feePayer": fee_payer,
            },
        }
    )
    nodes.append(
        {
            "id": "tx",
            "type": "solana.compose_transaction@1",
            "in": {
                "feePayer": fee_payer,
                "instructions": [f"${build_id}.instruction"],
                "simulate": True,
            },
        }
    )

    return {
        "fdl": FDL_VERSION,
        "meta": _meta(graph, ix, slug=slug, intent=intent),
        "inputs": flow_inputs,
        "outputs": {
            "transactions": {
                "type": "transaction[]",
                "description": (
                    f"Unsigned v0 transaction(s) carrying one {ix.name} instruction."
                ),
            }
        },
        "nodes": nodes,
        # no external.http@1 node is ever emitted, so the outbound-call budget is a
        # fact about this document, not a preference
        "policies": {"budgets": {"externalCalls": 0}},
        "x-gecko": _gecko_annex(graph, ix, aliases),
    }


# ---------------------------------------------------------------------------
# instruction / account plumbing
# ---------------------------------------------------------------------------


def _account(ix: InstructionGraph, name: str) -> AccountRef:
    for account in ix.accounts:
        if account.name == name:
            return account
    raise FdlProjectionError(
        f"instruction {ix.name!r} orders account {name!r} for derivation but does "
        "not declare it — the graph is internally inconsistent"
    )


def _account_values(
    ix: InstructionGraph, flow_inputs: dict[str, dict[str, str]]
) -> dict[str, str]:
    """Every account slot -> the FDL value that fills it, declaring inputs as needed.

    Three cases, in the order they are preferred: a PDA becomes a reference to its
    own ``resolve.pda@1`` node; an account whose address the IDL pins (the system,
    token and associated-token programs) becomes that literal address — a fact,
    never a thing to ask the caller for; anything left becomes a ``pubkey`` input.
    """
    values: dict[str, str] = {}
    for account in ix.accounts:
        if account.is_pda:
            values[account.name] = f"${_pda_node_id(account.name)}.address"
        elif account.address:
            values[account.name] = account.address
        else:
            key = _input_key(account.name, f"account of {ix.name!r}")
            flow_inputs[key] = {
                "type": "pubkey",
                "description": f"{account.name} account"
                + (" (signer)" if account.signer else ""),
            }
            values[account.name] = f"$inputs.{key}"
    return values


def _declare_arg_inputs(
    ix: InstructionGraph, flow_inputs: dict[str, dict[str, str]]
) -> None:
    for name, ty in ix.args:
        key = _input_key(name, f"arg of {ix.name!r}")
        input_type = _ARG_INPUT_TYPES.get(ty)
        if input_type is None:
            raise UnprojectableArgError(
                f"arg {name!r} of {ix.name!r} has type {ty!r}, which has no FDL "
                f"input type (FLOW_INPUT_TYPES = {sorted(_FLOW_INPUT_TYPES)}); "
                "a flow cannot carry this instruction's arguments"
            )
        flow_inputs[key] = {"type": input_type, "description": f"{name} ({ty})"}


def _apply_input_overrides(
    flow_inputs: dict[str, dict[str, str]], overrides: Mapping[str, str] | None
) -> None:
    """Apply caller-supplied input types. An existing key keeps its position (so the
    document stays byte-stable); a new key is appended in the caller's order."""
    for key, input_type in (overrides or {}).items():
        if input_type not in _FLOW_INPUT_TYPES:
            raise UnprojectableArgError(
                f"input {key!r} was given type {input_type!r}, which is not one of "
                f"his FLOW_INPUT_TYPES {sorted(_FLOW_INPUT_TYPES)}"
            )
        _input_key(key, "caller-supplied input")
        existing = flow_inputs.get(key)
        if existing is None:
            flow_inputs[key] = {"type": input_type}
        else:
            existing["type"] = input_type


def _fee_payer_ref(
    ix: InstructionGraph,
    account_values: dict[str, str],
    flow_inputs: dict[str, dict[str, str]],
) -> str:
    """The instruction's first declared signer pays. A signer is by definition a key
    the caller controls, so it is the honest fee payer; an instruction with no signer
    slot gets an explicit ``fee_payer`` input rather than a borrowed account."""
    for account in ix.accounts:
        if account.signer and not account.is_pda:
            return account_values[account.name]
    flow_inputs["fee_payer"] = {
        "type": "pubkey",
        "description": f"Fee payer ({ix.name!r} declares no signer account).",
    }
    return "$inputs.fee_payer"


def _resolve_pda_input(
    graph: ProgramGraph,
    account: AccountRef,
    account_values: dict[str, str],
    arg_types: Mapping[str, str],
    aliases: Mapping[str, str],
) -> dict[str, Any]:
    node = graph.pdas.get(account.name)
    if node is None:
        raise FdlProjectionError(
            f"account {account.name!r} is flagged as a PDA but the graph carries no "
            "seed recipe for it"
        )
    program = node.program_id or graph.program_id
    if not program:
        raise UnprojectableSeedError(
            f"PDA {account.name!r} has no program id; resolve.pda@1 needs one and a "
            "plausible default is exactly the silent-wrong-address failure"
        )
    return {
        "program": program,
        "seeds": seed_entries(node, account, account_values, arg_types, aliases),
    }


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


def _meta(
    graph: ProgramGraph, ix: InstructionGraph, *, slug: str, intent: str
) -> dict[str, Any]:
    return {
        "slug": slug,
        "name": ix.name.replace("_", " ").strip().title(),
        "description": (
            f"Builds one unsigned {ix.name} transaction. Every PDA account is "
            "derived in-flow from the program's own seed recipe, recovered by Gecko."
        ),
        "intent": intent,
        "programs": _programs(graph, ix),
        "sideEffects": _side_effects(ix),
    }


def _programs(graph: ProgramGraph, ix: InstructionGraph) -> list[str]:
    """The program addresses this flow provably touches: the owning program, plus any
    FOREIGN program a PDA in this instruction derives under (an ATA program).

    Deliberately narrow. The IDL also pins addresses for the system/token programs,
    but an ``AccountRef`` cannot tell a pinned program from a pinned sysvar, and
    listing a sysvar as a program is a claim the graph does not support.
    """
    found: list[str] = []
    if graph.program_id:
        found.append(graph.program_id)
    for account in ix.accounts:
        node: PdaNode | None = graph.pdas.get(account.name) if account.is_pda else None
        if node and node.program_id and node.program_id not in found:
            found.append(node.program_id)
    return found


def _side_effects(ix: InstructionGraph) -> dict[str, Any]:
    """What this flow does to chain state, at the honesty the graph supports.

    Writable/signer flags are stated by the IDL, so they are asserted. Whether the
    instruction creates an account or moves value is NOT in an IDL — those stay
    ``null`` (an explicit "this producer does not know"), never ``false``, which
    would read as "checked, and it does not".
    """
    return {
        "writes": [a.name for a in ix.accounts if a.writable],
        "signers": [a.name for a in ix.accounts if a.signer],
        "createsAccounts": None,
        "movesValue": None,
        "basis": "idl-account-flags",
    }


def _gecko_annex(
    graph: ProgramGraph, ix: InstructionGraph, aliases: Mapping[str, str]
) -> dict[str, Any]:
    """Facts the graph carries that FDL has no field for.

    His schema strips unknown top-level keys, so this rides along without changing
    the compiled plan or its content hash (verified against his compiler) — but it
    keeps the projection auditable: an FDL ``resolve.pda@1`` node states a recipe
    with no room to say whether that recipe came from the program's own IDL or from
    regex-parsed source, and a reviewer needs to know which.
    """
    return {
        "producer": "gecko.project.fdl",
        "instruction": ix.name,
        "programId": graph.program_id,
        "carriedOutsideFdl": {
            "pdaOrigins": {
                a.name: graph.origins.get(a.name) for a in ix.accounts if a.is_pda
            },
            "accountRoles": {
                a.name: {"signer": a.signer, "writable": a.writable}
                for a in ix.accounts
            },
            "argAliases": dict(aliases),
        },
    }


# ---------------------------------------------------------------------------
# identifiers
# ---------------------------------------------------------------------------


def _pda_node_id(account_name: str) -> str:
    return _node_id(f"pda_{account_name}", f"PDA account {account_name!r}")


def _build_node_id(instruction_name: str) -> str:
    return _node_id(f"ix_{instruction_name}", f"instruction {instruction_name!r}")


def _node_id(candidate: str, subject: str) -> str:
    if not _IDENTIFIER_RE.match(candidate):
        raise FdlProjectionError(
            f"{subject} yields node id {candidate!r}, which is not a valid FDL "
            "node id ([A-Za-z_][A-Za-z0-9_]*)"
        )
    return candidate


def _input_key(candidate: str, subject: str) -> str:
    if not _IDENTIFIER_RE.match(candidate):
        raise FdlProjectionError(
            f"{subject} named {candidate!r} is not a valid FDL input key "
            "([A-Za-z_][A-Za-z0-9_]*)"
        )
    return candidate
