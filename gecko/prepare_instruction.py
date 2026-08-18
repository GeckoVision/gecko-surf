"""Prepare ANY instruction of ANY comprehended program — unsigned, simulated, or refused.

``prepare_purchase`` does this for one instruction of one program. Everything specific to
that store lives in its plan; everything general lives here, so a second program is a
caller rather than a copy.

THE SEAM THIS IS BUILT ON, AND WHY IT IS THE RIGHT ONE. A catalogue that holds an IDL can
already *construct* an instruction — encode the arguments, fetch a blockhash, serialize.
What it cannot do is *derive the accounts*, because an Anchor IDL states a seed's path and
not its width, and states some seeds as fields of the very account being addressed. So the
division is: we derive, the builder builds. Measured on jurassic_fi `contribute` — our
accounts handed to a partner's own builder produced a transaction that simulates on
mainnet at 21,368 CU, bit-identical to one we built ourselves. The partner's construction
was never wrong; only the derivation was missing.

WHAT IT WILL NOT DO. It does not sign, it does not broadcast, and it does not guess. An
account it cannot derive is named, with what it would need, and the whole call is refused —
because a transaction missing one account fails at signing time, far from here, and a
transaction carrying a WRONG account is worse: it is well-formed, it may even land, and
nothing downstream catches it.

Every response is returned to the caller and never persisted (invariant #1).
"""

from __future__ import annotations

from typing import Any, Callable, Literal, Mapping

from .pda import (
    PdaNode,
    ResolverPdaSeedNode,
    VariablePdaSeedNode,
    derive_pda,
)
from .program_graph import ProgramGraph, build_program_graph

__all__ = [
    "PrepareInstructionRefusal",
    "AccountOrigin",
    "plan_accounts",
    "prepare_instruction_result",
]

#: How each account in the plan got its address. This is the provenance an agent needs to
#: decide how much to trust the call, and the field an A2A capability card would carry:
#: ``pinned`` is the program's own word, ``derived`` is computed from the graph,
#: ``supplied`` is the caller's claim and is the only one nobody has verified.
AccountOrigin = Literal["pinned", "derived", "supplied"]

PrepareInstructionRefusal = Literal[
    "program-unknown",
    "instruction-unknown",
    "accounts-unresolved",
    "argument-missing",
    "build-failed",
    "simulation-reverted",
]

#: Resolve a program id to its IDL. Injected, so the whole path is falsifiable offline and
#: so the catalogue this composes with is the caller's choice, not this module's.
IdlFetch = Callable[[str], dict[str, Any]]
#: Build an unsigned transaction from a fully-resolved plan. Injected for the same reason —
#: and because the builder that does this best is usually the catalogue's own.
BuildCall = Callable[..., str]
RpcCall = Callable[[str, str, list[Any]], dict[str, Any]]


def _refuse(
    code: PrepareInstructionRefusal, reason: str, **extra: Any
) -> dict[str, Any]:
    return {"refused": True, "code": code, "reason": reason, **extra}


def plan_accounts(
    graph: ProgramGraph,
    instruction: str,
    values: Mapping[str, Any],
) -> tuple[dict[str, str], list[dict[str, str]], list[dict[str, Any]]]:
    """Resolve every account slot of ``instruction``.

    Returns ``(resolved, origins, missing)``. An account is resolved from, in order: the
    address the IDL PINS (the program's own word, and never something to ask a caller for
    — asking is how a flow ends up parameterising the token program), a value the caller
    SUPPLIED, or DERIVATION from the graph. Anything left is reported with the seeds it
    still needs, never filled in.
    """
    target = next((ix for ix in graph.instructions if ix.name == instruction), None)
    if target is None:
        return {}, [], []

    resolved: dict[str, str] = {}
    origins: list[dict[str, str]] = []
    missing: list[dict[str, Any]] = []

    for account in target.accounts:
        if account.address:
            resolved[account.name] = account.address
            origins.append({"account": account.name, "origin": "pinned"})
            continue
        supplied = values.get(account.name)
        if isinstance(supplied, str) and supplied:
            resolved[account.name] = supplied
            origins.append({"account": account.name, "origin": "supplied"})
            continue
        if not account.is_pda:
            missing.append(
                {
                    "account": account.name,
                    "why": "not a PDA — the caller must supply this address",
                    "signer": account.signer,
                }
            )
            continue

        node = graph.pdas.get(account.name)
        if node is None or not account.resolvable:
            missing.append(
                {
                    "account": account.name,
                    "why": "the graph flags this recipe unresolvable",
                    "needs": sorted(
                        {b.seed_name for b in account.derive_from if not b.encoding}
                    ),
                }
            )
            continue

        # Seed values come from what the caller holds plus what we have already derived —
        # which is why derivation_order matters: `user_position` needs `launch` first.
        bindings: dict[str, Any] = {**resolved, **values}
        try:
            resolved[account.name] = derive_pda(node, bindings).address
        except Exception as exc:  # noqa: BLE001 - a failure to derive is an ANSWER
            missing.append(
                {
                    "account": account.name,
                    "why": f"{type(exc).__name__}: {exc}",
                    "needs": sorted(_unbound(node, bindings)),
                }
            )
            continue
        origins.append({"account": account.name, "origin": "derived"})

    return resolved, origins, missing


def _unbound(node: PdaNode, bindings: Mapping[str, Any]) -> set[str]:
    """The seed names a recipe still wants — what a caller must go and find."""
    return {
        seed.name
        for seed in node.seeds
        if isinstance(seed, (VariablePdaSeedNode, ResolverPdaSeedNode))
        and seed.name not in bindings
    }


def prepare_instruction_result(
    arguments: Mapping[str, Any],
    *,
    idl_fetch: IdlFetch,
    build_call: BuildCall,
    rpc_call: RpcCall | None = None,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    """Plan, build and simulate one instruction. Never raises for an answer.

    An expected outcome — an unknown instruction, an account nobody can derive, a
    simulation that reverts — comes back as a structured refusal carrying no transaction,
    because each is something the caller must handle rather than retry.
    """
    args = arguments or {}
    program_id = str(args.get("program_id") or "").strip()
    instruction = str(args.get("instruction") or "").strip()
    values: Mapping[str, Any] = args.get("values") or {}
    payer = str(args.get("payer") or "").strip()

    if not program_id:
        return _refuse("program-unknown", "no program_id was given")

    try:
        idl = idl_fetch(program_id)
    except Exception as exc:  # noqa: BLE001
        return _refuse(
            "program-unknown",
            f"the catalogue could not resolve {program_id}: {type(exc).__name__}",
        )

    graph = build_program_graph(idl=idl, program_id=program_id)
    names = [ix.name for ix in graph.instructions]
    if instruction not in names:
        return _refuse(
            "instruction-unknown",
            f"{instruction!r} is not an instruction of this program",
            available=names,
        )

    target = next(ix for ix in graph.instructions if ix.name == instruction)
    declared_args = [name for name, _ in target.args]
    absent = [name for name in declared_args if name not in values]
    if absent:
        return _refuse(
            "argument-missing",
            f"{instruction} declares {len(declared_args)} argument(s) and "
            f"{len(absent)} were not supplied",
            missing_arguments=[
                {"name": name, "type": ty} for name, ty in target.args if name in absent
            ],
        )

    resolved, origins, missing = plan_accounts(graph, instruction, values)
    if missing:
        return _refuse(
            "accounts-unresolved",
            f"{len(missing)} account(s) of {instruction} could not be resolved; a "
            "transaction is not built from a partial plan",
            missing_accounts=missing,
            resolved_accounts=resolved,
        )

    try:
        transaction = build_call(
            program_id=program_id,
            instruction=instruction,
            accounts=resolved,
            args={name: values[name] for name in declared_args},
            payer=payer,
        )
    except Exception as exc:  # noqa: BLE001
        return _refuse(
            "build-failed", f"the builder refused: {type(exc).__name__}: {exc}"
        )

    result: dict[str, Any] = {
        "refused": False,
        "signed": False,
        "instruction": instruction,
        "program_id": program_id,
        "accounts": resolved,
        "account_origins": origins,
        "derivation_order": list(target.derivation_order),
        "transaction_base64": transaction,
    }

    if rpc_call and rpc_url:
        simulation = rpc_call(
            rpc_url,
            "simulateTransaction",
            [
                transaction,
                {
                    "encoding": "base64",
                    "sigVerify": False,
                    "replaceRecentBlockhash": True,
                    "commitment": "processed",
                },
            ],
        )
        value = (simulation.get("result") or {}).get("value") or {}
        if value.get("err") is not None:
            return _refuse(
                "simulation-reverted",
                "the program rejected this call; the bytes are not handed over",
                error=value.get("err"),
                logs=(value.get("logs") or [])[-12:],
                accounts=resolved,
            )
        result["simulation"] = {
            "err": None,
            "compute_units": value.get("unitsConsumed"),
        }

    return result
